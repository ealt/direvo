import subprocess
from pathlib import Path

import pytest

from eden.config import load_config
from eden.init import scaffold_experiment


def test_scaffold_creates_expected_files(tmp_path: Path) -> None:
    target = tmp_path / "my-experiment"
    scaffold_experiment(target)

    assert (target / ".eden" / "config.yaml").exists()
    assert (target / "eval.py").exists()
    assert (target / "implement.py").exists()
    assert (target / "plan.py").exists()
    assert (target / "planner" / "AGENTS.md").exists()
    assert (target / "planner" / ".agents" / "skills" / "write-proposal.md").exists()
    assert (target / "planner" / ".agents" / "skills" / "query-trial-results.md").exists()
    assert (target / "planner" / ".agents" / "skills" / "navigate-workspace.md").exists()
    assert (target / "planner" / ".agents" / "skills" / "read-trial-artifacts.md").exists()
    assert (target / "planner" / ".agents" / "skills" / "query-proposals.md").exists()


def test_scaffold_creates_valid_config(tmp_path: Path) -> None:
    target = tmp_path / "my-experiment"
    scaffold_experiment(target)

    config = load_config(target / ".eden" / "config.yaml")
    assert config.experiment_root == target
    assert config.parallel_trials == 3
    assert "score" in config.metrics_schema


def test_scaffold_initializes_workspace_git(tmp_path: Path) -> None:
    target = tmp_path / "my-experiment"
    scaffold_experiment(target)

    workspace = target / "planner" / "workspace"
    assert (workspace / ".git").exists()

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip()


def test_scaffold_aborts_on_nonempty_directory(tmp_path: Path) -> None:
    target = tmp_path / "my-experiment"
    target.mkdir()
    (target / "existing-file.txt").write_text("content", encoding="utf-8")

    with pytest.raises(SystemExit, match="not empty"):
        scaffold_experiment(target)


def test_scaffold_force_allows_nonempty_directory(tmp_path: Path) -> None:
    target = tmp_path / "my-experiment"
    target.mkdir()
    (target / "existing-file.txt").write_text("content", encoding="utf-8")

    scaffold_experiment(target, force=True)
    assert (target / ".eden" / "config.yaml").exists()
    assert (target / "existing-file.txt").exists()


def test_scaffold_into_current_directory(tmp_path: Path) -> None:
    scaffold_experiment(tmp_path, force=True)
    assert (tmp_path / ".eden" / "config.yaml").exists()


def test_cli_init_creates_experiment(tmp_path: Path) -> None:
    target = tmp_path / "cli-test"
    result = subprocess.run(
        ["python", "-m", "eden.cli", "init", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert (target / ".eden" / "config.yaml").exists()
    assert "Created EDEN experiment" in result.stdout
