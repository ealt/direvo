"""Orchestrator dispatch loop and trial execution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from .bootstrap import BootstrapResult, bootstrap  # noqa: F401 -- public re-export
from .db import DatabaseManager
from .execution import ImplementationManager
from .git_manager import GitManager
from .grants import create_grant_symlinks, remove_grant_symlinks
from .logging import log_event
from .models import (
    ProposalClaim,
    ProposalStatus,
    SessionConfig,
    TrialPaths,
    TrialStatus,
    TrialUpdate,
    ValidatedProposal,
)
from .planner import PlannerSession, create_planner_session
from .termination import should_terminate
from .worktree import (
    clean_trial_docs,
    copy_tree_contents,
    copy_trial_docs_to_artifacts,
    secure_worktree_git_metadata,
    secure_worktree_root,
)


class Orchestrator:
    """Coordinate trials for a session."""

    _SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(
        self,
        config: SessionConfig,
        database_manager: DatabaseManager,
        logger: logging.Logger,
        *,
        git_manager: GitManager | None = None,
        execution_manager: ImplementationManager | None = None,
        planner_session: PlannerSession | None = None,
        idle_poll_interval_sec: float = 1.0,
    ) -> None:
        self.config = config
        self.database_manager = database_manager
        self.logger = logger
        self.git_manager = git_manager or GitManager(config.workspace_root)
        self.execution_manager = execution_manager or ImplementationManager(implement_command=config.implement_command)
        self.planner_session = planner_session or create_planner_session(
            command=config.plan_command,
            planner_root=config.planner_root,
            notify_template=config.plan_notify_template,
            startup_timeout_sec=config.plan_start_timeout_sec,
        )
        self.idle_poll_interval_sec = idle_poll_interval_sec
        self._stop_requested = threading.Event()
        self.session_trial_ids: list[int] = []
        self.last_termination_reason: str | None = None
        self.wall_time_seconds = 0.0

    def request_stop(self) -> None:
        """Stop dispatching new work and let in-flight trials drain."""
        if self._stop_requested.is_set():
            return
        self._stop_requested.set()
        log_event(self.logger, "session_stop_requested", reason="user_interrupt")

    def run(self) -> int:
        """Process ready proposals until the queue is empty or limits are reached."""
        restore_signal_handlers = self._install_signal_handlers()
        try:
            return asyncio.run(self._run_async())
        finally:
            restore_signal_handlers()

    def _install_signal_handlers(self) -> Callable[[], None]:
        """Install graceful signal handlers when running on the main thread."""
        if threading.current_thread() is not threading.main_thread():
            return lambda: None

        previous_handlers: dict[int, signal.Handlers | int | Callable[[int, FrameType | None], object] | None] = {}

        def handle_signal(signum: int, _frame: object) -> None:
            log_event(self.logger, "signal_received", signal=signum)
            self.request_stop()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)

        def restore() -> None:
            for signum, previous_handler in previous_handlers.items():
                signal.signal(signum, previous_handler)

        return restore

    async def _run_async(self) -> int:
        """Run slot workers concurrently."""
        claimed_count = 0
        attempted_proposals: set[int] = set()
        coordination_lock = asyncio.Lock()
        termination_reason: str | None = None
        session_started_at = time.monotonic()
        self.planner_session.start()
        try:
            for slot in range(self.config.parallel_trials):
                worktree_path = self.git_manager.initialize_worktree(slot)
                secure_worktree_root(worktree_path, f"trial-{slot}")
                secure_worktree_git_metadata(self.config.workspace_root, slot, f"trial-{slot}")

            async def worker(slot: int) -> None:
                nonlocal claimed_count, termination_reason
                while True:
                    async with coordination_lock:
                        if termination_reason is not None:
                            return
                        if self._stop_requested.is_set():
                            termination_reason = "user_interrupt"
                            return
                        decision = should_terminate(
                            claimed_count=claimed_count,
                            max_trials=self.config.max_trials,
                            elapsed_seconds=time.monotonic() - session_started_at,
                            max_wall_time_seconds=self.config.max_wall_time_seconds,
                            database_manager=self.database_manager,
                            objective_expr=self.config.objective.expr,
                            objective_direction=self.config.objective.direction,
                            convergence_window=self.config.convergence_window,
                            target_condition=self.config.target_condition,
                        )
                        if decision.should_stop:
                            termination_reason = decision.reason
                            return
                        proposal = self.database_manager.claim_ready_proposal()
                        if proposal is None:
                            if self.config.plan_command is None:
                                if termination_reason is None:
                                    termination_reason = "queue_empty"
                                return
                            proposal = None
                        else:
                            if proposal.proposal_id in attempted_proposals:
                                self.database_manager.update_proposal_status(proposal.proposal_id, ProposalStatus.READY)
                                if termination_reason is None:
                                    termination_reason = "queue_repeat"
                                return
                            attempted_proposals.add(proposal.proposal_id)
                            claimed_count += 1
                    if proposal is None:
                        await asyncio.sleep(self.idle_poll_interval_sec)
                        continue
                    validated_proposal = self._validate_claimed_proposal(proposal)
                    if validated_proposal is None:
                        async with coordination_lock:
                            claimed_count -= 1
                        continue
                    await asyncio.to_thread(self._run_claimed_trial, slot=slot, proposal=validated_proposal)

            await asyncio.gather(*(worker(slot) for slot in range(self.config.parallel_trials)))
        finally:
            self.planner_session.stop()
            for slot in range(self.config.parallel_trials):
                self.git_manager.remove_worktree(slot)
            self.wall_time_seconds = time.monotonic() - session_started_at
            self.last_termination_reason = termination_reason or "shutdown"
            log_event(
                self.logger,
                "session_ended",
                total_trials=claimed_count,
                reason=self.last_termination_reason,
                wall_time_seconds=self.wall_time_seconds,
            )
        return claimed_count

    def _validate_claimed_proposal(self, proposal: ProposalClaim) -> ValidatedProposal | None:
        """Validate a claimed proposal before reserving a trial id."""
        try:
            return self._validated_proposal(proposal)
        except RuntimeError as exc:
            error = str(exc)
        self.database_manager.update_proposal_status(proposal.proposal_id, ProposalStatus.COMPLETED)
        msg = f"Invalid proposal {proposal.proposal_id}: {error}"
        self._notify_planner_error(msg, proposal_id=proposal.proposal_id)
        log_event(
            self.logger,
            "proposal_invalid",
            proposal_id=proposal.proposal_id,
            error=error,
        )
        return None

    def _validated_proposal(self, proposal: ProposalClaim) -> ValidatedProposal:
        """Return a proposal with parsed and validated fields."""
        if not self._SLUG_RE.match(proposal.slug):
            raise RuntimeError(f"invalid slug: {proposal.slug!r}")

        try:
            parent_commits = json.loads(proposal.parent_commits)
        except json.JSONDecodeError:
            raise RuntimeError("invalid parent_commits JSON") from None

        if not isinstance(parent_commits, list) or not parent_commits:
            raise RuntimeError("parent_commits must be a non-empty JSON list")
        if len(parent_commits) > 2:
            raise RuntimeError("only up to two parent commits are supported")
        if not all(isinstance(commit, str) and commit.strip() for commit in parent_commits):
            raise RuntimeError("parent_commits entries must be non-empty strings")

        for commit in parent_commits:
            if not self.git_manager.commit_exists(commit.strip()):
                raise RuntimeError(f"parent commit not found in workspace: {commit.strip()[:12]}")

        proposal_docs_path = self._resolve_proposal_docs_path(proposal.artifacts_uri)
        if not proposal_docs_path.exists():
            raise RuntimeError(f"proposal docs not found: {proposal_docs_path}")
        if not proposal_docs_path.is_dir():
            raise RuntimeError(f"proposal docs path is not a directory: {proposal_docs_path}")
        return ValidatedProposal(
            proposal_id=proposal.proposal_id,
            priority=proposal.priority,
            slug=proposal.slug,
            parent_commits=[commit.strip() for commit in parent_commits],
            artifacts_path=proposal_docs_path,
        )

    def _resolve_proposal_docs_path(self, artifacts_uri: str) -> Path:
        """Resolve proposal docs paths across host/container workspace roots."""
        path = Path(artifacts_uri)
        if not path.is_absolute():
            return self.config.planner_root / path
        if path.exists():
            return path

        try:
            proposals_relative = self.config.proposals_dir.relative_to(self.config.planner_root)
        except ValueError:
            return path

        start_index = self._find_path_sequence(path.parts, proposals_relative.parts)
        if start_index is None:
            return path
        relative_suffix = Path(*path.parts[start_index:])
        return self.config.planner_root / relative_suffix

    @staticmethod
    def _find_path_sequence(parts: tuple[str, ...], sequence: tuple[str, ...]) -> int | None:
        """Return the start index of a path-part sequence when present."""
        if not sequence or len(sequence) > len(parts):
            return None
        limit = len(parts) - len(sequence) + 1
        for index in range(limit):
            if parts[index : index + len(sequence)] == sequence:
                return index
        return None

    def _run_claimed_trial(self, *, slot: int, proposal: ValidatedProposal) -> None:
        trial_id = self.database_manager.reserve_trial_id()
        self.session_trial_ids.append(trial_id)
        branch_name = f"trial/{trial_id}-{proposal.slug}"
        description = f"trial/{trial_id}-{proposal.slug}"
        log_event(
            self.logger,
            "trial_id_reserved",
            proposal_id=proposal.proposal_id,
            slot=slot,
            trial_id=trial_id,
            branch=branch_name,
        )
        paths = self._prepare_trial(slot=slot, trial_id=trial_id, proposal=proposal, branch_name=branch_name)
        log_event(
            self.logger,
            "proposal_claimed",
            proposal_id=proposal.proposal_id,
            slot=slot,
            trial_id=trial_id,
            branch=branch_name,
        )
        log_event(
            self.logger,
            "trial_started",
            proposal_id=proposal.proposal_id,
            slot=slot,
            trial_id=trial_id,
            branch=branch_name,
        )
        created_grants: list[Path] = []
        try:
            created_grants = create_grant_symlinks(
                self.config.file_permissions,
                actor="implementer",
                source_root=self.config.experiment_root,
                target_root=paths.worktree_path,
                skip_existing=True,
            )
            implementation_result = self.execution_manager.run_implementation(
                worktree_path=paths.worktree_path,
                slot=slot,
                timeout_sec=self.config.implement_timeout_sec,
                user=f"trial-{slot}",
                slug=proposal.slug,
                trial_id=trial_id,
            )
            remove_grant_symlinks(created_grants, target_root=paths.worktree_path)
            created_grants = []
            log_event(
                self.logger,
                "implementation_complete",
                trial_id=trial_id,
                slot=slot,
                exit_code=implementation_result.returncode,
                reason=implementation_result.reason,
                stdout=implementation_result.stdout.strip(),
                stderr=implementation_result.stderr.strip(),
            )
            if not implementation_result.success:
                self._recover_trial(
                    slot=slot,
                    trial_id=trial_id,
                    proposal=proposal,
                    branch_name=branch_name,
                    parent_commits=json.dumps(proposal.parent_commits),
                    error_message=self._command_failure_message(
                        reason=implementation_result.reason or "implementation_failed",
                        stdout=implementation_result.stdout,
                        stderr=implementation_result.stderr,
                    ),
                )
                return

            self.git_manager.commit_all(
                paths.worktree_path,
                f"{description}: completed",
            )
            commit_sha = self.git_manager.current_head_sha(paths.worktree_path)
            evaluation_result = self.execution_manager.run_evaluation(
                worktree_path=paths.worktree_path,
                evaluate_command=self.config.evaluate_command,
                timeout_sec=self.config.evaluation_timeout_sec,
                user=None,
            )
            log_event(
                self.logger,
                "evaluation_complete",
                trial_id=trial_id,
                slot=slot,
                exit_code=evaluation_result.returncode,
                metrics=evaluation_result.metrics,
                reason=evaluation_result.reason,
                stdout=evaluation_result.stdout.strip(),
                stderr=evaluation_result.stderr.strip(),
            )

            copy_trial_docs_to_artifacts(paths.trial_docs_path, paths.artifacts_path)
            self._reset_evaluation_artifacts(paths.worktree_path)

            status = TrialStatus.SUCCESS if evaluation_result.success else TrialStatus.EVAL_ERROR
            self.database_manager.update_trial(
                TrialUpdate(
                    trial_id=trial_id,
                    status=status,
                    commit_sha=commit_sha,
                    parent_commits=json.dumps(proposal.parent_commits),
                    branch=branch_name,
                    artifacts_uri=str(paths.artifacts_path),
                    description=description,
                    metrics=evaluation_result.metrics if evaluation_result.success else {},
                )
            )
            self.database_manager.update_proposal_status(proposal.proposal_id, ProposalStatus.COMPLETED)
            log_event(
                self.logger,
                "trial_complete",
                trial_id=trial_id,
                slot=slot,
                commit_sha=commit_sha,
                status=status.value,
                branch=branch_name,
                metrics=evaluation_result.metrics,
            )
            self._notify_planner_trial_completed(trial_id=trial_id, proposal_id=proposal.proposal_id)
        except Exception as exc:
            if created_grants:
                remove_grant_symlinks(created_grants, target_root=paths.worktree_path)
            self._recover_trial(
                slot=slot,
                trial_id=trial_id,
                proposal=proposal,
                branch_name=branch_name,
                parent_commits=json.dumps(proposal.parent_commits),
                error_message=str(exc),
            )

    def _prepare_trial(self, *, slot: int, trial_id: int, proposal: ValidatedProposal, branch_name: str) -> TrialPaths:
        worktree_path = self.git_manager.ensure_worktree(slot)
        primary_parent = proposal.parent_commits[0]

        self.git_manager.create_branch(branch_name, primary_parent)
        self.git_manager.checkout_branch(worktree_path, branch_name)
        if len(proposal.parent_commits) == 2:
            self.git_manager.merge_no_commit(worktree_path, proposal.parent_commits[1])

        trial_docs_path = clean_trial_docs(worktree_path)
        copy_tree_contents(proposal.artifacts_path, trial_docs_path)
        secure_worktree_root(worktree_path, f"trial-{slot}")
        secure_worktree_git_metadata(self.config.workspace_root, slot, f"trial-{slot}")

        artifacts_path = self.config.artifacts_dir / f"trial-{trial_id}"
        return TrialPaths(
            worktree_path=worktree_path,
            trial_docs_path=trial_docs_path,
            artifacts_path=artifacts_path,
        )

    def _reset_evaluation_artifacts(self, worktree_path: Path) -> None:
        """Restore the committed worktree state after evaluation."""
        self.git_manager.reset_hard(worktree_path)
        self.git_manager.clean_untracked(worktree_path)
        self.git_manager.require_clean_status(worktree_path)

    def _recover_trial(
        self,
        *,
        slot: int,
        trial_id: int,
        proposal: ValidatedProposal,
        branch_name: str,
        parent_commits: str,
        error_message: str | None = None,
    ) -> None:
        worktree_path = self.git_manager.ensure_worktree(slot)
        self.git_manager.abort_in_progress_git_state(worktree_path)
        self.git_manager.reset_hard(worktree_path)
        self.git_manager.clean_untracked(worktree_path)
        self.git_manager.require_clean_status(worktree_path)
        secure_worktree_root(worktree_path, f"trial-{slot}")
        secure_worktree_git_metadata(self.config.workspace_root, slot, f"trial-{slot}")
        self.database_manager.update_trial(
            TrialUpdate(
                trial_id=trial_id,
                status=TrialStatus.ERROR,
                parent_commits=parent_commits,
                branch=branch_name,
                description=error_message or "trial execution failed",
            )
        )
        self.database_manager.update_proposal_status(
            proposal.proposal_id,
            ProposalStatus.READY,
            priority=max(0.0, proposal.priority - self.config.proposal_retry_priority_delta),
        )
        log_event(
            self.logger,
            "trial_failed",
            trial_id=trial_id,
            slot=slot,
            proposal_id=proposal.proposal_id,
            branch=branch_name,
            error=error_message or "trial execution failed",
        )

    @staticmethod
    def _command_failure_message(*, reason: str, stdout: str, stderr: str) -> str:
        """Summarize a subprocess failure for logs and persisted trial rows."""
        details = stderr.strip() or stdout.strip()
        if not details:
            return reason
        return f"{reason}: {details}"

    def _notify_planner_trial_completed(self, *, trial_id: int, proposal_id: int) -> None:
        """Notify the planner about a completed trial without affecting trial results."""
        try:
            self.planner_session.notify_trial_completed(trial_id)
        except Exception as exc:
            log_event(
                self.logger,
                "planner_notify_failed",
                trial_id=trial_id,
                proposal_id=proposal_id,
                error=str(exc),
            )
            return
        log_event(self.logger, "planner_notified", trial_id=trial_id, proposal_id=proposal_id)

    def _notify_planner_error(self, message: str, *, proposal_id: int) -> None:
        """Notify the planner about an invalid proposal without affecting queue handling."""
        try:
            self.planner_session.notify_error(message)
        except Exception as exc:
            log_event(
                self.logger,
                "planner_notify_failed",
                proposal_id=proposal_id,
                error=str(exc),
            )
