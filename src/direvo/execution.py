"""Subprocess helpers for execution and evaluation commands."""

from __future__ import annotations

import json
import os
import pwd
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


class CommandTimeoutError(RuntimeError):
    """Raised when a subprocess exceeds its timeout."""

    def __init__(self, *, command: list[str], timeout_sec: int) -> None:
        self.command = command
        self.timeout_sec = timeout_sec
        super().__init__(f"Command timed out after {timeout_sec}s: {shlex.join(command)}")


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome from the execution agent."""

    success: bool
    stdout: str
    stderr: str
    returncode: int
    reason: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    """Outcome from the evaluation subprocess."""

    success: bool
    stdout: str
    stderr: str
    returncode: int
    metrics: dict[str, float | int | str | None]
    reason: str | None = None


class CommandRunner:
    """Run subprocess commands with timeouts."""

    def run(
        self, command: list[str], *, cwd: Path, timeout_sec: int
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and capture output."""
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(command=command, timeout_sec=timeout_sec) from exc
        except PermissionError as exc:
            if len(command) == 1 and command[0].endswith(".sh"):
                return subprocess.run(
                    ["sh", command[0]],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            raise exc


class ExecutionManager:
    """Run execution-agent and evaluation commands."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        execution_command: str = "claude -p {direction}",
    ) -> None:
        self.runner = runner or CommandRunner()
        self.execution_command = execution_command

    def run_execution(
        self,
        *,
        worktree_path: Path,
        slot: int,
        direction: str,
        timeout_sec: int,
        user: str | None = None,
    ) -> ExecutionResult:
        """Run the execution agent."""
        command = self._render_execution_command(direction)
        try:
            completed = self._run_as_user(command, cwd=worktree_path, timeout_sec=timeout_sec, user=user)
        except CommandTimeoutError:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                returncode=-1,
                reason="timeout",
            )
        return ExecutionResult(
            success=completed.returncode == 0,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            reason=None if completed.returncode == 0 else "nonzero_exit",
        )

    def run_evaluation(
        self,
        *,
        worktree_path: Path,
        eval_script: Path,
        timeout_sec: int,
        user: str | None = None,
    ) -> EvaluationResult:
        """Run the configured evaluation script and parse JSON metrics."""
        try:
            completed = self._run_as_user([str(eval_script)], cwd=worktree_path, timeout_sec=timeout_sec, user=user)
        except CommandTimeoutError:
            return EvaluationResult(
                success=False,
                stdout="",
                stderr="",
                returncode=-1,
                metrics={},
                reason="timeout",
            )
        if completed.returncode != 0:
            return EvaluationResult(
                success=False,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                metrics={},
                reason="nonzero_exit",
            )
        try:
            metrics = json.loads(completed.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return EvaluationResult(
                success=False,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                metrics={},
                reason="invalid_json",
            )
        if not isinstance(metrics, dict):
            return EvaluationResult(
                success=False,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                metrics={},
                reason="invalid_json",
            )
        return EvaluationResult(
            success=True,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            metrics=metrics,
            reason=None,
        )

    def _run_as_user(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_sec: int,
        user: str | None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command, optionally as another user."""
        if user and os.geteuid() == 0 and _user_exists(user):
            shell_command = f"cd {shlex.quote(str(cwd))} && {shlex.join(command)}"
            return self.runner.run(
                ["su", user, "-s", "/bin/sh", "-c", shell_command],
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
        return self.runner.run(command, cwd=cwd, timeout_sec=timeout_sec)

    def _render_execution_command(self, direction: str) -> list[str]:
        """Render the configured execution command with a single prompt argument."""
        rendered: list[str] = []
        for token in shlex.split(self.execution_command):
            if token == "{direction}":
                rendered.append(direction)
            else:
                rendered.append(token.replace("{direction}", direction))
        return rendered


def _user_exists(user: str) -> bool:
    """Return whether a system user exists."""
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True
