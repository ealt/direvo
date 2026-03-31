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

        self._ensure_ancestor_traversal(config.planner_root)
        self._ensure_ancestor_traversal(config.workspace_root)
        self._apply_directory_permissions(config.experiment_root, user="root", group="root", mode=0o711)
        self._apply_directory_permissions(config.experiment_root / ".direvo", user="root", group="root", mode=0o711)
        self._apply_directory_permissions(config.planner_root, user="planner", group="planner", mode=0o751)
        self._apply_directory_permissions(config.planner_root / ".direvo", user="planner", group="planner", mode=0o750)
        self._apply_directory_permissions(config.workspace_root, user="root", group="planner", mode=0o751)
        self._apply_directory_permissions(config.workspace_root / ".direvo", user="root", group="planner", mode=0o750)
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

    def _ensure_directories(self, config: SessionConfig) -> None:
        """Create the directory layout needed by the runtime."""
        (config.experiment_root / ".direvo").mkdir(parents=True, exist_ok=True)
        (config.planner_root / ".direvo").mkdir(parents=True, exist_ok=True)
        (config.workspace_root / ".direvo").mkdir(parents=True, exist_ok=True)
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
        path.chmod(mode)

    def _walk_descendants(self, path: Path) -> Iterable[Path]:
        """Yield all descendant paths depth-first."""
        for root, directories, files in os.walk(path):
            root_path = Path(root)
            for directory in directories:
                yield root_path / directory
            for filename in files:
                yield root_path / filename


def main(argv: list[str] | None = None) -> int:
    """Run the runtime setup helper."""
    parser = argparse.ArgumentParser(prog="python -m direvo.runtime")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    RuntimeSetup().prepare(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
