"""Scaffold a new EDEN experiment directory."""

from __future__ import annotations

import importlib.resources
import shutil
import subprocess
from importlib.resources.abc import Traversable
from pathlib import Path


def scaffold_experiment(target: Path, *, force: bool = False) -> None:
    """Create a new experiment directory from the built-in templates.

    Copies the template tree into *target*, placing the config file under
    ``.eden/config.yaml``.  Initializes a git repository in the workspace
    directory with an initial commit.

    Args:
        target: Destination directory.  Created if it does not exist.
        force: If True, allow scaffolding into a non-empty directory.

    Raises:
        SystemExit: If *target* is non-empty and *force* is False.
    """
    target = target.resolve()

    if target.exists() and any(target.iterdir()) and not force:
        raise SystemExit(
            f"error: directory is not empty: {target}\n"
            "Use --force to scaffold into an existing directory."
        )

    target.mkdir(parents=True, exist_ok=True)

    templates = importlib.resources.files("eden.templates")
    _copy_tree(templates, target)

    # Move config.yaml into .eden/
    eden_dir = target / ".eden"
    eden_dir.mkdir(exist_ok=True)
    config_src = target / "config.yaml"
    if config_src.exists():
        shutil.move(str(config_src), str(eden_dir / "config.yaml"))

    # Initialize workspace git repo with explicit identity
    workspace = target / "planner" / "workspace"
    if workspace.is_dir() and not (workspace / ".git").exists():
        subprocess.run(
            ["git", "init", "-q"],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c", "user.name=eden",
                "-c", "user.email=eden@experiment",
                "commit",
                "--allow-empty",
                "-q",
                "-m", "initial baseline",
            ],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    _print_summary(target)


def _copy_tree(source: Traversable, dest: Path) -> None:
    """Recursively copy a traversable resource tree to a filesystem path."""
    for item in source.iterdir():
        if item.name == "__init__.py" or item.name == "__pycache__":
            continue
        target = dest / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())


def _print_summary(target: Path) -> None:
    """Print a summary of the scaffolded experiment."""
    print(f"Created EDEN experiment at {target}/")
    print()
    print("  .eden/config.yaml    -- experiment configuration")
    print("  eval.py              -- evaluator stub")
    print("  implement.py         -- implementer stub")
    print("  planner/plan.py      -- planner stub")
    print("  planner/AGENTS.md    -- planner agent guidance")
    print("  planner/.agents/     -- planner skills")
    print("  planner/workspace/   -- git repo (initialized)")
    print()
    print("Next steps:")
    print("  1. Edit eval.py and implement.py with your experiment logic")
    print("  2. Update implement_command and evaluate_command in .eden/config.yaml")
    print("  3. Add your initial code to planner/workspace/ and commit")
    print("  4. Run: eden doctor --config .eden/config.yaml")
