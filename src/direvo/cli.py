"""Command-line interface for DirEvo."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .git_manager import GitManager
from .orchestrator import Orchestrator, bootstrap


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(prog="direvo")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("run", "doctor"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--config", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            return doctor(Path(args.config))
        if args.command == "run":
            result = bootstrap(Path(args.config))
            orchestrator = Orchestrator(result.config, result.database_manager, result.logger)
            orchestrator.run()
            return 0
    except (ConfigError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


def doctor(config_path: Path) -> int:
    """Validate local environment requirements for a config."""
    config = load_config(config_path)
    if not config.workspace_root.exists():
        raise RuntimeError(f"Workspace root does not exist: {config.workspace_root}")
    if not config.workspace_root.is_dir():
        raise RuntimeError(f"Workspace root is not a directory: {config.workspace_root}")
    if not GitManager(config.workspace_root).is_git_repo():
        raise RuntimeError(f"Workspace is not a git repo: {config.workspace_root}")

    execution_binary = shlex.split(config.execution_command)[0]
    checks = {
        "git": _resolve_executable("git"),
        "execution_command": _resolve_executable(execution_binary),
    }
    if config.planner_command is not None:
        planner_binary = shlex.split(config.planner_command)[0]
        checks["planner_command"] = _resolve_executable(planner_binary)
    missing = [name for name, resolved in checks.items() if resolved is None]
    if missing:
        raise RuntimeError(f"Missing required executables: {', '.join(missing)}")

    with sqlite3.connect(":memory:") as connection:
        connection.execute("select sqlite_version()")

    if not config.eval_script.exists():
        raise RuntimeError(f"Evaluation script does not exist: {config.eval_script}")
    if not config.eval_script.is_file():
        raise RuntimeError(f"Evaluation script is not a file: {config.eval_script}")
    mode = config.eval_script.stat().st_mode
    if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) == 0:
        raise RuntimeError(f"Evaluation script is not executable: {config.eval_script}")

    writable_paths = [
        config.results_db.parent,
        config.proposals_db.parent,
        config.proposals_dir,
        config.artifacts_dir,
    ]
    for path in writable_paths:
        existing_parent = _nearest_existing_parent(path)
        if existing_parent is None:
            raise RuntimeError(f"No existing parent directory found for path: {path}")
        if not os.access(existing_parent, os.W_OK | os.X_OK):
            raise RuntimeError(f"Path is not writable: {existing_parent}")
    return 0


def _nearest_existing_parent(path: Path) -> Path | None:
    """Return the nearest existing directory for a target path."""
    candidate = path
    while True:
        if candidate.exists():
            return candidate if candidate.is_dir() else candidate.parent
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent


def _resolve_executable(command: str) -> str | None:
    """Resolve a command name or absolute executable path."""
    candidate = Path(command)
    if candidate.parent != Path():
        if candidate.exists() and candidate.is_file():
            mode = candidate.stat().st_mode
            if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                return str(candidate)
        return None
    return shutil.which(command)


if __name__ == "__main__":
    raise SystemExit(main())
