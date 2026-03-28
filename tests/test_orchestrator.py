import subprocess
import textwrap
import threading
import time
from pathlib import Path

from direvo.config import load_config
from direvo.db import DatabaseManager
from direvo.execution import EvaluationResult, ExecutionResult
from direvo.git_manager import GitManager
from direvo.logging import configure_logging
from direvo.models import ProposalStatus
from direvo.orchestrator import Orchestrator


class FakePlannerSession:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.completed: list[int] = []
        self.errors: list[str] = []

    def start(self) -> None:
        self.started = True

    def notify_trial_completed(self, trial_id: int) -> None:
        self.completed.append(trial_id)

    def notify_error(self, message: str) -> None:
        self.errors.append(message)

    def stop(self) -> None:
        self.stopped = True


class RaisingPlannerSession(FakePlannerSession):
    def notify_trial_completed(self, trial_id: int) -> None:
        raise RuntimeError("planner unavailable")

    def notify_error(self, message: str) -> None:
        raise RuntimeError("planner unavailable")


class FakeExecutionManager:
    def __init__(self, *, execution_success: bool = True, evaluation_success: bool = True) -> None:
        self.execution_success = execution_success
        self.evaluation_success = evaluation_success

    def run_execution(
        self, *, worktree_path: Path, slot: int, direction: str, timeout_sec: int, user: str | None = None
    ) -> ExecutionResult:
        (worktree_path / "code.txt").write_text("changed\n", encoding="utf-8")
        trial_dir = worktree_path / ".direvo" / "trial"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "implementation.md").write_text(direction, encoding="utf-8")
        return ExecutionResult(
            success=self.execution_success,
            stdout="",
            stderr="",
            returncode=0 if self.execution_success else 1,
        )

    def run_evaluation(
        self, *, worktree_path: Path, eval_script: Path, timeout_sec: int, user: str | None = None
    ) -> EvaluationResult:
        trial_dir = worktree_path / ".direvo" / "trial"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "results.md").write_text("ok\n", encoding="utf-8")
        return EvaluationResult(
            success=self.evaluation_success,
            stdout='{"test_pass_rate": 1.0}',
            stderr="",
            returncode=0 if self.evaluation_success else 1,
            metrics={"test_pass_rate": 1.0} if self.evaluation_success else {},
        )


class SlowExecutionManager(FakeExecutionManager):
    def __init__(self, delay_sec: float) -> None:
        super().__init__()
        self.delay_sec = delay_sec
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def run_execution(
        self, *, worktree_path: Path, slot: int, direction: str, timeout_sec: int, user: str | None = None
    ) -> ExecutionResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(self.delay_sec)
            return super().run_execution(
                worktree_path=worktree_path,
                slot=slot,
                direction=direction,
                timeout_sec=timeout_sec,
                user=user,
            )
        finally:
            with self._lock:
                self._active -= 1


class InterruptibleExecutionManager(SlowExecutionManager):
    def __init__(self, delay_sec: float) -> None:
        super().__init__(delay_sec=delay_sec)
        self.started = threading.Event()

    def run_execution(
        self, *, worktree_path: Path, slot: int, direction: str, timeout_sec: int, user: str | None = None
    ) -> ExecutionResult:
        self.started.set()
        return super().run_execution(
            worktree_path=worktree_path,
            slot=slot,
            direction=direction,
            timeout_sec=timeout_sec,
            user=user,
        )


class MergeResolvingExecutionManager(FakeExecutionManager):
    def run_execution(
        self, *, worktree_path: Path, slot: int, direction: str, timeout_sec: int, user: str | None = None
    ) -> ExecutionResult:
        (worktree_path / "tracked.txt").write_text("resolved\n", encoding="utf-8")
        return super().run_execution(
            worktree_path=worktree_path,
            slot=slot,
            direction=direction,
            timeout_sec=timeout_sec,
            user=user,
        )


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _current_branch(cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_orchestrator_runs_single_ready_proposal(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "worktrees").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=1.0,
        slug="smoke",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    assert planner.started
    assert planner.stopped
    assert planner.completed == [1]

    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"
    assert trial_row["branch"] == "trial/1-smoke"
    assert trial_row["commit_sha"]

    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"
    assert (tmp_path / ".direvo" / "artifacts" / "trial-1" / "plan.md").exists()
    assert not (tmp_path / "worktrees" / "wt-0").exists()


def test_orchestrator_recovers_and_requeues_failed_execution(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=1.0,
        slug="fail",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(execution_success=False),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "error"

    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "ready"
    assert proposal_row["priority"] == 0.9
    assert planner.completed == []
    assert not (tmp_path / "worktrees" / "wt-0").exists()


def test_orchestrator_records_eval_error_but_keeps_commit(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=1.0,
        slug="eval-error",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(evaluation_success=False),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "eval_error"
    assert trial_row["commit_sha"]
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"
    assert (tmp_path / ".direvo" / "artifacts" / "trial-1" / "plan.md").exists()


def test_orchestrator_creates_merge_trial_commit(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    base_branch = _current_branch(tmp_path)

    _run(["git", "checkout", "-b", "left"], cwd=tmp_path)
    tracked.write_text("left\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], cwd=tmp_path)
    _run(["git", "commit", "-m", "left"], cwd=tmp_path)
    left_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _run(["git", "checkout", base_branch], cwd=tmp_path)
    _run(["git", "checkout", "-b", "right"], cwd=tmp_path)
    tracked.write_text("right\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], cwd=tmp_path)
    _run(["git", "commit", "-m", "right"], cwd=tmp_path)
    right_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _run(["git", "checkout", base_branch], cwd=tmp_path)

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 1
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Merge the two approaches.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=1.0,
        slug="merge",
        parent_commits=[left_sha, right_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=MergeResolvingExecutionManager(),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"
    merge_commit = trial_row["commit_sha"]
    assert merge_commit
    parent_line = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-list", "--parents", "-n", "1", merge_commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert len(parent_line.split()) == 3
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"


def test_orchestrator_processes_multiple_slots_concurrently(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_root = tmp_path / ".direvo" / "proposals"
    proposal_one = proposal_root / "proposal-1"
    proposal_two = proposal_root / "proposal-2"
    proposal_one.mkdir(parents=True)
    proposal_two.mkdir(parents=True)
    (proposal_one / "plan.md").write_text("Implement the first change.\n", encoding="utf-8")
    (proposal_two / "plan.md").write_text("Implement the second change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    database_manager.create_proposal(
        priority=2.0,
        slug="first",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_one),
        status=ProposalStatus.READY,
    )
    database_manager.create_proposal(
        priority=1.0,
        slug="second",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_two),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    execution_manager = SlowExecutionManager(delay_sec=0.2)
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=execution_manager,
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 2
    assert execution_manager.max_active >= 2
    assert sorted(planner.completed) == [1, 2]


def test_orchestrator_stops_when_target_condition_is_met(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            target_condition: "test_pass_rate >= 1.0"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_root = tmp_path / ".direvo" / "proposals"
    for index, slug in enumerate(("first", "second"), start=1):
        proposal_dir = proposal_root / f"proposal-{index}"
        proposal_dir.mkdir(parents=True)
        (proposal_dir / "plan.md").write_text(f"Implement {slug}.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    database_manager.create_proposal(
        priority=2.0,
        slug="first",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_root / "proposal-1"),
        status=ProposalStatus.READY,
    )
    second_id = database_manager.create_proposal(
        priority=1.0,
        slug="second",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_root / "proposal-2"),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    second_row = database_manager.get_proposal_row(second_id)
    assert second_row is not None
    assert second_row["status"] == "ready"


def test_orchestrator_drains_in_flight_trial_after_stop_request(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_root = tmp_path / ".direvo" / "proposals"
    for index, slug in enumerate(("first", "second"), start=1):
        proposal_dir = proposal_root / f"proposal-{index}"
        proposal_dir.mkdir(parents=True)
        (proposal_dir / "plan.md").write_text(f"Implement {slug}.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    database_manager.create_proposal(
        priority=2.0,
        slug="first",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_root / "proposal-1"),
        status=ProposalStatus.READY,
    )
    second_id = database_manager.create_proposal(
        priority=1.0,
        slug="second",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_root / "proposal-2"),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    execution_manager = InterruptibleExecutionManager(delay_sec=0.2)
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=execution_manager,
        planner_session=planner,
    )

    result: dict[str, int] = {}

    def run_orchestrator() -> None:
        result["processed"] = orchestrator.run()

    thread = threading.Thread(target=run_orchestrator)
    thread.start()
    assert execution_manager.started.wait(timeout=2.0)
    orchestrator.request_stop()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result["processed"] == 1
    assert orchestrator.last_termination_reason == "user_interrupt"

    first_row = database_manager.get_trial_row(1)
    assert first_row is not None
    assert first_row["status"] == "success"

    second_row = database_manager.get_proposal_row(second_id)
    assert second_row is not None
    assert second_row["status"] == "ready"


def test_recovery_requires_clean_worktree_state(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=1.0,
        slug="dirty",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(execution_success=False),
        planner_session=planner,
    )

    original_require_clean = orchestrator.git_manager.require_clean_status
    injected_dirty_state = {"done": False, "calls": 0}

    def fail_require_clean(repo_path: Path) -> None:
        injected_dirty_state["calls"] += 1
        if injected_dirty_state["calls"] > 1 and not injected_dirty_state["done"]:
            (repo_path / "leftover.txt").write_text("dirty\n", encoding="utf-8")
            injected_dirty_state["done"] = True
        original_require_clean(repo_path)

    orchestrator.git_manager.require_clean_status = fail_require_clean  # type: ignore[method-assign]

    processed = orchestrator.run()

    assert processed == 1
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "error"
    assert "not clean after recovery" in trial_row["description"]
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "ready"


def test_orchestrator_waits_for_late_ready_proposal(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            planner_command: "planner"
            max_trials: 1
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the delayed change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(),
        planner_session=planner,
        idle_poll_interval_sec=0.05,
    )

    result: dict[str, int] = {}

    def run_orchestrator() -> None:
        result["processed"] = orchestrator.run()

    thread = threading.Thread(target=run_orchestrator)
    thread.start()
    time.sleep(0.15)

    proposal_id = database_manager.create_proposal(
        priority=1.0,
        slug="late",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result["processed"] == 1
    assert orchestrator.last_termination_reason == "max_trials"

    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"


def test_orchestrator_remaps_absolute_proposal_paths_to_workspace(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 1
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    database_manager.create_proposal(
        priority=1.0,
        slug="portable-path",
        parent_commits=[head_sha],
        artifacts_uri=f"/host/workspace/.direvo/proposals/{proposal_dir.name}",
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"


def test_orchestrator_completes_invalid_proposal_without_trial(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    invalid_docs = tmp_path / ".direvo" / "proposals" / "proposal-1"
    invalid_docs.mkdir(parents=True)
    (invalid_docs / "plan.md").write_text("Broken proposal.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=2.0,
        slug="bad slug",
        parent_commits=[head_sha],
        artifacts_uri=str(invalid_docs),
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 0
    assert database_manager.list_trials() == []
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"
    assert planner.errors
    assert "invalid slug" in planner.errors[0]


def test_trial_completion_survives_planner_notification_failure(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 1
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    proposal_dir = tmp_path / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=1.0,
        slug="notify-fail",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(),
        planner_session=RaisingPlannerSession(),
    )

    processed = orchestrator.run()

    assert processed == 1
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"


def test_invalid_proposal_survives_planner_error_notification_failure(tmp_path: Path) -> None:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    (tmp_path / "evaluate.sh").chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            max_trials: 5
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    invalid_docs = tmp_path / ".direvo" / "proposals" / "proposal-1"
    invalid_docs.mkdir(parents=True)
    (invalid_docs / "plan.md").write_text("Broken proposal.\n", encoding="utf-8")

    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    proposal_id = database_manager.create_proposal(
        priority=2.0,
        slug="bad slug",
        parent_commits=[head_sha],
        artifacts_uri=str(invalid_docs),
        status=ProposalStatus.READY,
    )

    logger = configure_logging(tmp_path / ".direvo" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeExecutionManager(),
        planner_session=RaisingPlannerSession(),
    )

    processed = orchestrator.run()

    assert processed == 0
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"
