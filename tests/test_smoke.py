import os
import subprocess
import sys
import textwrap
from pathlib import Path

from eden.config import load_config
from eden.db import DatabaseManager
from eden.models import ProposalStatus
from eden.runtime import RuntimeSetup


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def test_cli_doctor_and_run_smoke(tmp_path: Path) -> None:
    experiment_root = tmp_path / "experiment"
    workspace = experiment_root / "planner" / "workspace"
    (experiment_root / ".eden").mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")

    eval_script = experiment_root / "evaluate.sh"
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
    config_path = experiment_root / ".eden" / "config.yaml"
    fake_claude = workspace / "fake-implement.sh"
    fake_claude.write_text(
        textwrap.dedent(
            """#!/bin/sh
            printf 'changed\\n' > code.txt
            mkdir -p .eden/trial
            printf '%s\\n' "$*" > .eden/trial/implementation.md
            """
        ),
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    config_path.write_text(
        textwrap.dedent(
            """
            planner_root: "./planner"
            workspace: "./workspace"
            parallel_trials: 1
            evaluate_command: "./evaluate.sh"
            implement_command: "sh fake-implement.sh"
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
    _run(["git", "add", "fake-implement.sh"], cwd=workspace)
    _run(["git", "commit", "-m", "add implement helper"], cwd=workspace)
    head_sha = _run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

    config = load_config(config_path)
    proposal_dir = config.proposals_dir / "proposal-1"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("Implement the change.\n", encoding="utf-8")
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
        [sys.executable, "-m", "eden.cli", "doctor", "--config", str(config_path)],
        cwd=repo_root,
        env=env,
    )
    _run(
        [sys.executable, "-m", "eden.cli", "run", "--config", str(config_path)],
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

    artifact_plan = experiment_root / ".eden" / "artifacts" / "trial-1" / "plan.md"
    artifact_impl = experiment_root / ".eden" / "artifacts" / "trial-1" / "implementation.md"
    session_log = experiment_root / ".eden" / "session.log"
    assert artifact_plan.exists()
    assert artifact_impl.exists()
    assert session_log.exists()
