import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from eden.config import load_config
from eden.db import DatabaseManager
from eden.models import ProposalStatus

if shutil.which("docker") is None or os.environ.get("EDEN_RUN_DOCKER_TESTS") != "1":
    pytestmark = pytest.mark.skip(reason="requires Docker and EDEN_RUN_DOCKER_TESTS=1")


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def test_docker_run_smoke(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    experiment_root = tmp_path / "experiment"
    workspace = experiment_root / "planner" / "workspace"
    workspace.mkdir(parents=True)
    (experiment_root / ".eden").mkdir(parents=True)
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")

    eval_script = experiment_root / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\nprintf '{\"test_pass_rate\": 1.0}\\n'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    fake_execution = workspace / "fake-execution.sh"
    fake_execution.write_text(
        textwrap.dedent(
            """#!/bin/sh
            printf 'changed\\n' > code.txt
            mkdir -p .eden/trial
            printf '%s\\n' "$*" > .eden/trial/implementation.md
            """
        ),
        encoding="utf-8",
    )
    fake_execution.chmod(0o755)

    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "test@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Test User"], cwd=workspace)
    _run(["git", "add", "."], cwd=workspace)
    _run(["git", "commit", "-m", "seed"], cwd=workspace)
    head_sha = _run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

    config_path = experiment_root / ".eden" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            planner_root: "./planner"
            workspace: "./workspace"
            parallel_trials: 1
            evaluate_command: "./evaluate.sh"
            implement_command: "sh fake-execution.sh"
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
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    database_manager.create_proposal(
        priority=1.0,
        slug="docker-smoke",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    image_tag = f"eden-test:{uuid.uuid4().hex[:8]}"
    try:
        _run(["docker", "build", "-t", image_tag, "."], cwd=repo_root)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{experiment_root}:/experiment",
                image_tag,
                "--config",
                "/experiment/.eden/config.yaml",
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        subprocess.run(["docker", "rmi", "-f", image_tag], cwd=repo_root, check=False, capture_output=True, text=True)

    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"
    assert trial_row["commit_sha"]
    assert (experiment_root / ".eden" / "artifacts" / "trial-1" / "implementation.md").exists()
