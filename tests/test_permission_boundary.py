import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from direvo.config import load_config
from direvo.db import DatabaseManager
from direvo.git_manager import GitManager
from direvo.runtime import RuntimeSetup
from direvo.worktree import secure_worktree_root


if os.geteuid() != 0 or os.environ.get("DIREVO_RUN_PRIVILEGED_TESTS") != "1":
    pytestmark = pytest.mark.skip(
        reason="requires root in an ephemeral environment and DIREVO_RUN_PRIVILEGED_TESTS=1"
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
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    eval_script = tmp_path / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\nprintf '{\"test_pass_rate\": 1.0}\\n'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)

    config_path = tmp_path / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
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

    assert _su_status("planner", f"test -r {tmp_path / '.git' / 'HEAD'}") == 0
    assert _su_status("planner", f"test -r {config.results_db}") == 0
    assert _su_status("planner", f"test -w {config.proposals_db}") == 0

    assert _su_status("trial-0", f"test -r {worktree_zero / 'tracked.txt'}") == 0
    assert _su_status("trial-0", f"test -r {tmp_path / '.git' / 'HEAD'}") != 0
    assert _su_status("trial-0", f"test -r {config.results_db}") != 0
    assert _su_status("trial-0", f"ls {worktree_one}") != 0
