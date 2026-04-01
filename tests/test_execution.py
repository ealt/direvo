import subprocess
from pathlib import Path

from direvo.execution import (
    CommandRunner,
    CommandTimeoutError,
    EvaluationResult,
    ImplementationManager,
    ImplementationResult,
)


class TimeoutRunner(CommandRunner):
    def run(
        self, command: list[str], *, cwd: Path, timeout_sec: int
    ) -> subprocess.CompletedProcess[str]:
        raise CommandTimeoutError(command=command, timeout_sec=timeout_sec)


class StaticRunner(CommandRunner):
    def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.completed = completed
        self.calls: list[list[str]] = []

    def run(
        self, command: list[str], *, cwd: Path, timeout_sec: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
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
    manager = ImplementationManager(runner=TimeoutRunner())

    result = manager.run_implementation(
        worktree_path=tmp_path,
        slot=0,
        timeout_sec=30,
    )

    assert isinstance(result, ImplementationResult)
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
    manager = ImplementationManager(runner=StaticRunner(completed))

    result = manager.run_evaluation(
        worktree_path=tmp_path,
        evaluate_command="python3 eval.py",
        timeout_sec=30,
    )

    assert isinstance(result, EvaluationResult)
    assert not result.success
    assert result.reason == "invalid_json"
    assert result.metrics == {}


def test_implement_command_renders_template_variables(tmp_path: Path) -> None:
    manager = ImplementationManager(
        runner=StaticRunner(
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
        ),
        implement_command="claude -p {slug}",
    )

    command = manager._render_implement_command(slug="investigate-failing-tests")

    assert command == ["claude", "-p", "investigate-failing-tests"]


def test_evaluation_falls_back_to_sh_for_shell_scripts(tmp_path: Path) -> None:
    eval_script = tmp_path / "evaluate.sh"
    eval_script.write_text("printf '{\"test_pass_rate\": 1.0}\\n'\n", encoding="utf-8")
    runner = ShellFallbackRunner()
    manager = ImplementationManager(runner=runner)

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
    manager = ImplementationManager(runner=runner, implement_command="claude -p {slug}")

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: (_ for _ in ()).throw(KeyError(user)))

    assert manager.run_implementation(
        worktree_path=tmp_path,
        slot=0,
        slug="investigate",
        timeout_sec=30,
        user="trial-0",
    ) == ImplementationResult(
        success=True,
        stdout="",
        stderr="",
        returncode=0,
        reason=None,
    )


def test_run_as_user_sets_auth_and_xdg_environment_when_configured(
    monkeypatch, tmp_path: Path
) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    runner = StaticRunner(completed)
    manager = ImplementationManager(runner=runner, implement_command="codex exec --ephemeral test")

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: object())
    monkeypatch.setenv("DIREVO_AUTH_HOME", "/root")
    monkeypatch.setenv("DIREVO_RUNTIME_DIR", "/tmp/direvo-runtime")
    monkeypatch.setenv("PATH", "/usr/local/bin:/root/.codex/tmp/path/codex-arg0bad:/usr/bin")

    manager.run_implementation(
        worktree_path=tmp_path,
        slot=0,
        timeout_sec=30,
        user="trial-0",
    )

    assert runner.calls
    command = runner.calls[0]
    assert command[:4] == ["su", "trial-0", "-s", "/bin/sh"]
    assert "HOME=/tmp/direvo-runtime/trial-0/home" in command[-1]
    assert "CODEX_HOME=/tmp/direvo-runtime/trial-0/home/.codex" in command[-1]
    assert "TMPDIR=/tmp/direvo-runtime/trial-0/tmp" in command[-1]
    assert "XDG_CACHE_HOME=/tmp/direvo-runtime/trial-0/cache" in command[-1]
    assert "PATH=/usr/local/bin:/usr/bin" in command[-1]
