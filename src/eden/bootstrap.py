"""Session bootstrap: workspace validation, directory setup, and database initialization."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .db import DatabaseManager
from .git_manager import GitManager
from .grants import create_grant_symlinks
from .logging import configure_logging, log_event
from .models import SessionConfig
from .runtime import RuntimeSetup
from .worktree import ensure_trial_directories


@dataclass(slots=True)
class BootstrapResult:
    """Result of a successful bootstrap pass."""

    config: SessionConfig
    database_manager: DatabaseManager
    session_log_path: Path
    logger: logging.Logger


def _ensure_symlink(source: Path, target: Path) -> None:
    """Create or refresh a symlink to a known source path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return
        target.unlink()
    elif target.exists():
        raise RuntimeError(f"Cannot create symlink over existing path: {target}")
    target.symlink_to(source)


def bootstrap(config_path: str | Path, *, progress: bool = True) -> BootstrapResult:
    """Bootstrap the workspace and initialize persistent session state."""
    config = load_config(config_path)
    git = GitManager(config.workspace_root)
    if not git.is_git_repo():
        raise RuntimeError(f"Workspace is not a git repo: {config.workspace_root}")

    eden_dir = config.experiment_root / ".eden"
    planner_eden_dir = config.planner_root / ".eden"
    eden_dir.mkdir(parents=True, exist_ok=True)
    planner_eden_dir.mkdir(parents=True, exist_ok=True)
    ensure_trial_directories(config.proposals_dir, config.artifacts_dir)
    _ensure_symlink(config.results_db, planner_eden_dir / "results.db")
    _ensure_symlink(config.artifacts_dir, planner_eden_dir / "artifacts")
    create_grant_symlinks(
        config.file_permissions,
        actor="planner",
        source_root=config.experiment_root,
        target_root=config.planner_root,
    )

    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    RuntimeSetup().prepare(config)

    session_log_path = eden_dir / "session.log"
    logger = configure_logging(session_log_path, progress=progress, progress_start_time=time.monotonic())
    log_event(
        logger,
        "session_started",
        workspace_root=str(config.workspace_root),
        parallel_trials=config.parallel_trials,
        results_db=str(config.results_db),
        proposals_db=str(config.proposals_db),
    )

    return BootstrapResult(
        config=config,
        database_manager=database_manager,
        session_log_path=session_log_path,
        logger=logger,
    )
