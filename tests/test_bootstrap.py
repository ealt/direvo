"""Tests for the bootstrap module extraction."""

import subprocess
import textwrap
from pathlib import Path

import pytest


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _init_experiment(tmp_path: Path) -> tuple[Path, Path]:
    experiment_root = tmp_path / "experiment"
    planner_root = experiment_root / "planner"
    workspace = planner_root / "workspace"
    (experiment_root / ".eden").mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")
    eval_script = experiment_root / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\necho '{\"score\": 1.0}'\n", encoding="utf-8")
    eval_script.chmod(0o755)
    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "test@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Test"], cwd=workspace)
    _run(["git", "add", "."], cwd=workspace)
    _run(["git", "commit", "-m", "seed"], cwd=workspace)
    return experiment_root, workspace


def _write_config(experiment_root: Path) -> Path:
    config_path = experiment_root / ".eden" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            planner_root: "./planner"
            workspace: "./workspace"
            parallel_trials: 1
            evaluate_command: "./evaluate.sh"
            implement_command: "echo noop"
            max_trials: 1
            max_wall_time: "1h"
            objective:
              expr: "score"
              direction: "maximize"
            metrics_schema:
              score: real
            """
        ),
        encoding="utf-8",
    )
    return config_path


def test_bootstrap_from_bootstrap_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Import bootstrap directly from eden.bootstrap."""
    from eden.bootstrap import BootstrapResult, bootstrap

    experiment_root, _workspace = _init_experiment(tmp_path)
    config_path = _write_config(experiment_root)

    class FakeRuntimeSetup:
        def prepare(self, config: object) -> None:
            pass

    monkeypatch.setattr("eden.bootstrap.RuntimeSetup", FakeRuntimeSetup)

    result = bootstrap(config_path, progress=False)
    assert isinstance(result, BootstrapResult)
    assert result.config.parallel_trials == 1
    assert result.session_log_path.exists()


def test_bootstrap_reexport_from_orchestrator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Import bootstrap from eden.orchestrator (backward compat re-export)."""
    from eden.orchestrator import BootstrapResult, bootstrap

    experiment_root, _workspace = _init_experiment(tmp_path)
    config_path = _write_config(experiment_root)

    class FakeRuntimeSetup:
        def prepare(self, config: object) -> None:
            pass

    monkeypatch.setattr("eden.bootstrap.RuntimeSetup", FakeRuntimeSetup)

    result = bootstrap(config_path, progress=False)
    assert isinstance(result, BootstrapResult)
    assert result.config.parallel_trials == 1


def test_bootstrap_initializes_databases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    experiment_root, _workspace = _init_experiment(tmp_path)
    config_path = _write_config(experiment_root)

    class FakeRuntimeSetup:
        def prepare(self, config: object) -> None:
            pass

    monkeypatch.setattr("eden.bootstrap.RuntimeSetup", FakeRuntimeSetup)

    from eden.bootstrap import bootstrap

    result = bootstrap(config_path, progress=False)
    assert result.config.results_db.exists()
    assert result.config.proposals_db.exists()


def test_bootstrap_rejects_non_git_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    experiment_root = tmp_path / "experiment"
    planner_root = experiment_root / "planner"
    workspace = planner_root / "workspace"
    (experiment_root / ".eden").mkdir(parents=True)
    workspace.mkdir(parents=True)
    eval_script = experiment_root / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\necho '{}'\n", encoding="utf-8")
    eval_script.chmod(0o755)
    config_path = _write_config(experiment_root)

    from eden.bootstrap import bootstrap

    with pytest.raises(RuntimeError, match="not a git repo"):
        bootstrap(config_path, progress=False)
