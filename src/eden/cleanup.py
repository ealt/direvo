"""Post-run cleanup: restore an experiment directory to pre-run state."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .git_manager import GitManager
from .models import SessionConfig


@dataclass(frozen=True)
class CleanupResult:
    """Summary of a hard-reset cleanup run."""

    worktrees_removed: int
    branches_deleted: tuple[str, ...]
    sqlite_files_removed: int
    session_log_cleared: bool
    proposal_items_removed: int
    artifact_items_removed: int
    planner_symlinks_removed: int
    grants_removed: int


def _unlink_sqlite_cluster(path: Path) -> int:
    """Remove a SQLite database file and common journal sidecars."""
    removed = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        name = path.name + suffix
        candidate = path.parent / name
        if candidate.is_file():
            candidate.unlink()
            removed += 1
    return removed


def _remove_path_children(directory: Path) -> int:
    """Delete every file, symlink, and subdirectory directly under ``directory``."""
    if not directory.is_dir():
        return 0
    count = 0
    for child in list(directory.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        else:
            shutil.rmtree(child, ignore_errors=True)
        count += 1
    return count


def _remove_bootstrap_planner_symlinks(config: SessionConfig) -> int:
    """Remove ``results.db`` and ``artifacts`` symlinks under ``planner_root/.eden``."""
    planner_eden = config.planner_root / ".eden"
    removed = 0
    for name in ("results.db", "artifacts"):
        link = planner_eden / name
        if link.is_symlink():
            link.unlink()
            removed += 1
    return removed


def _remove_persistent_planner_grants(config: SessionConfig) -> int:
    """Remove planner grant symlinks created at bootstrap."""
    removed = 0
    for grant in config.file_permissions:
        if grant.actor != "planner":
            continue
        target = config.planner_root / grant.path
        if target.is_symlink():
            target.unlink()
            removed += 1
    return removed


def _clear_session_log(experiment_root: Path) -> bool:
    path = experiment_root / ".eden" / "session.log"
    if not path.exists():
        return False
    path.write_text("", encoding="utf-8")
    return True


def hard_reset_experiment_state(config: SessionConfig) -> CleanupResult:
    """Remove git worktrees, DBs, logs, proposals, artifacts, and planner symlinks.

    Leaves ``.eden/config.yaml`` and the workspace git history unchanged. Run
    order: worktrees and branches first, then planner-facing symlinks, then
    files under the experiment and planner trees.
    """
    git = GitManager(config.workspace_root)

    worktrees_removed = git.remove_all_eden_worktrees()
    branches_deleted = tuple(git.delete_local_branches_matching("refs/heads/trial/*"))

    planner_symlinks_removed = _remove_bootstrap_planner_symlinks(config)
    grants_removed = _remove_persistent_planner_grants(config)

    sqlite_files_removed = 0
    sqlite_files_removed += _unlink_sqlite_cluster(config.results_db.resolve())
    sqlite_files_removed += _unlink_sqlite_cluster(config.proposals_db.resolve())

    proposal_items_removed = _remove_path_children(config.proposals_dir.resolve())
    artifact_items_removed = _remove_path_children(config.artifacts_dir.resolve())

    session_log_cleared = _clear_session_log(config.experiment_root)

    return CleanupResult(
        worktrees_removed=worktrees_removed,
        branches_deleted=branches_deleted,
        sqlite_files_removed=sqlite_files_removed,
        session_log_cleared=session_log_cleared,
        proposal_items_removed=proposal_items_removed,
        artifact_items_removed=artifact_items_removed,
        planner_symlinks_removed=planner_symlinks_removed,
        grants_removed=grants_removed,
    )
