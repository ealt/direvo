"""Minimal reproduction tests for Claude CLI auth propagation.

These tests verify that Claude auth files seeded by runtime.py land at
the exact paths that execution.py's environment prefix directs the CLI
to look.  A mismatch between these two modules is the root cause of the
"Not logged in · Please run /login" error inside Docker containers.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from eden.config import load_config
from eden.execution import ImplementationManager, ImplementationResult
from eden.runtime import RuntimeSetup, SystemRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRunner(SystemRunner):
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.existing_users: set[str] = set()

    def run(self, command: list[str]):  # noqa: ANN001
        self.commands.append(command)
        if command[:2] == ["id", "-u"]:
            return subprocess.CompletedProcess(command, 0 if command[2] in self.existing_users else 1, "", "")
        if command[0] == "useradd":
            self.existing_users.add(command[-1])
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class CapturingRunner:
    """Records the shell command passed to su so we can inspect env vars."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, command: list[str], *, cwd: Path, timeout_sec: int):  # noqa: ANN001
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")


def _write_config(tmp_path: Path) -> Path:
    experiment_root = tmp_path / "experiment"
    eden_dir = experiment_root / ".eden"
    eden_dir.mkdir(parents=True)
    config_path = eden_dir / "config.yaml"
    config_path.write_text(
        (
            'planner_root: "./planner"\n'
            'workspace: "./workspace"\n'
            "parallel_trials: 1\n"
            'evaluate_command: "echo ok"\n'
            'implement_command: "claude -p test"\n'
            "max_trials: 5\n"
            'max_wall_time: "1h"\n'
            "objective:\n"
            '  expr: "score"\n'
            '  direction: "maximize"\n'
            "metrics_schema:\n"
            "  score: real\n"
        ),
        encoding="utf-8",
    )
    (experiment_root / "planner" / "workspace").mkdir(parents=True)
    return config_path


def _populate_auth_home(auth_home: Path) -> dict[str, str]:
    """Create representative Claude auth files and return expected contents."""
    files = {}

    # HOME-relative: ~/.claude/
    claude_dir = auth_home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "credentials.json").write_text('{"oauth":"tok_abc"}', encoding="utf-8")
    files["credentials.json"] = '{"oauth":"tok_abc"}'

    # HOME-relative: ~/.config/claude/
    config_dir = auth_home / ".config" / "claude"
    config_dir.mkdir(parents=True)
    (config_dir / "settings.json").write_text('{"model":"opus"}', encoding="utf-8")
    files["settings.json"] = '{"model":"opus"}'

    # XDG_STATE_HOME: ~/.local/state/claude/
    state_dir = auth_home / ".local" / "state" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "state.db").write_text("state-data", encoding="utf-8")
    files["state.db"] = "state-data"

    # XDG_DATA_HOME: ~/.local/share/claude/
    share_dir = auth_home / ".local" / "share" / "claude"
    share_dir.mkdir(parents=True)
    (share_dir / "data.json").write_text("{}", encoding="utf-8")
    files["data.json"] = "{}"

    # XDG_CACHE_HOME: ~/.cache/claude/
    cache_dir = auth_home / ".cache" / "claude"
    cache_dir.mkdir(parents=True)
    (cache_dir / "cache.db").write_text("cached", encoding="utf-8")
    files["cache.db"] = "cached"

    # Legacy single file
    (auth_home / ".claude.json").write_text('{"legacy":1}', encoding="utf-8")
    files[".claude.json"] = '{"legacy":1}'

    return files


def _extract_env_from_su_command(su_command: list[str]) -> dict[str, str]:
    """Parse environment assignments from a captured su -c shell string."""
    shell_str = su_command[-1]
    env = {}
    for part in shell_str.split():
        if "=" in part and not part.startswith("cd"):
            key, _, value = part.partition("=")
            env[key] = value.strip("'\"")
    return env


# ---------------------------------------------------------------------------
# Test: auth seeding + env prefix alignment
# ---------------------------------------------------------------------------


class TestClaudeAuthPropagation:
    """Verify that runtime seeding and execution env vars agree on paths."""

    def test_seeded_auth_files_exist_at_env_prefix_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """End-to-end: seed auth → build env prefix → check files at resolved paths.

        This is the core reproduction for the Docker "Not logged in" error.
        If this test fails, Claude CLI cannot find its auth in the container.
        """
        config = load_config(_write_config(tmp_path))
        config.workspace_root.mkdir(parents=True, exist_ok=True)

        auth_home = tmp_path / "auth-home"
        runtime_root = tmp_path / "runtime"
        expected = _populate_auth_home(auth_home)

        # Phase 1: runtime setup seeds auth files.
        monkeypatch.setenv("EDEN_AUTH_HOME", str(auth_home))
        monkeypatch.setenv("EDEN_RUNTIME_DIR", str(runtime_root))
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)
        RuntimeSetup(FakeRunner()).prepare(config)

        # Phase 2: execution builds env prefix for trial-0.
        monkeypatch.setattr("pwd.getpwnam", lambda user: object())
        runner = CapturingRunner()
        manager = ImplementationManager(runner=runner, implement_command="claude -p test")
        manager.run_implementation(
            worktree_path=tmp_path,
            slot=0,
            timeout_sec=30,
            user="trial-0",
        )

        assert runner.calls, "Expected a su command to be captured"
        env = _extract_env_from_su_command(runner.calls[0])

        home = Path(env["HOME"])
        xdg_state = Path(env["XDG_STATE_HOME"])
        xdg_data = Path(env["XDG_DATA_HOME"])
        xdg_cache = Path(env["XDG_CACHE_HOME"])

        # Verify every auth file is present where Claude CLI will look.
        assert (home / ".claude" / "credentials.json").exists(), \
            f"Missing $HOME/.claude/credentials.json (HOME={home})"
        assert (home / ".claude" / "credentials.json").read_text() == expected["credentials.json"]

        assert (home / ".config" / "claude" / "settings.json").exists(), \
            f"Missing $HOME/.config/claude/settings.json (HOME={home})"

        assert (xdg_state / "claude" / "state.db").exists(), \
            f"Missing $XDG_STATE_HOME/claude/state.db (XDG_STATE_HOME={xdg_state})"

        assert (xdg_data / "claude" / "data.json").exists(), \
            f"Missing $XDG_DATA_HOME/claude/data.json (XDG_DATA_HOME={xdg_data})"

        assert (xdg_cache / "claude" / "cache.db").exists(), \
            f"Missing $XDG_CACHE_HOME/claude/cache.db (XDG_CACHE_HOME={xdg_cache})"

        assert (home / ".claude.json").exists(), \
            f"Missing $HOME/.claude.json (HOME={home})"

    def test_missing_auth_home_does_not_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No EDEN_AUTH_HOME set → seeding is a no-op, nothing crashes."""
        config = load_config(_write_config(tmp_path))
        config.workspace_root.mkdir(parents=True, exist_ok=True)

        runtime_root = tmp_path / "runtime"
        monkeypatch.setenv("EDEN_RUNTIME_DIR", str(runtime_root))
        monkeypatch.delenv("EDEN_AUTH_HOME", raising=False)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)

        RuntimeSetup(FakeRunner()).prepare(config)

        home = runtime_root / "trial-0" / "home"
        assert not (home / ".claude").exists()

    def test_nested_broken_symlink_in_claude_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Broken symlink inside a subdirectory (e.g. .claude/debug/latest) must not crash."""
        config = load_config(_write_config(tmp_path))
        config.workspace_root.mkdir(parents=True, exist_ok=True)

        auth_home = tmp_path / "auth-home"
        claude_dir = auth_home / ".claude"
        debug_dir = claude_dir / "debug"
        debug_dir.mkdir(parents=True)
        (claude_dir / "credentials.json").write_text('{"ok":true}', encoding="utf-8")
        # Simulate .claude/debug/latest → dangling symlink (common in Claude CLI)
        (debug_dir / "latest").symlink_to(debug_dir / "nonexistent-session")

        runtime_root = tmp_path / "runtime"
        monkeypatch.setenv("EDEN_AUTH_HOME", str(auth_home))
        monkeypatch.setenv("EDEN_RUNTIME_DIR", str(runtime_root))
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)

        RuntimeSetup(FakeRunner()).prepare(config)

        trial_root = runtime_root / "trial-0"
        assert (trial_root / "home" / ".claude" / "credentials.json").exists()
        # The broken symlink should be silently skipped, not copied.
        assert not (trial_root / "home" / ".claude" / "debug" / "latest").exists()

    def test_partial_auth_only_seeds_what_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Only ~/.claude exists on host → only that dir is seeded."""
        config = load_config(_write_config(tmp_path))
        config.workspace_root.mkdir(parents=True, exist_ok=True)

        auth_home = tmp_path / "auth-home"
        (auth_home / ".claude").mkdir(parents=True)
        (auth_home / ".claude" / "creds.json").write_text("{}", encoding="utf-8")
        # No .config/claude, no XDG dirs, no .claude.json

        runtime_root = tmp_path / "runtime"
        monkeypatch.setenv("EDEN_AUTH_HOME", str(auth_home))
        monkeypatch.setenv("EDEN_RUNTIME_DIR", str(runtime_root))
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)

        RuntimeSetup(FakeRunner()).prepare(config)

        trial_root = runtime_root / "trial-0"
        assert (trial_root / "home" / ".claude" / "creds.json").exists()
        assert not (trial_root / "home" / ".config" / "claude").exists()
        assert not (trial_root / "state" / "claude").exists()
        assert not (trial_root / "home" / ".claude.json").exists()


# ---------------------------------------------------------------------------
# Test: evaluation pipeline (reproduces eval_error without Docker)
# ---------------------------------------------------------------------------


class TestEvalPipeline:
    """Exercise the evaluate_command path with the data-fitting scripts.

    These tests import eval.py directly to reproduce eval_error failures
    without needing Claude CLI or Docker.
    """

    @pytest.fixture()
    def workspace(self, tmp_path: Path) -> Path:
        """Set up a minimal workspace with the data-fitting eval harness."""
        numpy = pytest.importorskip("numpy")

        # Copy eval data from the example.
        example_root = Path(__file__).resolve().parent.parent / "example" / "data-fitting"
        if not (example_root / "train.npz").exists():
            pytest.skip("example/data-fitting/train.npz not found")

        import shutil
        ws = tmp_path / "workspace"
        ws.mkdir()
        shutil.copy2(example_root / "train.npz", tmp_path / "train.npz")
        shutil.copy2(example_root / "test.npz", tmp_path / "test.npz")
        shutil.copy2(example_root / "eval.py", tmp_path / "eval.py")
        (ws / ".eden" / "trial").mkdir(parents=True)
        return ws

    def test_baseline_model_evaluates_successfully(self, workspace: Path, tmp_path: Path) -> None:
        """The baseline predict (mean) should evaluate without error."""
        import numpy as np

        # Write the baseline model.
        (workspace / "model.py").write_text(
            "import numpy as np\n"
            "\n"
            "def predict(X_train, y_train, X_test):\n"
            "    return np.full_like(X_test, np.mean(y_train), dtype=float)\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            ["python3", str(tmp_path / "eval.py")],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Eval failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        import json
        metrics = json.loads(result.stdout.strip())
        assert "r_squared" in metrics
        assert "rmse" in metrics

    def test_broken_model_produces_eval_error(self, workspace: Path, tmp_path: Path) -> None:
        """A model with a syntax error should cause a non-zero eval exit."""
        (workspace / "model.py").write_text(
            "def predict(X_train, y_train, X_test):\n"
            "    return UNDEFINED_VARIABLE\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            ["python3", str(tmp_path / "eval.py")],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, "Expected eval to fail on broken model"

    def test_wrong_shape_produces_eval_error(self, workspace: Path, tmp_path: Path) -> None:
        """A model returning wrong shape should cause eval to fail."""
        (workspace / "model.py").write_text(
            "import numpy as np\n"
            "\n"
            "def predict(X_train, y_train, X_test):\n"
            "    # Return scalar instead of array — shape mismatch.\n"
            "    return np.array([42.0])\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            ["python3", str(tmp_path / "eval.py")],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # eval.py exits 1 on shape mismatch
        assert result.returncode != 0, f"Expected shape-mismatch error:\nstdout: {result.stdout}\nstderr: {result.stderr}"
