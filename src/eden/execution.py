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
class ImplementationResult:
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


class ImplementationManager:
    """Run execution-agent and evaluation commands."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        implement_command: str = "echo {slug}",
    ) -> None:
        self.runner = runner or CommandRunner()
        self.implement_command = implement_command

    def run_implementation(
        self,
        *,
        worktree_path: Path,
        slot: int,
        timeout_sec: int,
        user: str | None = None,
        slug: str = "",
        trial_id: int = 0,
    ) -> ImplementationResult:
        """Run the execution agent."""
        command = self._render_implement_command(slug=slug, trial_id=trial_id)
        try:
            completed = self._run_as_user(command, cwd=worktree_path, timeout_sec=timeout_sec, user=user)
        except CommandTimeoutError:
            return ImplementationResult(
                success=False,
                stdout="",
                stderr="",
                returncode=-1,
                reason="timeout",
            )
        return ImplementationResult(
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
        evaluate_command: str,
        timeout_sec: int,
        user: str | None = None,
    ) -> EvaluationResult:
        """Run the configured evaluation command and parse JSON metrics."""
        command = shlex.split(evaluate_command)
        try:
            completed = self._run_as_user(command, cwd=worktree_path, timeout_sec=timeout_sec, user=user)
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
            shell_command = f"cd {shlex.quote(str(cwd))} && {_shell_env_prefix(user)}{shlex.join(command)}"
            return self.runner.run(
                ["su", user, "-s", "/bin/sh", "-c", shell_command],
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
        return self.runner.run(command, cwd=cwd, timeout_sec=timeout_sec)

    def _render_implement_command(self, **kwargs: object) -> list[str]:
        """Render the configured execute command with optional template variables."""
        rendered: list[str] = []
        for token in shlex.split(self.implement_command):
            for key, value in kwargs.items():
                token = token.replace(f"{{{key}}}", str(value))
            rendered.append(token)
        return rendered


def _user_exists(user: str) -> bool:
    """Return whether a system user exists."""
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


def _shell_env_prefix(user: str) -> str:
    """Render shell environment assignments for a target user command."""
    assignments: list[str] = []
    auth_home = os.environ.get("EDEN_AUTH_HOME")
    runtime_root = os.environ.get("EDEN_RUNTIME_DIR")
    if runtime_root and user.startswith("trial-"):
        user_home = Path(runtime_root) / user / "home"
        user_root = Path(runtime_root) / user
        assignments.extend(
            [
                f"HOME={shlex.quote(str(user_home))}",
                f"CODEX_HOME={shlex.quote(str(user_home / '.codex'))}",
                f"TMPDIR={shlex.quote(str(user_root / 'tmp'))}",
                f"PATH={shlex.quote(_clean_agent_path(os.environ.get('PATH', '')))}",
            ]
        )
    elif auth_home:
        assignments.append(f"HOME={shlex.quote(auth_home)}")
    if runtime_root:
        user_root = Path(runtime_root) / user
        assignments.extend(
            [
                f"XDG_STATE_HOME={shlex.quote(str(user_root / 'state'))}",
                f"XDG_CACHE_HOME={shlex.quote(str(user_root / 'cache'))}",
                f"XDG_DATA_HOME={shlex.quote(str(user_root / 'share'))}",
            ]
        )
    if not assignments:
        return ""
    return f"env {' '.join(assignments)} "


def _clean_agent_path(path_value: str) -> str:
    """Remove stale Codex helper shims from PATH before launching a fresh session."""
    parts = [part for part in path_value.split(":") if part and "/.codex/tmp/path/" not in part]
    if not parts:
        return "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return ":".join(parts)
