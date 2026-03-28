import subprocess
from pathlib import Path

from direvo.execution import (
    CommandRunner,
    CommandTimeoutError,
    EvaluationResult,
    ExecutionManager,
    ExecutionResult,
)


class TimeoutRunner(CommandRunner):
    def run(
        self, command: list[str], *, cwd: Path, timeout_sec: int
    ) -> subprocess.CompletedProcess[str]:
        raise CommandTimeoutError(command=command, timeout_sec=timeout_sec)


class StaticRunner(CommandRunner):
    def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.completed = completed

    def run(
        self, command: list[str], *, cwd: Path, timeout_sec: int
    ) -> subprocess.CompletedProcess[str]:
        return self.completed


class ShellFallbackRunner(CommandRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(
        self, command: list[str], *, cwd: Path, timeout_sec: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        return super().run(command, cwd=cwd, timeout_sec=timeout_sec)


def test_execution_timeout_returns_explicit_reason(tmp_path: Path) -> None:
    manager = ExecutionManager(runner=TimeoutRunner())

    result = manager.run_execution(
        worktree_path=tmp_path,
        slot=0,
        timeout_sec=30,
    )

    assert isinstance(result, ExecutionResult)
    assert not result.success
    assert result.reason == "timeout"
    assert result.returncode == -1


def test_evaluation_invalid_json_sets_reason(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(
        args=["./evaluate.sh"],
        returncode=0,
        stdout="not-json",
        stderr="",
    )
    manager = ExecutionManager(runner=StaticRunner(completed))

    result = manager.run_evaluation(
        worktree_path=tmp_path,
        evaluate_command="python3 eval.py",
        timeout_sec=30,
    )

    assert isinstance(result, EvaluationResult)
    assert not result.success
    assert result.reason == "invalid_json"
    assert result.metrics == {}


def test_execute_command_renders_template_variables(tmp_path: Path) -> None:
    manager = ExecutionManager(
        runner=StaticRunner(
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
        ),
        execute_command="claude -p {slug}",
    )

    command = manager._render_execute_command(slug="investigate-failing-tests")

    assert command == ["claude", "-p", "investigate-failing-tests"]


def test_evaluation_falls_back_to_sh_for_shell_scripts(tmp_path: Path) -> None:
    eval_script = tmp_path / "evaluate.sh"
    eval_script.write_text("printf '{\"test_pass_rate\": 1.0}\\n'\n", encoding="utf-8")
    runner = ShellFallbackRunner()
    manager = ExecutionManager(runner=runner)

    result = manager.run_evaluation(
        worktree_path=tmp_path,
        evaluate_command=str(eval_script),
        timeout_sec=30,
    )

    assert result.success
    assert result.metrics == {"test_pass_rate": 1.0}


def test_run_as_user_falls_back_to_direct_command_when_user_missing(
    monkeypatch, tmp_path: Path
) -> None:
    completed = subprocess.CompletedProcess(
        args=["claude", "-p", "Investigate"],
        returncode=0,
        stdout="",
        stderr="",
    )
    runner = StaticRunner(completed)
    manager = ExecutionManager(runner=runner, execute_command="claude -p {slug}")

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: (_ for _ in ()).throw(KeyError(user)))

    assert manager.run_execution(
        worktree_path=tmp_path,
        slot=0,
        slug="investigate",
        timeout_sec=30,
        user="trial-0",
    ) == ExecutionResult(
        success=True,
        stdout="",
        stderr="",
        returncode=0,
        reason=None,
    )
