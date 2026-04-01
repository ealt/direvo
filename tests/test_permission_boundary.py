import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from eden.config import load_config
from eden.db import DatabaseManager
from eden.git_manager import GitManager
from eden.runtime import RuntimeSetup
from eden.worktree import secure_worktree_root

if os.geteuid() != 0 or os.environ.get("EDEN_RUN_PRIVILEGED_TESTS") != "1":
    pytestmark = pytest.mark.skip(
        reason="requires root in an ephemeral environment and EDEN_RUN_PRIVILEGED_TESTS=1"
    )


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _su_status(user: str, command: str) -> int:
    return subprocess.run(
        ["su", user, "-s", "/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def test_runtime_enforces_permission_boundaries(tmp_path: Path) -> None:
    experiment_root = tmp_path / "experiment"
    workspace = experiment_root / "planner" / "workspace"
    (experiment_root / ".eden").mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")
    eval_script = experiment_root / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\nprintf '{\"test_pass_rate\": 1.0}\\n'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "test@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Test User"], cwd=workspace)
    _run(["git", "add", "."], cwd=workspace)
    _run(["git", "commit", "-m", "seed"], cwd=workspace)

    config_path = experiment_root / ".eden" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            planner_root: "./planner"
            workspace: "./workspace"
            parallel_trials: 2
            evaluate_command: "./evaluate.sh"
            implement_command: "echo noop"
            max_trials: 1
            max_wall_time: "1h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()

    RuntimeSetup().prepare(config)

    git = GitManager(config.workspace_root)
    worktree_zero = git.initialize_worktree(0)
    worktree_one = git.initialize_worktree(1)
    secure_worktree_root(worktree_zero, "trial-0")
    secure_worktree_root(worktree_one, "trial-1")
    RuntimeSetup().prepare(config)

    assert (worktree_zero.stat().st_mode & 0o777) == 0o700
    assert (worktree_one.stat().st_mode & 0o777) == 0o700
    assert (config.results_db.stat().st_mode & 0o777) == 0o640
    assert (config.proposals_db.stat().st_mode & 0o777) == 0o660

    assert _su_status("planner", f"test -r {config.workspace_root / '.git' / 'HEAD'}") == 0
    assert _su_status("planner", f"test -r {config.results_db}") == 0
    assert _su_status("planner", f"test -w {config.proposals_db}") == 0

    assert _su_status("trial-0", f"test -r {worktree_zero / 'tracked.txt'}") == 0
    assert _su_status("trial-0", f"test -r {config.workspace_root / '.git' / 'HEAD'}") != 0
    assert _su_status("trial-0", f"test -r {config.results_db}") != 0
    assert _su_status("trial-0", f"ls {worktree_one}") != 0
    assert _su_status("trial-0", f"ls {config.planner_root}") != 0
