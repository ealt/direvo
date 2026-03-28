import os
import subprocess
import sys
import textwrap
from pathlib import Path

from direvo.config import load_config
from direvo.db import DatabaseManager
from direvo.models import ProposalStatus


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def _init_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".direvo").mkdir()
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")
    eval_script = workspace / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\nprintf '{\"test_pass_rate\": 1.0}\\n'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "test@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Test User"], cwd=workspace)
    _run(["git", "add", "."], cwd=workspace)
    _run(["git", "commit", "-m", "seed"], cwd=workspace)

    fake_execution = tmp_path / "fake-execution.sh"
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
    return workspace, fake_execution


def test_entrypoint_requires_arguments() -> None:
    entrypoint = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"

    result = subprocess.run(
        ["sh", str(entrypoint)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "usage: direvo-entrypoint" in result.stderr


def test_entrypoint_run_defaults_from_flag_only_invocation(tmp_path: Path) -> None:
    entrypoint = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
    repo_root = Path(__file__).resolve().parents[1]
    workspace, fake_execution = _init_workspace(tmp_path)
    head_sha = _run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

    config_path = workspace / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            execution_command: "sh {fake_execution} {{direction}}"
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
        slug="entrypoint",
        parent_commits=[head_sha],
        artifacts_uri=str(proposal_dir),
        status=ProposalStatus.READY,
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")

    result = subprocess.run(
        ["sh", str(entrypoint), "--config", str(config_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    trial_row = database_manager.get_trial_row(1)
    assert trial_row is not None
    assert trial_row["status"] == "success"


def test_entrypoint_doctor_skips_runtime_setup(tmp_path: Path) -> None:
    entrypoint = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
    repo_root = Path(__file__).resolve().parents[1]
    workspace, fake_execution = _init_workspace(tmp_path)
    config_path = workspace / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            parallel_trials: 1
            eval_script: "./evaluate.sh"
            execution_command: "sh {fake_execution} {{direction}}"
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

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")

    result = subprocess.run(
        ["sh", str(entrypoint), "doctor", "--config", str(config_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert not (workspace / ".direvo" / "results.db").exists()
    assert not (workspace / "worktrees").exists()


def test_entrypoint_run_requires_config() -> None:
    entrypoint = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"

    result = subprocess.run(
        ["sh", str(entrypoint), "run"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "error: --config is required for run" in result.stderr
