"""Command-line interface for EDEN."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

from .cleanup import hard_reset_experiment_state
from .config import ConfigError, load_config
from .docker_runner import build_image, run_container
from .git_manager import GitManager
from .init import scaffold_experiment
from .orchestrator import Orchestrator, bootstrap
from .summary import print_summary


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(prog="eden")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("directory", nargs="?", default=".")
    init_parser.add_argument("--force", action="store_true")

    for command in ("run", "doctor"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--config", required=True)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Hard reset experiment state (worktrees, DBs, logs, proposals, artifacts, trial/* branches)",
    )
    cleanup_parser.add_argument("--config", required=True)

    docker_parser = subparsers.add_parser("docker")
    docker_sub = docker_parser.add_subparsers(dest="docker_command")
    for docker_cmd in ("build", "run"):
        sub = docker_sub.add_parser(docker_cmd)
        sub.add_argument("--config", required=True)
        sub.add_argument("--tag", default=None)
    docker_sub.choices["run"].add_argument("--output", default=None)

    ui_parser = subparsers.add_parser("ui")
    ui_group = ui_parser.add_mutually_exclusive_group(required=True)
    ui_group.add_argument("--config", default=None)
    ui_group.add_argument("--experiment-dir", default=None)
    ui_parser.add_argument("--port", type=int, default=8741)
    ui_parser.add_argument("--no-open", action="store_true")
    ui_parser.add_argument("--dev", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            scaffold_experiment(Path(args.directory), force=args.force)
            return 0
        if args.command == "doctor":
            return doctor(Path(args.config))
        if args.command == "cleanup":
            return cleanup_command(Path(args.config))
        if args.command == "run":
            result = bootstrap(Path(args.config))
            orchestrator = Orchestrator(result.config, result.database_manager, result.logger)
            orchestrator.run()
            print_summary(orchestrator)
            return 0
        if args.command == "docker":
            return _docker_command(args)
        if args.command == "ui":
            return _ui_command(args)
    except (ConfigError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


def _docker_command(args: argparse.Namespace) -> int:
    """Handle eden docker build/run commands."""
    if not args.docker_command:
        args.docker_command = "run"

    config = load_config(Path(args.config))
    if config.docker is None:
        print("error: config has no docker section", file=sys.stderr)
        return 1

    if not shutil.which("docker"):
        print("error: docker is not installed or not in PATH", file=sys.stderr)
        return 1

    tag = build_image(config, tag=args.tag)
    print(f"Built image: {tag}")

    if args.docker_command == "build":
        return 0

    output_dir = Path(args.output) if args.output else None
    return run_container(config, tag=tag, output_dir=output_dir)


def _ui_command(args: argparse.Namespace) -> int:
    """Handle eden ui command."""
    try:
        import uvicorn
    except ImportError:
        print("error: eden[web] extras not installed. Run: uv pip install 'direvo[web]'", file=sys.stderr)
        return 1

    from .web.server import create_app

    config_path = Path(args.config) if args.config else None
    experiment_dir = Path(args.experiment_dir) if args.experiment_dir else None

    # Locate SPA build directory.
    spa_dir: Path | None = None
    if not args.dev:
        # Check source-tree location first, then installed package.
        candidates = [
            Path(__file__).resolve().parent.parent.parent / "packages" / "web-ui" / "dist",
            Path(__file__).resolve().parent / "web" / "static",
        ]
        for candidate in candidates:
            if candidate.is_dir():
                spa_dir = candidate
                break
        if spa_dir is None:
            print(
                "warning: SPA build not found. Run 'cd packages/web-ui && npm run build' first,\n"
                "  or use --dev to proxy to the Vite dev server.",
                file=sys.stderr,
            )

    app = create_app(config_path=config_path, experiment_dir=experiment_dir, dev=args.dev, spa_dir=spa_dir)
    port = args.port

    if not args.no_open and not args.dev:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=[f"http://localhost:{port}"]).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def cleanup_command(config_path: Path) -> int:
    """Hard reset experiment state to match a fresh tree before ``eden run``."""
    config = load_config(config_path)
    if not GitManager(config.workspace_root).is_git_repo():
        raise RuntimeError(f"Workspace is not a git repo: {config.workspace_root}")

    result = hard_reset_experiment_state(config)
    print(f"Removed {result.worktrees_removed} worktree directory(ies) under workspace/worktrees/.")
    if result.branches_deleted:
        print("Deleted local branches:\n  " + "\n  ".join(result.branches_deleted))
    else:
        print("No trial/* branches to delete.")
    print(f"Removed {result.sqlite_files_removed} SQLite file(s) (databases and journals).")
    if result.session_log_cleared:
        print("Cleared session.log.")
    else:
        print("session.log was already absent; nothing to clear.")
    print(f"Removed {result.proposal_items_removed} item(s) from the proposals directory.")
    print(f"Removed {result.artifact_items_removed} item(s) from the artifacts directory.")
    print(f"Removed {result.planner_symlinks_removed} planner .eden symlink(s) (results.db / artifacts).")
    print(f"Removed {result.grants_removed} planner grant symlink(s).")
    return 0


def doctor(config_path: Path) -> int:
    """Validate local environment requirements for a config."""
    config = load_config(config_path)
    if not config.workspace_root.exists():
        raise RuntimeError(f"Workspace root does not exist: {config.workspace_root}")
    if not config.workspace_root.is_dir():
        raise RuntimeError(f"Workspace root is not a directory: {config.workspace_root}")
    if not GitManager(config.workspace_root).is_git_repo():
        raise RuntimeError(f"Workspace is not a git repo: {config.workspace_root}")

    execute_binary = shlex.split(config.implement_command)[0]
    evaluate_binary = shlex.split(config.evaluate_command)[0]
    checks = {
        "git": _resolve_executable("git"),
        "implement_command": _resolve_executable(execute_binary),
        "evaluate_command": _resolve_executable(evaluate_binary),
    }
    if config.plan_command is not None:
        plan_binary = shlex.split(config.plan_command)[0]
        checks["plan_command"] = _resolve_executable(plan_binary)
    missing = [name for name, resolved in checks.items() if resolved is None]
    if missing:
        raise RuntimeError(f"Missing required executables: {', '.join(missing)}")

    with sqlite3.connect(":memory:") as connection:
        connection.execute("select sqlite_version()")

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
