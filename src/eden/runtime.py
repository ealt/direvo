"""Container runtime setup for users and permissions."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from .config import load_config
from .models import SessionConfig
from .worktree import secure_worktree_git_metadata


class SystemRunner:
    """Run system-level setup commands."""

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a system command and capture output."""
        return subprocess.run(command, capture_output=True, text=True, check=False)


class RuntimeSetup:
    """Prepare the container runtime layout for a session."""

    def __init__(self, runner: SystemRunner | None = None) -> None:
        self.runner = runner or SystemRunner()

    def prepare(self, config: SessionConfig) -> None:
        """Create required directories and apply runtime permissions."""
        self._ensure_directories(config)
        if os.geteuid() != 0:
            return

        self._ensure_user("planner")
        for slot in range(config.parallel_trials):
            self._ensure_user(f"trial-{slot}")

        self._ensure_runtime_dirs("planner")
        for slot in range(config.parallel_trials):
            self._ensure_runtime_dirs(f"trial-{slot}")

        self._mark_git_safe_directory(config.workspace_root)
        self._ensure_ancestor_traversal(config.planner_root)
        self._ensure_ancestor_traversal(config.workspace_root)
        self._apply_directory_permissions(config.experiment_root, user="root", group="root", mode=0o711)
        self._apply_directory_permissions(config.experiment_root / ".eden", user="root", group="root", mode=0o711)
        self._apply_directory_permissions(config.planner_root, user="planner", group="planner", mode=0o751)
        self._apply_directory_permissions(config.planner_root / ".eden", user="planner", group="planner", mode=0o750)
        self._apply_directory_permissions(config.workspace_root, user="root", group="planner", mode=0o751)
        self._apply_directory_permissions(config.workspace_root / ".eden", user="root", group="planner", mode=0o750)
        self._apply_mode(config.workspace_root / "worktrees", 0o755)
        self._apply_tree_permissions(
            config.workspace_root / ".git",
            user="root",
            group="planner",
            directory_mode=0o750,
            file_mode=0o640,
        )
        self._apply_tree_permissions(
            config.proposals_dir,
            user="planner",
            group="root",
            directory_mode=0o770,
            file_mode=0o660,
        )
        self._apply_tree_permissions(
            config.artifacts_dir,
            user="root",
            group="planner",
            directory_mode=0o750,
            file_mode=0o640,
        )
        self._apply_file_permissions(config.results_db, user="root", group="planner", mode=0o640)
        self._apply_file_permissions(config.proposals_db, user="planner", group="root", mode=0o660)
        self._apply_grant_source_permissions(config)
        self._restore_existing_worktree_git_metadata(config)

    def _ensure_directories(self, config: SessionConfig) -> None:
        """Create the directory layout needed by the runtime."""
        (config.experiment_root / ".eden").mkdir(parents=True, exist_ok=True)
        (config.planner_root / ".eden").mkdir(parents=True, exist_ok=True)
        (config.workspace_root / ".eden").mkdir(parents=True, exist_ok=True)
        (config.workspace_root / "worktrees").mkdir(parents=True, exist_ok=True)
        config.proposals_dir.mkdir(parents=True, exist_ok=True)
        config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        config.results_db.parent.mkdir(parents=True, exist_ok=True)
        config.proposals_db.parent.mkdir(parents=True, exist_ok=True)

    def _apply_grant_source_permissions(self, config: SessionConfig) -> None:
        """Make explicitly granted files readable to the intended actor."""
        for grant in config.file_permissions:
            source = config.experiment_root / grant.path
            self._ensure_ancestor_traversal(source)
            if grant.actor == "planner":
                self._apply_file_permissions(source, user="root", group="planner", mode=0o640)
            else:
                self._apply_file_permissions(source, user="root", group="root", mode=0o644)

    def _ensure_user(self, username: str) -> None:
        """Ensure a system user exists."""
        if self.runner.run(["id", "-u", username]).returncode == 0:
            return
        result = self.runner.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", username])
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"Failed to create user: {username}"
            raise RuntimeError(message)

    def _mark_git_safe_directory(self, workspace_root: Path) -> None:
        """Allow non-owner planner reads of the shared workspace repository."""
        result = self.runner.run(["git", "config", "--system", "--add", "safe.directory", str(workspace_root)])
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            if not message:
                message = f"Failed to mark safe.directory: {workspace_root}"
            raise RuntimeError(message)

    def _ensure_runtime_dirs(self, username: str) -> None:
        """Create writable per-user runtime directories for XDG state and cache."""
        runtime_root = os.environ.get("EDEN_RUNTIME_DIR")
        if not runtime_root:
            return
        user_root = Path(runtime_root) / username
        for name in ("state", "cache", "share", "home", "tmp"):
            path = user_root / name
            path.mkdir(parents=True, exist_ok=True)
            self._apply_directory_permissions(path, user=username, group=username, mode=0o700)
        codex_home = user_root / "home" / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        self._seed_codex_home(codex_home)
        self._apply_tree_permissions(
            codex_home,
            user=username,
            group=username,
            directory_mode=0o700,
            file_mode=0o600,
        )
        self._seed_claude_auth(user_root)
        for claude_dir in self._claude_auth_destinations(user_root):
            self._apply_tree_permissions(
                claude_dir,
                user=username,
                group=username,
                directory_mode=0o700,
                file_mode=0o600,
            )

    def _seed_codex_home(self, destination: Path) -> None:
        """Populate a writable Codex home from the mounted auth directory when available."""
        auth_home = os.environ.get("EDEN_AUTH_HOME")
        if not auth_home:
            return
        source = Path(auth_home) / ".codex"
        if not source.exists() or not source.is_dir():
            return
        self._copy_auth_tree(source, destination)

    def _seed_claude_auth(self, user_root: Path) -> None:
        """Populate writable Claude CLI auth directories from the mounted auth home."""
        auth_home = os.environ.get("EDEN_AUTH_HOME")
        if not auth_home:
            return
        auth_base = Path(auth_home)

        # HOME-relative paths: destination is under user_root/home.
        home_mappings = [
            (".claude", "home/.claude"),
            (".config/claude", "home/.config/claude"),
        ]
        # XDG-relative paths: destination mirrors the overridden XDG dirs.
        xdg_mappings = [
            (".local/state/claude", "state/claude"),
            (".local/share/claude", "share/claude"),
            (".cache/claude", "cache/claude"),
        ]
        for src_rel, dst_rel in home_mappings + xdg_mappings:
            source = auth_base / src_rel
            if not source.is_dir():
                continue
            destination = user_root / dst_rel
            destination.mkdir(parents=True, exist_ok=True)
            self._copy_auth_tree(source, destination)

        # Single-file: .claude.json (legacy config).
        claude_json = auth_base / ".claude.json"
        if claude_json.is_file():
            target = user_root / "home" / ".claude.json"
            try:
                shutil.copy2(claude_json, target)
            except FileNotFoundError:
                pass

    @staticmethod
    def _claude_auth_destinations(user_root: Path) -> list[Path]:
        """Return Claude auth directories under a user runtime root for permission fixup."""
        candidates = [
            user_root / "home" / ".claude",
            user_root / "home" / ".config" / "claude",
            user_root / "state" / "claude",
            user_root / "share" / "claude",
            user_root / "cache" / "claude",
        ]
        return [p for p in candidates if p.exists()]

    def _copy_auth_tree(self, source: Path, destination: Path) -> None:
        """Copy directory contents, skipping broken symlinks and transient dirs."""
        for item in source.iterdir():
            if item.name == "tmp":
                continue
            target = destination / item.name
            try:
                if item.is_symlink() and not item.exists():
                    continue
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True, ignore_dangling_symlinks=True)
                else:
                    shutil.copy2(item, target)
            except FileNotFoundError:
                continue

    def _apply_tree_permissions(
        self,
        path: Path,
        *,
        user: str,
        group: str,
        directory_mode: int,
        file_mode: int,
    ) -> None:
        """Apply ownership and permissions recursively to a tree."""
        if not path.exists():
            return
        for current in [path, *self._walk_descendants(path)]:
            if current.is_dir():
                if self._apply_ownership(current, user=user, group=group):
                    self._apply_mode(current, directory_mode)
            else:
                if self._apply_ownership(current, user=user, group=group):
                    self._apply_mode(current, file_mode)

    def _apply_file_permissions(self, path: Path, *, user: str, group: str, mode: int) -> None:
        """Apply ownership and permissions to a single file when present."""
        if not path.exists():
            return
        if self._apply_ownership(path, user=user, group=group):
            self._apply_mode(path, mode)

    def _apply_directory_permissions(self, path: Path, *, user: str, group: str, mode: int) -> None:
        """Apply ownership and permissions to a single directory when present."""
        if not path.exists():
            return
        if self._apply_ownership(path, user=user, group=group):
            self._apply_mode(path, mode)

    def _ensure_ancestor_traversal(self, path: Path) -> None:
        """Grant execute-only traversal on ancestors needed to reach the workspace."""
        anchor = Path(path.anchor)
        for ancestor in reversed(path.parents):
            if ancestor == anchor:
                continue
            try:
                current_mode = ancestor.stat().st_mode & 0o777
                self._apply_mode(ancestor, current_mode | 0o001)
            except PermissionError:
                continue

    def _apply_ownership(self, path: Path, *, user: str, group: str) -> bool:
        """Apply ownership to a path when the filesystem permits it."""
        try:
            shutil.chown(path, user=user, group=group)
        except PermissionError:
            return False
        return True

    def _apply_mode(self, path: Path, mode: int) -> None:
        """Apply a POSIX mode to a path."""
        try:
            path.chmod(mode)
        except FileNotFoundError:
            return

    def _walk_descendants(self, path: Path) -> Iterable[Path]:
        """Yield all descendant paths depth-first."""
        for root, directories, files in os.walk(path):
            root_path = Path(root)
            for directory in directories:
                yield root_path / directory
            for filename in files:
                yield root_path / filename

    def _restore_existing_worktree_git_metadata(self, config: SessionConfig) -> None:
        """Reapply trial-readable git metadata to any existing slot worktrees."""
        worktree_root = config.workspace_root / "worktrees"
        if not worktree_root.exists():
            return
        for path in worktree_root.iterdir():
            if not path.is_dir() or not path.name.startswith("wt-"):
                continue
            try:
                slot = int(path.name.removeprefix("wt-"))
            except ValueError:
                continue
            secure_worktree_git_metadata(config.workspace_root, slot, f"trial-{slot}")


def main(argv: list[str] | None = None) -> int:
    """Run the runtime setup helper."""
    parser = argparse.ArgumentParser(prog="python -m eden.runtime")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    RuntimeSetup().prepare(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
