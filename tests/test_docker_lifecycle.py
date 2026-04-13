"""Integration tests simulating the Docker build → run lifecycle.

These tests exercise the full trial pipeline WITHOUT Docker, catching
bugs that only manifest when an experiment directory is copied into an
image (symlinks flattened, stale databases preserved, git repos
re-initialized).

Run with:  uv run -m pytest tests/test_docker_lifecycle.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

from eden.config import load_config
from eden.db import DatabaseManager
from eden.execution import EvaluationResult, ImplementationManager, ImplementationResult
from eden.models import ProposalStatus
from eden.orchestrator import Orchestrator
from eden.planner import PlannerSession

# ---------------------------------------------------------------------------
# Test doubles (same patterns as test_orchestrator.py)
# ---------------------------------------------------------------------------


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


class FakeImplementationManager(ImplementationManager):
    def __init__(self) -> None:
        super().__init__(implement_command="echo noop")

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
        (worktree_path / "code.txt").write_text(f"trial-{trial_id}\n", encoding="utf-8")
        trial_dir = worktree_path / ".eden" / "trial"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "implementation.md").write_text(slug, encoding="utf-8")
        return ImplementationResult(success=True, stdout="", stderr="", returncode=0)

    def run_evaluation(
        self, *, worktree_path: Path, evaluate_command: str, timeout_sec: int, user: str | None = None
    ) -> EvaluationResult:
        trial_dir = worktree_path / ".eden" / "trial"
        trial_dir.mkdir(parents=True, exist_ok=True)
        return EvaluationResult(
            success=True,
            stdout='{"score": 1.0}',
            stderr="",
            returncode=0,
            metrics={"score": 1.0},
        )


class EvalCrashImplementationManager(FakeImplementationManager):
    """Implementation succeeds but evaluation raises, triggering _recover_trial after commit."""

    def run_evaluation(
        self, *, worktree_path: Path, evaluate_command: str, timeout_sec: int, user: str | None = None
    ) -> EvaluationResult:
        raise RuntimeError("simulated eval crash")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_experiment(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal experiment with a git-initialized workspace."""
    experiment_root = tmp_path / "experiment"
    planner_root = experiment_root / "planner"
    workspace = planner_root / "workspace"
    (experiment_root / ".eden").mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")

    eval_script = experiment_root / "evaluate.sh"
    eval_script.write_text('#!/bin/sh\necho \'{"score": 1.0}\'\n', encoding="utf-8")
    eval_script.chmod(0o755)

    _run_git(["init"], cwd=workspace)
    _run_git(["config", "user.email", "test@test.local"], cwd=workspace)
    _run_git(["config", "user.name", "Test"], cwd=workspace)
    _run_git(["add", "."], cwd=workspace)
    _run_git(["commit", "-m", "initial"], cwd=workspace)
    return experiment_root, workspace


def _write_config(experiment_root: Path, *, parallel_trials: int = 1, max_trials: int = 3) -> Path:
    config_path = experiment_root / ".eden" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(f"""\
            planner_root: "./planner"
            workspace: "./workspace"
            parallel_trials: {parallel_trials}
            evaluate_command: "./evaluate.sh"
            implement_command: "echo noop"
            max_trials: {max_trials}
            max_wall_time: "1h"
            objective:
              expr: "score"
              direction: "maximize"
            metrics_schema:
              score: real
        """),
        encoding="utf-8",
    )
    return config_path


def _head_sha(workspace: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=workspace)


def _bootstrap_and_run(config_path: Path, *, max_trials: int) -> tuple[DatabaseManager, int]:
    """Bootstrap an experiment and run the orchestrator with fakes."""
    from eden.bootstrap import bootstrap

    result = bootstrap(str(config_path), progress=False)
    config = result.config
    db = result.database_manager
    orchestrator = Orchestrator(
        config,
        db,
        result.logger,
        execution_manager=FakeImplementationManager(),
        planner_session=FakePlannerSession(),
    )
    total = orchestrator.run()
    return db, total


def _seed_proposals(db: DatabaseManager, head_sha: str, *, count: int = 3) -> None:
    """Insert ready proposals referencing the given parent commit."""
    for i in range(count):
        proposal_dir = db.proposals_db.parent / "proposals" / f"p-{i}"
        proposal_dir.mkdir(parents=True, exist_ok=True)
        (proposal_dir / "plan.md").write_text(f"Plan {i}\n", encoding="utf-8")
        db.create_proposal(
            priority=float(i),
            slug=f"test-{i}",
            parent_commits=[head_sha],
            artifacts_uri=str(proposal_dir),
            status=ProposalStatus.READY,
        )


def _simulate_docker_copy(src: Path, dst: Path) -> None:
    """Copy experiment tree the way Docker COPY does: follow symlinks."""
    shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDockerContextCleanup:
    """Verify that _clean_docker_context removes stale runtime state."""

    def test_stale_databases_removed_from_docker_context(self, tmp_path: Path) -> None:
        experiment_root, workspace = _init_experiment(tmp_path)
        config_path = _write_config(experiment_root)
        config = load_config(config_path)
        head = _head_sha(workspace)

        # Run a session so databases and symlinks exist.
        from eden.bootstrap import bootstrap

        result = bootstrap(str(config_path), progress=False)
        db = result.database_manager
        _seed_proposals(db, head)
        assert config.results_db.exists()
        assert config.proposals_db.exists()

        # Simulate Docker build: copy and clean.
        docker_experiment = tmp_path / "docker-experiment"
        _simulate_docker_copy(experiment_root, docker_experiment)

        from eden.docker_runner import _clean_docker_context

        docker_config = load_config(docker_experiment / ".eden" / "config.yaml")
        _clean_docker_context(docker_experiment, docker_config)

        # Databases and stale state should be gone.
        assert not (docker_experiment / ".eden" / "results.db").exists()
        planner_eden = docker_experiment / "planner" / ".eden"
        assert not planner_eden.exists()
        assert not (docker_experiment / "planner" / "workspace" / "worktrees").exists()

        # Config must survive.
        assert (docker_experiment / ".eden" / "config.yaml").exists()

    def test_stale_worktrees_removed_from_docker_context(self, tmp_path: Path) -> None:
        experiment_root, workspace = _init_experiment(tmp_path)
        _write_config(experiment_root)

        # Create a stale worktree.
        worktree_dir = workspace / "worktrees" / "wt-0"
        worktree_dir.mkdir(parents=True)
        (worktree_dir / "code.txt").write_text("stale\n", encoding="utf-8")

        docker_experiment = tmp_path / "docker-experiment"
        _simulate_docker_copy(experiment_root, docker_experiment)

        from eden.docker_runner import _clean_docker_context

        docker_config = load_config(docker_experiment / ".eden" / "config.yaml")
        _clean_docker_context(docker_experiment, docker_config)

        assert not (docker_experiment / "planner" / "workspace" / "worktrees").exists()


class TestInvalidParentCommit:
    """Orchestrator rejects proposals with nonexistent parent commits."""

    def test_proposal_with_fabricated_sha_is_rejected(self, tmp_path: Path) -> None:
        experiment_root, workspace = _init_experiment(tmp_path)
        config_path = _write_config(experiment_root, max_trials=1)

        from eden.bootstrap import bootstrap

        result = bootstrap(str(config_path), progress=False)
        db = result.database_manager

        # Create proposal referencing a SHA that doesn't exist.
        fake_sha = "deadbeef" * 5
        proposal_dir = result.config.proposals_dir / "bad-proposal"
        proposal_dir.mkdir(parents=True, exist_ok=True)
        (proposal_dir / "plan.md").write_text("Bad plan\n", encoding="utf-8")
        db.create_proposal(
            priority=1.0,
            slug="bad-parent",
            parent_commits=[fake_sha],
            artifacts_uri=str(proposal_dir),
            status=ProposalStatus.READY,
        )

        planner = FakePlannerSession()
        orchestrator = Orchestrator(
            result.config,
            db,
            result.logger,
            execution_manager=FakeImplementationManager(),
            planner_session=planner,
        )
        orchestrator.run()

        # Invalid proposals are rejected at validation — no trial row is
        # created, the proposal is marked completed, and the planner is
        # notified of the error.
        proposal_row = db.get_proposal_row(1)
        assert proposal_row is not None
        assert proposal_row["status"] == ProposalStatus.COMPLETED.value
        assert any("parent commit not found" in e for e in planner.errors)

    def test_valid_parent_commit_succeeds(self, tmp_path: Path) -> None:
        experiment_root, workspace = _init_experiment(tmp_path)
        config_path = _write_config(experiment_root, max_trials=1)

        from eden.bootstrap import bootstrap

        result = bootstrap(str(config_path), progress=False)
        db = result.database_manager
        head = _head_sha(workspace)
        _seed_proposals(db, head, count=1)

        orchestrator = Orchestrator(
            result.config,
            db,
            result.logger,
            execution_manager=FakeImplementationManager(),
            planner_session=FakePlannerSession(),
        )
        orchestrator.run()

        trial_row = db.get_trial_row(1)
        assert trial_row is not None
        assert trial_row["status"] == "success"


class TestRecoveryAfterCommittedTrial:
    """Recovery must not fail when .eden/trial/ files were already committed."""

    def test_recovery_after_eval_crash_does_not_fail_on_clean_status(self, tmp_path: Path) -> None:
        """Reproduce: implementation commits .eden/trial/notes.md and plan.md,
        then evaluation crashes.  _recover_trial must leave the worktree clean.

        Before the fix, clean_trial_docs deleted committed files, causing
        require_clean_status to fail with:
            D .eden/trial/notes.md
             D .eden/trial/plan.md
        """
        experiment_root, workspace = _init_experiment(tmp_path)
        config_path = _write_config(experiment_root, max_trials=1)

        from eden.bootstrap import bootstrap

        result = bootstrap(str(config_path), progress=False)
        db = result.database_manager
        head = _head_sha(workspace)
        _seed_proposals(db, head, count=1)

        orchestrator = Orchestrator(
            result.config,
            db,
            result.logger,
            execution_manager=EvalCrashImplementationManager(),
            planner_session=FakePlannerSession(),
        )
        # This used to raise RuntimeError("Worktree is not clean after recovery")
        orchestrator.run()

        trial = db.get_trial_row(1)
        assert trial is not None
        assert trial["status"] == "error"


class TestFullDockerLifecycle:
    """Simulate local run → Docker build → Docker run end-to-end."""

    def test_experiment_succeeds_after_simulated_docker_build(self, tmp_path: Path) -> None:
        """Run locally, copy experiment (flattening symlinks), run again.

        This is the core integration test — it catches:
        - Stale databases with host SHAs leaking into Docker
        - Flattened symlinks blocking bootstrap
        - Invalid parent commits from stale proposals
        """
        # --- Phase 1: local run ---
        experiment_root, workspace = _init_experiment(tmp_path)
        config_path = _write_config(experiment_root, max_trials=2)
        head = _head_sha(workspace)

        from eden.bootstrap import bootstrap

        local_result = bootstrap(str(config_path), progress=False)
        _seed_proposals(local_result.database_manager, head, count=2)

        local_orchestrator = Orchestrator(
            local_result.config,
            local_result.database_manager,
            local_result.logger,
            execution_manager=FakeImplementationManager(),
            planner_session=FakePlannerSession(),
        )
        local_total = local_orchestrator.run()
        assert local_total == 2

        # Verify local state accumulated.
        assert local_result.config.results_db.exists()
        assert local_result.config.proposals_db.exists()
        planner_symlink = local_result.config.planner_root / ".eden" / "results.db"
        assert planner_symlink.is_symlink()

        # --- Phase 2: simulate Docker build ---
        docker_experiment = tmp_path / "docker-experiment"
        _simulate_docker_copy(experiment_root, docker_experiment)

        # Symlinks should be flattened (this is what Docker COPY does).
        docker_planner_results = docker_experiment / "planner" / ".eden" / "results.db"
        if docker_planner_results.exists():
            assert not docker_planner_results.is_symlink(), "copytree with symlinks=False should flatten"

        # Clean context (what build_image now does).
        from eden.docker_runner import _clean_docker_context

        docker_config = load_config(docker_experiment / ".eden" / "config.yaml")
        _clean_docker_context(docker_experiment, docker_config)

        # Re-initialize git (what the Dockerfile does when .git doesn't exist).
        docker_workspace = docker_experiment / "planner" / "workspace"
        shutil.rmtree(docker_workspace / ".git")
        _run_git(["init"], cwd=docker_workspace)
        _run_git(["config", "user.email", "eden@experiment"], cwd=docker_workspace)
        _run_git(["config", "user.name", "eden"], cwd=docker_workspace)
        _run_git(["add", "."], cwd=docker_workspace)
        _run_git(["commit", "-m", "initial baseline"], cwd=docker_workspace)

        # --- Phase 3: simulate Docker run ---
        docker_config_path = docker_experiment / ".eden" / "config.yaml"
        docker_head = _head_sha(docker_workspace)

        docker_result = bootstrap(str(docker_config_path), progress=False)
        docker_db = docker_result.database_manager

        # Seed fresh proposals with the Docker workspace HEAD.
        _seed_proposals(docker_db, docker_head, count=2)

        docker_orchestrator = Orchestrator(
            docker_result.config,
            docker_db,
            docker_result.logger,
            execution_manager=FakeImplementationManager(),
            planner_session=FakePlannerSession(),
        )
        docker_total = docker_orchestrator.run()

        # All trials should succeed — no stale SHAs, no symlink errors.
        assert docker_total == 2
        trials = docker_db.list_trials()
        assert all(t["status"] == "success" for t in trials), (
            f"Expected all trials to succeed, got: {[(t['trial_id'], t['status']) for t in trials]}"
        )
        # Trial IDs should start at 1 (no stale results.db rows).
        assert trials[0]["trial_id"] == 1

    def test_stale_proposals_with_host_shas_do_not_leak(self, tmp_path: Path) -> None:
        """Without cleanup, stale proposals would reference invalid SHAs."""
        experiment_root, workspace = _init_experiment(tmp_path)
        config_path = _write_config(experiment_root, max_trials=1)
        local_head = _head_sha(workspace)

        from eden.bootstrap import bootstrap

        local_result = bootstrap(str(config_path), progress=False)
        _seed_proposals(local_result.database_manager, local_head, count=1)

        # Copy WITHOUT cleanup (simulating old broken behavior).
        docker_experiment = tmp_path / "docker-experiment"
        _simulate_docker_copy(experiment_root, docker_experiment)

        # Re-init git so local SHAs are invalid.
        docker_workspace = docker_experiment / "planner" / "workspace"
        shutil.rmtree(docker_workspace / ".git")
        _run_git(["init"], cwd=docker_workspace)
        _run_git(["config", "user.email", "eden@experiment"], cwd=docker_workspace)
        _run_git(["config", "user.name", "eden"], cwd=docker_workspace)
        _run_git(["add", "."], cwd=docker_workspace)
        _run_git(["commit", "-m", "initial baseline"], cwd=docker_workspace)

        # Bootstrap the Docker experiment (stale proposals.db still has local SHAs).
        docker_config_path = docker_experiment / ".eden" / "config.yaml"
        # Fix flattened symlinks so bootstrap doesn't crash.
        planner_eden = docker_experiment / "planner" / ".eden"
        for stale in ("results.db", "artifacts"):
            p = planner_eden / stale
            if p.exists() and not p.is_symlink():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()

        docker_result = bootstrap(str(docker_config_path), progress=False)
        docker_db = docker_result.database_manager

        # The stale proposal from the local run should still be in the DB.
        stale_proposal = docker_db.get_proposal_row(1)
        assert stale_proposal is not None

        # Run the orchestrator — the stale proposal should fail validation
        # (parent commit doesn't exist) but not crash the whole session.
        planner = FakePlannerSession()
        docker_orchestrator = Orchestrator(
            docker_result.config,
            docker_db,
            docker_result.logger,
            execution_manager=FakeImplementationManager(),
            planner_session=planner,
        )
        docker_orchestrator.run()

        # Stale proposal rejected at validation (no trial created),
        # marked completed so it's not retried.
        proposal = docker_db.get_proposal_row(1)
        assert proposal is not None
        assert proposal["status"] == ProposalStatus.COMPLETED.value
        assert any("parent commit not found" in e for e in planner.errors)
