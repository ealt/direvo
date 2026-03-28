import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from direvo.config import load_config
from direvo.db import DatabaseManager
from direvo.models import ProposalStatus


if shutil.which("docker") is None or os.environ.get("DIREVO_RUN_DOCKER_TESTS") != "1":
    pytestmark = pytest.mark.skip(reason="requires Docker and DIREVO_RUN_DOCKER_TESTS=1")


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def test_docker_run_smoke(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".direvo").mkdir()
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")

    eval_script = workspace / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\nprintf '{\"test_pass_rate\": 1.0}\\n'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    fake_execution = workspace / "fake-execution.sh"
    fake_execution.write_text(
        textwrap.dedent(
            """#!/bin/sh
            printf 'changed\\n' > code.txt
            mkdir -p .direvo/trial
            printf '%s\\n' "$*" > .direvo/trial/implementation.md
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

    config_path = workspace / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            execution_command: "sh /workspace/fake-execution.sh {direction}"
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
    proposal_dir = workspace / ".direvo" / "proposals" / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")

    config = load_config(config_path)
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

    image_tag = f"direvo-test:{uuid.uuid4().hex[:8]}"
    try:
        _run(["docker", "build", "-t", image_tag, "."], cwd=repo_root)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{workspace}:/workspace",
                image_tag,
                "--config",
                "/workspace/.direvo/config.yaml",
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
    assert (workspace / ".direvo" / "artifacts" / "trial-1" / "implementation.md").exists()
