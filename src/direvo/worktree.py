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
        _chmod_if_present(worktree_path, 0o700)


def secure_worktree_git_metadata(workspace_root: Path, slot: int, user: str) -> None:
    """Grant a trial user read access to shared git metadata plus its own worktree gitdir."""
    if os.geteuid() != 0:
        return
    if not _user_exists(user):
        return

    git_root = workspace_root / ".git"
    if not git_root.exists():
        return

    worktrees_root = git_root / "worktrees"
    _grant_shared_git_read_access(git_root, worktrees_root)
    if worktrees_root.exists():
        worktrees_root.chmod(0o711)

    gitdir = worktrees_root / f"wt-{slot}"
    if chown_recursive(gitdir, user):
        _set_tree_mode(gitdir, directory_mode=0o700, file_mode=0o600)


def _user_exists(user: str) -> bool:
    """Return whether a system user exists."""
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


def _grant_shared_git_read_access(git_root: Path, worktrees_root: Path) -> None:
    """Expose shared git metadata while keeping per-worktree gitdirs private."""
    if not git_root.exists():
        return
    _chmod_if_present(git_root, 0o755)
    for root, directories, files in os.walk(git_root):
        root_path = Path(root)
        if root_path == worktrees_root or worktrees_root in root_path.parents:
            directories[:] = []
            continue
        directories[:] = [name for name in directories if root_path / name != worktrees_root]
        if root_path != git_root:
            _chmod_if_present(root_path, 0o755)
        for filename in files:
            _chmod_if_present(root_path / filename, 0o644)


def _set_tree_mode(path: Path, *, directory_mode: int, file_mode: int) -> None:
    """Apply modes recursively to a tree."""
    if not path.exists():
        return
    _chmod_if_present(path, directory_mode)
    for root, directories, files in os.walk(path):
        root_path = Path(root)
        if root_path != path:
            _chmod_if_present(root_path, directory_mode)
        for directory in directories:
            _chmod_if_present(root_path / directory, directory_mode)
        for filename in files:
            _chmod_if_present(root_path / filename, file_mode)


def _chmod_if_present(path: Path, mode: int) -> None:
    """Apply chmod unless the path disappeared during a concurrent git update."""
    try:
        path.chmod(mode)
    except FileNotFoundError:
        return
