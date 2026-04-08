import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from eden.cli import doctor, main


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
