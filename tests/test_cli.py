import subprocess
import textwrap
from pathlib import Path

import pytest

from direvo.cli import doctor


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".direvo").mkdir()
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    eval_script = tmp_path / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    return tmp_path


def _write_config(workspace: Path) -> Path:
    config_path = workspace / ".direvo" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 1
            evaluate_command: "./evaluate.sh"
            execute_command: "echo noop"
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
    return config_path


def test_doctor_validates_runtime_requirements(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    config_path = _write_config(workspace)
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")

    assert doctor(config_path) == 0


def test_doctor_rejects_non_executable_eval_script(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    config_path = _write_config(workspace)
    (workspace / "evaluate.sh").chmod(0o644)
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")

    with pytest.raises(RuntimeError, match="Missing required executables"):
        doctor(config_path)
