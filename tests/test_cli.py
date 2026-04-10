import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from eden.cli import cleanup_command, doctor, main
from eden.config import load_config
from eden.db import DatabaseManager
from eden.git_manager import GitManager


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def experiment(tmp_path: Path) -> tuple[Path, Path]:
    experiment_root = tmp_path / "experiment"
    planner_root = experiment_root / "planner"
    workspace = planner_root / "workspace"
    (experiment_root / ".eden").mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")
    eval_script = experiment_root / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\necho '{\"test_pass_rate\": 1.0}'\n", encoding="utf-8")
    eval_script.chmod(0o755)

    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "test@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Test User"], cwd=workspace)
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
    monkeypatch: pytest.MonkeyPatch, experiment: tuple[Path, Path]
) -> None:
    experiment_root, _workspace = experiment
    config_path = _write_config(experiment_root)
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")

    assert doctor(config_path) == 0


def test_doctor_rejects_non_executable_eval_script(
    monkeypatch: pytest.MonkeyPatch, experiment: tuple[Path, Path]
) -> None:
    experiment_root, _workspace = experiment
    config_path = _write_config(experiment_root)
    (experiment_root / "evaluate.sh").chmod(0o644)
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")

    with pytest.raises(RuntimeError, match="Missing required executables"):
        doctor(config_path)


def test_main_run_prints_summary(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    bootstrap_result = SimpleNamespace(config=object(), database_manager=object(), logger=object())

    class FakeOrchestrator:
        def __init__(self, config: object, database_manager: object, logger: object) -> None:
            self.config = config
            self.database_manager = database_manager
            self.logger = logger
            self.ran = False

        def run(self) -> int:
            self.ran = True
            return 1

    orchestrators: list[FakeOrchestrator] = []

    def fake_bootstrap(_config_path: Path) -> SimpleNamespace:
        return bootstrap_result

    def fake_orchestrator(config: object, database_manager: object, logger: object) -> FakeOrchestrator:
        instance = FakeOrchestrator(config, database_manager, logger)
        orchestrators.append(instance)
        return instance

    monkeypatch.setattr("eden.cli.bootstrap", fake_bootstrap)
    monkeypatch.setattr("eden.cli.Orchestrator", fake_orchestrator)
    monkeypatch.setattr("eden.cli.print_summary", lambda _orchestrator: print("summary"))

    assert main(["run", "--config", "config.yaml"]) == 0
    assert orchestrators
    assert orchestrators[0].ran
    assert capsys.readouterr().out == "summary\n"


def test_docker_build_parses_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls: list[tuple[object, str | None]] = []

    def fake_load_config(path: Path) -> SimpleNamespace:
        return SimpleNamespace(docker=SimpleNamespace())

    def fake_build_image(config: object, *, tag: str | None = None) -> str:
        build_calls.append((config, tag))
        return tag or "test-image"

    monkeypatch.setattr("eden.cli.load_config", fake_load_config)
    monkeypatch.setattr("eden.cli.build_image", fake_build_image)
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    assert main(["docker", "build", "--config", "config.yaml"]) == 0
    assert len(build_calls) == 1
    assert build_calls[0][1] is None


def test_docker_build_with_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls: list[tuple[object, str | None]] = []

    def fake_load_config(path: Path) -> SimpleNamespace:
        return SimpleNamespace(docker=SimpleNamespace())

    def fake_build_image(config: object, *, tag: str | None = None) -> str:
        build_calls.append((config, tag))
        return tag or "test-image"

    monkeypatch.setattr("eden.cli.load_config", fake_load_config)
    monkeypatch.setattr("eden.cli.build_image", fake_build_image)
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    assert main(["docker", "build", "--config", "c.yaml", "--tag", "my-tag"]) == 0
    assert build_calls[0][1] == "my-tag"


def test_docker_run_calls_build_and_run(monkeypatch: pytest.MonkeyPatch) -> None:
    run_calls: list[tuple[object, str, Path | None]] = []

    def fake_load_config(path: Path) -> SimpleNamespace:
        return SimpleNamespace(docker=SimpleNamespace())

    def fake_build_image(config: object, *, tag: str | None = None) -> str:
        return tag or "test-image"

    def fake_run_container(config: object, *, tag: str, output_dir: Path | None = None) -> int:
        run_calls.append((config, tag, output_dir))
        return 0

    monkeypatch.setattr("eden.cli.load_config", fake_load_config)
    monkeypatch.setattr("eden.cli.build_image", fake_build_image)
    monkeypatch.setattr("eden.cli.run_container", fake_run_container)
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    assert main(["docker", "run", "--config", "c.yaml", "--output", "/tmp/out"]) == 0
    assert len(run_calls) == 1
    assert run_calls[0][1] == "test-image"
    assert run_calls[0][2] == Path("/tmp/out")


def test_docker_rejects_missing_docker_section(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load_config(path: Path) -> SimpleNamespace:
        return SimpleNamespace(docker=None)

    monkeypatch.setattr("eden.cli.load_config", fake_load_config)
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    assert main(["docker", "build", "--config", "c.yaml"]) == 1


def test_docker_rejects_missing_docker_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load_config(path: Path) -> SimpleNamespace:
        return SimpleNamespace(docker=SimpleNamespace())

    monkeypatch.setattr("eden.cli.load_config", fake_load_config)
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    assert main(["docker", "build", "--config", "c.yaml"]) == 1


def test_cleanup_removes_worktrees(experiment: tuple[Path, Path]) -> None:
    experiment_root, workspace = experiment
    config_path = _write_config(experiment_root)
    git = GitManager(workspace)
    wt = git.ensure_worktree(0)
    assert wt.is_dir()

    assert cleanup_command(config_path) == 0
    assert not wt.exists()


def test_cleanup_hard_reset_clears_experiment_outputs(experiment: tuple[Path, Path]) -> None:
    experiment_root, workspace = experiment
    config_path = _write_config(experiment_root)
    config = load_config(config_path)
    (config.experiment_root / ".eden").mkdir(parents=True, exist_ok=True)
    (config.planner_root / ".eden").mkdir(parents=True, exist_ok=True)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()

    planner_eden = config.planner_root / ".eden"
    (planner_eden / "results.db").symlink_to(config.results_db.resolve())
    (planner_eden / "artifacts").symlink_to(config.artifacts_dir.resolve())

    (config.artifacts_dir / "trial-1").mkdir(parents=True)
    (config.artifacts_dir / "trial-1" / "plan.md").write_text("p", encoding="utf-8")
    proposal_dir = config.proposals_dir / "proposal-a"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "plan.md").write_text("q", encoding="utf-8")
    (config.experiment_root / ".eden" / "session.log").write_text("log line\n", encoding="utf-8")

    git = GitManager(workspace)
    head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git.create_branch("trial/1-smoke", head)
    git.ensure_worktree(0)

    assert cleanup_command(config_path) == 0

    assert not config.results_db.exists()
    assert not config.proposals_db.exists()
    assert (config.experiment_root / ".eden" / "session.log").read_text(encoding="utf-8") == ""
    assert not (planner_eden / "results.db").exists()
    assert not (planner_eden / "artifacts").exists()
    assert not (config.artifacts_dir / "trial-1").exists()
    assert not proposal_dir.exists()
    assert (config.experiment_root / ".eden" / "config.yaml").exists()

    branches = subprocess.run(
        ["git", "-C", str(workspace), "branch", "--list", "trial/*"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branches == ""


def test_cleanup_then_database_initialize_succeeds(experiment: tuple[Path, Path]) -> None:
    experiment_root, workspace = experiment
    config_path = _write_config(experiment_root)
    config = load_config(config_path)
    (config.experiment_root / ".eden").mkdir(parents=True, exist_ok=True)
    (config.planner_root / ".eden").mkdir(parents=True, exist_ok=True)
    database_manager = DatabaseManager(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        metrics_schema=config.metrics_schema,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )
    database_manager.initialize()
    cleanup_command(config_path)
    database_manager.initialize()
    assert config.results_db.is_file()
    assert config.proposals_db.is_file()


def test_main_cleanup_invokes_cleanup_command(
    monkeypatch: pytest.MonkeyPatch, experiment: tuple[Path, Path]
) -> None:
    experiment_root, workspace = experiment
    config_path = _write_config(experiment_root)
    GitManager(workspace).ensure_worktree(0)

    calls: list[Path] = []

    def fake_cleanup_command(path: Path) -> int:
        calls.append(path)
        return 0

    monkeypatch.setattr("eden.cli.cleanup_command", fake_cleanup_command)
    assert main(["cleanup", "--config", str(config_path)]) == 0
    assert calls == [Path(str(config_path))]
