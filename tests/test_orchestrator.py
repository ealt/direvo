import json
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest

from eden.config import load_config
from eden.db import DatabaseManager
from eden.execution import EvaluationResult, ImplementationManager, ImplementationResult
from eden.git_manager import GitManager
from eden.logging import configure_logging
from eden.models import ProposalStatus
from eden.orchestrator import Orchestrator, bootstrap
from eden.planner import PlannerSession


class FakePlannerSession(PlannerSession):
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


class FakeImplementationManager(ImplementationManager):
    def __init__(self, *, execution_success: bool = True, evaluation_success: bool = True) -> None:
        super().__init__(implement_command="echo noop")
        self.execution_success = execution_success
        self.evaluation_success = evaluation_success

    def run_implementation(
        self,
        *,
        worktree_path: Path,
        slot: int,
        timeout_sec: int,
        user: str | None = None,
        slug: str = "",
        trial_id: int = 0,
    ) -> ImplementationResult:
        (worktree_path / "code.txt").write_text("changed\n", encoding="utf-8")
        trial_dir = worktree_path / ".eden" / "trial"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "implementation.md").write_text(slug, encoding="utf-8")
        return ImplementationResult(
            success=self.execution_success,
            stdout="",
            stderr="",
            returncode=0 if self.execution_success else 1,
        )

    def run_evaluation(
        self, *, worktree_path: Path, evaluate_command: str, timeout_sec: int, user: str | None = None
    ) -> EvaluationResult:
        trial_dir = worktree_path / ".eden" / "trial"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "results.md").write_text("ok\n", encoding="utf-8")
        return EvaluationResult(
            success=self.evaluation_success,
            stdout='{"test_pass_rate": 1.0}',
            stderr="",
            returncode=0 if self.evaluation_success else 1,
            metrics={"test_pass_rate": 1.0} if self.evaluation_success else {},
        )


class SlowImplementationManager(FakeImplementationManager):
    def __init__(self, delay_sec: float) -> None:
        super().__init__()
        self.delay_sec = delay_sec
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def run_implementation(
        self,
        *,
        worktree_path: Path,
        slot: int,
        timeout_sec: int,
        user: str | None = None,
        slug: str = "",
        trial_id: int = 0,
    ) -> ImplementationResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(self.delay_sec)
            return super().run_implementation(
                worktree_path=worktree_path,
                slot=slot,
                timeout_sec=timeout_sec,
                user=user,
                slug=slug,
                trial_id=trial_id,
            )
        finally:
            with self._lock:
                self._active -= 1


class InterruptibleImplementationManager(SlowImplementationManager):
    def __init__(self, delay_sec: float) -> None:
        super().__init__(delay_sec=delay_sec)
        self.started = threading.Event()

    def run_implementation(
        self,
        *,
        worktree_path: Path,
        slot: int,
        timeout_sec: int,
        user: str | None = None,
        slug: str = "",
        trial_id: int = 0,
    ) -> ImplementationResult:
        self.started.set()
        return super().run_implementation(
            worktree_path=worktree_path,
            slot=slot,
            timeout_sec=timeout_sec,
            user=user,
            slug=slug,
            trial_id=trial_id,
        )


class MergeResolvingImplementationManager(FakeImplementationManager):
    def run_implementation(
        self,
        *,
        worktree_path: Path,
        slot: int,
        timeout_sec: int,
        user: str | None = None,
        slug: str = "",
        trial_id: int = 0,
    ) -> ImplementationResult:
        (worktree_path / "tracked.txt").write_text("resolved\n", encoding="utf-8")
        return super().run_implementation(
            worktree_path=worktree_path,
            slot=slot,
            timeout_sec=timeout_sec,
            user=user,
            slug=slug,
            trial_id=trial_id,
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


def _init_experiment(tmp_path: Path, *, tracked_contents: str = "seed\n") -> tuple[Path, Path]:
    experiment_root = tmp_path / "experiment"
    workspace = experiment_root / "planner" / "workspace"
    (experiment_root / ".eden").mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "tracked.txt").write_text(tracked_contents, encoding="utf-8")
    eval_script = experiment_root / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "test@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Test User"], cwd=workspace)
    _run(["git", "add", "."], cwd=workspace)
    _run(["git", "commit", "-m", "seed"], cwd=workspace)
    return experiment_root, workspace


def _head_sha(workspace: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_config(experiment_root: Path, body: str) -> Path:
    config_path = experiment_root / ".eden" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            planner_root: "./planner"
            workspace: "./workspace"
            """
        )
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    return config_path


def test_orchestrator_runs_single_ready_proposal(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )

    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(),
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
    assert (experiment_root / ".eden" / "artifacts" / "trial-1" / "plan.md").exists()
    assert not (config.workspace_root / "worktrees" / "wt-0").exists()
    assert orchestrator.wall_time_seconds >= 0

    log_entries = [
        json.loads(line)
        for line in (experiment_root / ".eden" / "session.log").read_text(encoding="utf-8").splitlines()
    ]
    trial_complete = next(entry for entry in log_entries if entry["event"] == "trial_complete")
    assert trial_complete["branch"] == "trial/1-smoke"
    assert trial_complete["metrics"] == {"test_pass_rate": 1.0}


def test_bootstrap_reapplies_runtime_setup_after_database_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment_root, _workspace = _init_experiment(tmp_path)
    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 1
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )

    prepare_calls: list[Path] = []

    class FakeRuntimeSetup:
        def prepare(self, config: object) -> None:
            prepare_calls.append(config.proposals_db)  # type: ignore[attr-defined]

    monkeypatch.setattr("eden.bootstrap.RuntimeSetup", FakeRuntimeSetup)

    result = bootstrap(config_path, progress=False)

    assert result.config.proposals_db.exists()
    assert prepare_calls == [result.config.proposals_db]


def test_orchestrator_records_subprocess_failure_details(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 1
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    database_manager.create_proposal(
        priority=1.0,
        slug="fail-detail",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    class FailingImplementationManager(FakeImplementationManager):
        def run_implementation(
            self,
            *,
            worktree_path: Path,
            slot: int,
            timeout_sec: int,
            user: str | None = None,
            slug: str = "",
            trial_id: int = 0,
        ) -> ImplementationResult:
            return ImplementationResult(
                success=False,
                stdout="usage: codex exec [OPTIONS]",
                stderr="error: unexpected argument '--approval-mode'",
                returncode=2,
            )

    planner = FakePlannerSession()
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FailingImplementationManager(),
        planner_session=planner,
    )

    orchestrator.run()

    log_entries = [
        json.loads(line)
        for line in (experiment_root / ".eden" / "session.log").read_text(encoding="utf-8").splitlines()
    ]
    implementation_complete = next(entry for entry in log_entries if entry["event"] == "implementation_complete")
    trial_failed = next(entry for entry in log_entries if entry["event"] == "trial_failed")

    assert implementation_complete["stderr"] == "error: unexpected argument '--approval-mode'"
    assert implementation_complete["stdout"] == "usage: codex exec [OPTIONS]"
    assert trial_failed["error"] == "implementation_failed: error: unexpected argument '--approval-mode'"


def test_orchestrator_recovers_and_requeues_failed_execution(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )

    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(execution_success=False),
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
    assert not (config.workspace_root / "worktrees" / "wt-0").exists()

    log_entries = [
        json.loads(line)
        for line in (experiment_root / ".eden" / "session.log").read_text(encoding="utf-8").splitlines()
    ]
    trial_failed = next(entry for entry in log_entries if entry["event"] == "trial_failed")
    assert trial_failed["branch"] == "trial/1-fail"


def test_orchestrator_records_eval_error_but_keeps_commit(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )

    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(evaluation_success=False),
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
    assert (experiment_root / ".eden" / "artifacts" / "trial-1" / "plan.md").exists()


def test_orchestrator_creates_merge_trial_commit(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path, tracked_contents="base\n")
    tracked = workspace / "tracked.txt"
    base_branch = _current_branch(workspace)

    _run(["git", "checkout", "-b", "left"], cwd=workspace)
    tracked.write_text("left\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], cwd=workspace)
    _run(["git", "commit", "-m", "left"], cwd=workspace)
    left_sha = _head_sha(workspace)

    _run(["git", "checkout", base_branch], cwd=workspace)
    _run(["git", "checkout", "-b", "right"], cwd=workspace)
    tracked.write_text("right\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], cwd=workspace)
    _run(["git", "commit", "-m", "right"], cwd=workspace)
    right_sha = _head_sha(workspace)

    _run(["git", "checkout", base_branch], cwd=workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 1
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )

    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Merge the two approaches.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=MergeResolvingImplementationManager(),
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
        ["git", "-C", str(workspace), "rev-list", "--parents", "-n", "1", merge_commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert len(parent_line.split()) == 3
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"


def test_orchestrator_processes_multiple_slots_concurrently(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 2
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_root = config.proposals_dir
    proposal_one = proposal_root / "proposal-1"
    proposal_two = proposal_root / "proposal-2"
    proposal_one.mkdir(parents=True)
    proposal_two.mkdir(parents=True)
    (proposal_one / "plan.md").write_text("Implement the first change.\n", encoding="utf-8")
    (proposal_two / "plan.md").write_text("Implement the second change.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    execution_manager = SlowImplementationManager(delay_sec=0.2)
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
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        target_condition: "test_pass_rate >= 1.0"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_root = config.proposals_dir
    for index, slug in enumerate(("first", "second"), start=1):
        proposal_dir = proposal_root / f"proposal-{index}"
        proposal_dir.mkdir(parents=True)
        (proposal_dir / "plan.md").write_text(f"Implement {slug}.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    second_row = database_manager.get_proposal_row(second_id)
    assert second_row is not None
    assert second_row["status"] == "ready"


def test_orchestrator_drains_in_flight_trial_after_stop_request(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_root = config.proposals_dir
    for index, slug in enumerate(("first", "second"), start=1):
        proposal_dir = proposal_root / f"proposal-{index}"
        proposal_dir.mkdir(parents=True)
        (proposal_dir / "plan.md").write_text(f"Implement {slug}.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    execution_manager = InterruptibleImplementationManager(delay_sec=0.2)
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
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(execution_success=False),
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

    orchestrator.git_manager.require_clean_status = fail_require_clean

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
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        plan_command: "planner"
        max_trials: 1
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the delayed change.\n", encoding="utf-8")
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()

    planner = FakePlannerSession()
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(),
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


def test_orchestrator_remaps_absolute_proposal_paths_to_experiment_root(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 1
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
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
        artifacts_uri=f"/host/workspace/.eden/proposals/{proposal_dir.name}",
        status=ProposalStatus.READY,
    )

    planner = FakePlannerSession()
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(),
        planner_session=planner,
    )

    processed = orchestrator.run()

    assert processed == 1
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"


def test_orchestrator_completes_invalid_proposal_without_trial(tmp_path: Path) -> None:
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    invalid_docs = config.proposals_dir / "proposal-1"
    invalid_docs.mkdir(parents=True)
    (invalid_docs / "plan.md").write_text("Broken proposal.\n", encoding="utf-8")
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
    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(),
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
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 1
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
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

    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(),
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
    experiment_root, workspace = _init_experiment(tmp_path)
    head_sha = _head_sha(workspace)

    config_path = _write_config(
        experiment_root,
        """
        parallel_trials: 1
        evaluate_command: "./evaluate.sh"
        implement_command: "echo noop"
        max_trials: 5
        max_wall_time: "1h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """,
    )
    config = load_config(config_path)
    invalid_docs = config.proposals_dir / "proposal-1"
    invalid_docs.mkdir(parents=True)
    (invalid_docs / "plan.md").write_text("Broken proposal.\n", encoding="utf-8")
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

    logger = configure_logging(experiment_root / ".eden" / "session.log")
    orchestrator = Orchestrator(
        config,
        database_manager,
        logger,
        git_manager=GitManager(config.workspace_root),
        execution_manager=FakeImplementationManager(),
        planner_session=RaisingPlannerSession(),
    )

    processed = orchestrator.run()

    assert processed == 0
    proposal_row = database_manager.get_proposal_row(proposal_id)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"
