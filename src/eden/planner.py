"""Planner integration adapters."""

from __future__ import annotations

import os
import pwd
import shlex
import subprocess
import time
from pathlib import Path


class PlannerError(RuntimeError):
    """Raised when the planner subprocess cannot be managed safely."""


class PlannerSession:
    """Base planner session."""

    def start(self) -> None:
        """Start the planner session."""
        return None

    def notify_trial_completed(self, trial_id: int) -> None:
        """Notify the planner about a completed trial."""
        return None

    def notify_error(self, message: str) -> None:
        """Notify the planner about a planner-facing runtime error."""
        return None

    def stop(self) -> None:
        """Stop the planner session."""
        return None


class NullPlannerSession(PlannerSession):
    """No-op planner session used when no plan command is configured."""


class SubprocessPlannerSession(PlannerSession):
    """Persistent planner subprocess using stdin notifications."""

    _STARTUP_STABILITY_SEC = 0.1

    def __init__(
        self,
        *,
        command: str,
        planner_root: Path,
        notify_template: str,
        startup_timeout_sec: int,
        user: str | None = "planner",
    ) -> None:
        self.command = command
        self.planner_root = planner_root
        self.notify_template = notify_template
        self.startup_timeout_sec = startup_timeout_sec
        self.user = user
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        """Start the planner subprocess and verify it stays alive."""
        if self._process is not None and self._process.poll() is None:
            return
        self._process = subprocess.Popen(
            self._planner_command(),
            cwd=self.planner_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + min(self.startup_timeout_sec, self._STARTUP_STABILITY_SEC)
        while True:
            if self._process.poll() is not None:
                details = ""
                if self._process.stderr is not None:
                    stderr = self._process.stderr.read().strip()
                    if stderr:
                        details = f": {stderr}"
                self._process = None
                raise PlannerError(f"Planner process exited during startup: {self.command}{details}")
            if time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def notify_trial_completed(self, trial_id: int) -> None:
        """Send a trial completion notification to the planner subprocess."""
        self._write_message(self.notify_template.format(trial_id=trial_id))

    def notify_error(self, message: str) -> None:
        """Send an error notification to the planner subprocess."""
        self._write_message(message)

    def _write_message(self, message: str) -> None:
        """Write one line to the planner subprocess."""
        if self._process is None or self._process.poll() is not None:
            raise PlannerError("Planner process is not running.")
        if self._process.stdin is None:
            raise PlannerError("Planner process stdin is unavailable.")
        self._process.stdin.write(f"{message}\n")
        self._process.stdin.flush()

    def stop(self) -> None:
        """Stop the planner subprocess gracefully."""
        if self._process is None:
            return
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        finally:
            self._process = None

    def _planner_command(self) -> list[str]:
        """Render the planner subprocess command."""
        command = shlex.split(self.command)
        if self.user and os.geteuid() == 0 and _user_exists(self.user):
            env_prefix = _shell_env_prefix(self.user)
            shell_command = f"cd {shlex.quote(str(self.planner_root))} && {env_prefix}{shlex.join(command)}"
            return ["su", self.user, "-s", "/bin/sh", "-c", shell_command]
        return command


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
    auth_home = os.environ.get("DIREVO_AUTH_HOME")
    runtime_root = os.environ.get("DIREVO_RUNTIME_DIR")
    if auth_home:
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


def create_planner_session(
    *, command: str | None, planner_root: Path, notify_template: str, startup_timeout_sec: int
) -> PlannerSession:
    """Create the planner session implementation for the current config."""
    if command is None:
        return NullPlannerSession()
    return SubprocessPlannerSession(
        command=command,
        planner_root=planner_root,
        notify_template=notify_template,
        startup_timeout_sec=startup_timeout_sec,
    )
