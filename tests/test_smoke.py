import os
import subprocess
import sys
import textwrap
from pathlib import Path

from direvo.config import load_config
from direvo.db import DatabaseManager
from direvo.models import ProposalStatus
from direvo.runtime import RuntimeSetup


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def test_cli_doctor_and_run_smoke(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".direvo").mkdir()
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")

    eval_script = workspace / "evaluate.sh"
    eval_script.write_text(
        "#!/bin/sh\nprintf '{\"test_pass_rate\": 1.0}\\n'\n",
        encoding="utf-8",
    )
    eval_script.chmod(0o755)

    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "test@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Test User"], cwd=workspace)
    _run(["git", "add", "."], cwd=workspace)
    _run(["git", "commit", "-m", "seed"], cwd=workspace)
    head_sha = _run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

    config_path = workspace / ".direvo" / "config.yaml"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        textwrap.dedent(
            """#!/bin/sh
            printf 'changed\\n' > code.txt
            mkdir -p .direvo/trial
            printf '%s\\n' "$*" > .direvo/trial/implementation.md
            """
        ),
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    config_path.write_text(
        textwrap.dedent(
            f"""
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            execution_command: "sh {fake_claude} {{direction}}"
            max_trials: 5
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
    RuntimeSetup().prepare(config)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    database_manager.create_proposal(
        priority=1.0,
        slug="smoke",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")

    _run(
        [sys.executable, "-m", "direvo.cli", "doctor", "--config", str(config_path)],
        cwd=repo_root,
        env=env,
    )
    _run(
        [sys.executable, "-m", "direvo.cli", "run", "--config", str(config_path)],
        cwd=repo_root,
        env=env,
    )

    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"
    assert trial_row["commit_sha"]

    proposal_row = database_manager.get_proposal_row(1)
    assert proposal_row is not None
    assert proposal_row["status"] == "completed"

    artifact_plan = workspace / ".direvo" / "artifacts" / "trial-1" / "plan.md"
    artifact_impl = workspace / ".direvo" / "artifacts" / "trial-1" / "implementation.md"
    session_log = workspace / ".direvo" / "session.log"
    assert artifact_plan.exists()
    assert artifact_impl.exists()
    assert session_log.exists()
