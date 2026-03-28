"""Filesystem helpers for worktree layout."""

from __future__ import annotations

import os
import pwd
import shutil
from pathlib import Path


def ensure_trial_directories(proposals_dir: Path, artifacts_dir: Path) -> None:
    """Ensure the planner proposal and artifact directories exist."""
    proposals_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)


def ensure_worktree_trial_dir(worktree_path: Path) -> Path:
    """Ensure the trial docs directory exists in a worktree."""
    trial_dir = worktree_path / ".direvo" / "trial"
    trial_dir.mkdir(parents=True, exist_ok=True)
    return trial_dir


def clean_trial_docs(worktree_path: Path) -> Path:
    """Delete inherited trial docs and return the recreated directory."""
    trial_dir = worktree_path / ".direvo" / "trial"
    if trial_dir.exists():
        shutil.rmtree(trial_dir)
    trial_dir.mkdir(parents=True, exist_ok=True)
    return trial_dir


def copy_tree_contents(source_dir: Path, dest_dir: Path) -> None:
    """Copy directory contents into a destination directory."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        destination = dest_dir / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def copy_trial_docs_to_artifacts(trial_docs_dir: Path, artifacts_dir: Path) -> None:
    """Copy committed trial docs to the artifact destination."""
    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)
    shutil.copytree(trial_docs_dir, artifacts_dir)


def chown_recursive(path: Path, user: str) -> bool:
    """Best-effort chown for production runs.

    The change is skipped when the current process is not root, which keeps
    local tests portable.
    """
    if os.geteuid() != 0:
        return False
    if not _user_exists(user):
        return False
    try:
        shutil.chown(path, user=user)
        for root, directories, files in os.walk(path):
            for name in directories:
                shutil.chown(Path(root) / name, user=user)
            for name in files:
                shutil.chown(Path(root) / name, user=user)
    except (LookupError, PermissionError):
        return False
    return True


def secure_worktree_root(worktree_path: Path, user: str) -> None:
    """Assign a worktree to a trial user and restrict traversal to that user."""
    if os.geteuid() != 0:
        return
    if chown_recursive(worktree_path, user):
        worktree_path.chmod(0o700)


def _user_exists(user: str) -> bool:
    """Return whether a system user exists."""
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True
