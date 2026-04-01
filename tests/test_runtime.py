import subprocess
import textwrap
from pathlib import Path

import pytest

from eden.config import load_config
from eden.runtime import RuntimeSetup, SystemRunner


class FakeRunner(SystemRunner):
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.existing_users: set[str] = set()

    def run(self, command: list[str]):  # noqa: ANN001
        self.commands.append(command)
        if command[:2] == ["id", "-u"]:
            username = command[2]
            return subprocess.CompletedProcess(command, 0 if username in self.existing_users else 1, "", "")
        if command[0] == "useradd":
            self.existing_users.add(command[-1])
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


def _write_config(tmp_path: Path) -> Path:
    experiment_root = tmp_path / "experiment"
    (experiment_root / ".eden").mkdir(parents=True)
    config_path = experiment_root / ".eden" / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            planner_root: "./planner"
            workspace: "./workspace"
            parallel_trials: 2
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
    (experiment_root / "evaluate.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return config_path


def test_runtime_setup_creates_directories_without_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    runner = FakeRunner()
    monkeypatch.setattr("os.geteuid", lambda: 1000)

    RuntimeSetup(runner).prepare(config)

    assert (tmp_path / "experiment" / "planner" / "workspace" / "worktrees").is_dir()
    assert config.proposals_dir.is_dir()
    assert config.artifacts_dir.is_dir()
    assert runner.commands == []


def test_runtime_setup_creates_users_and_applies_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    config.proposals_db.parent.mkdir(parents=True, exist_ok=True)
    git_dir = config.workspace_root / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    config.results_db.write_text("", encoding="utf-8")
    config.proposals_db.write_text("", encoding="utf-8")
    (config.proposals_dir / "proposal-1").mkdir(parents=True)
    (config.proposals_dir / "proposal-1" / "plan.md").write_text("plan\n", encoding="utf-8")
    (config.artifacts_dir / "trial-1").mkdir(parents=True)
    (config.artifacts_dir / "trial-1" / "results.md").write_text("ok\n", encoding="utf-8")

    runner = FakeRunner()
    chown_calls: list[tuple[Path, str | None, str | None]] = []
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "shutil.chown",
        lambda path, user=None, group=None: chown_calls.append((Path(path), user, group)),
    )

    RuntimeSetup(runner).prepare(config)

    assert ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "planner"] in runner.commands
    assert ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "trial-0"] in runner.commands
    assert ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "trial-1"] in runner.commands
    assert ["git", "config", "--system", "--add", "safe.directory", str(config.workspace_root)] in runner.commands
    assert config.experiment_root.stat().st_mode & 0o777 == 0o711
    assert (config.experiment_root / ".eden").stat().st_mode & 0o777 == 0o711
    assert config.planner_root.stat().st_mode & 0o777 == 0o751
    assert (config.planner_root / ".eden").stat().st_mode & 0o777 == 0o750
    assert (config.workspace_root / "worktrees").stat().st_mode & 0o777 == 0o755
    assert git_dir.stat().st_mode & 0o777 == 0o750
    assert config.proposals_dir.stat().st_mode & 0o777 == 0o770
    assert config.artifacts_dir.stat().st_mode & 0o777 == 0o750
    assert config.results_db.stat().st_mode & 0o777 == 0o640
    assert config.proposals_db.stat().st_mode & 0o777 == 0o660
    assert (config.results_db, "root", "planner") in chown_calls
    assert (config.proposals_db, "planner", "root") in chown_calls


def test_runtime_setup_tolerates_permission_denied_on_chown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    config.proposals_db.parent.mkdir(parents=True, exist_ok=True)
    git_dir = config.workspace_root / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    config.results_db.write_text("", encoding="utf-8")
    config.proposals_db.write_text("", encoding="utf-8")

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("shutil.chown", lambda path, user=None, group=None: (_ for _ in ()).throw(PermissionError()))

    RuntimeSetup(FakeRunner()).prepare(config)

    assert (config.workspace_root / "worktrees").stat().st_mode & 0o777 == 0o755
    assert config.proposals_dir.is_dir()
    assert config.artifacts_dir.is_dir()


def test_runtime_setup_raises_when_safe_directory_configuration_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    config.workspace_root.mkdir(parents=True, exist_ok=True)

    class FailingRunner(FakeRunner):
        def run(self, command: list[str]):  # noqa: ANN001
            self.commands.append(command)
            if command[:2] == ["id", "-u"]:
                username = command[2]
                return subprocess.CompletedProcess(command, 0 if username in self.existing_users else 1, "", "")
            if command[0] == "useradd":
                self.existing_users.add(command[-1])
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:4] == ["git", "config", "--system", "--add"]:
                return subprocess.CompletedProcess(command, 1, "", "git config failed")
            return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("os.geteuid", lambda: 0)

    with pytest.raises(RuntimeError, match="git config failed"):
        RuntimeSetup(FailingRunner()).prepare(config)


def test_runtime_setup_creates_user_runtime_dirs_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    config.workspace_root.mkdir(parents=True, exist_ok=True)

    runtime_root = tmp_path / "runtime"
    runner = FakeRunner()
    chown_calls: list[tuple[Path, str | None, str | None]] = []
    monkeypatch.setenv("DIREVO_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "shutil.chown",
        lambda path, user=None, group=None: chown_calls.append((Path(path), user, group)),
    )

    RuntimeSetup(runner).prepare(config)

    assert (runtime_root / "planner" / "cache").is_dir()
    assert (runtime_root / "trial-0" / "state").is_dir()
    assert (runtime_root / "trial-1" / "share").is_dir()
    assert (runtime_root / "trial-0" / "tmp").is_dir()
    assert (runtime_root / "trial-0" / "home" / ".codex").is_dir()
    assert (runtime_root / "planner" / "cache", "planner", "planner") in chown_calls


def test_runtime_setup_seeds_codex_home_from_auth_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    config.workspace_root.mkdir(parents=True, exist_ok=True)

    runtime_root = tmp_path / "runtime"
    auth_home = tmp_path / "auth-home"
    (auth_home / ".codex").mkdir(parents=True)
    (auth_home / ".codex" / "auth.json").write_text('{"token":"x"}', encoding="utf-8")

    monkeypatch.setenv("DIREVO_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setenv("DIREVO_AUTH_HOME", str(auth_home))
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)

    RuntimeSetup(FakeRunner()).prepare(config)

    copied = runtime_root / "trial-0" / "home" / ".codex" / "auth.json"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == '{"token":"x"}'


def test_runtime_setup_skips_transient_codex_tmp_from_auth_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    config.workspace_root.mkdir(parents=True, exist_ok=True)

    runtime_root = tmp_path / "runtime"
    auth_home = tmp_path / "auth-home"
    transient_dir = auth_home / ".codex" / "tmp" / "path"
    transient_dir.mkdir(parents=True)
    (transient_dir / "apply_patch").write_text("shim\n", encoding="utf-8")

    monkeypatch.setenv("DIREVO_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setenv("DIREVO_AUTH_HOME", str(auth_home))
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)

    RuntimeSetup(FakeRunner()).prepare(config)

    copied = runtime_root / "trial-0" / "home" / ".codex" / "tmp"
    assert not copied.exists()


def test_runtime_setup_skips_broken_codex_symlinks_from_auth_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    config.workspace_root.mkdir(parents=True, exist_ok=True)

    runtime_root = tmp_path / "runtime"
    auth_home = tmp_path / "auth-home"
    codex_home = auth_home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"token":"x"}', encoding="utf-8")
    (codex_home / "AGENTS.md").symlink_to(codex_home / "missing-target.md")

    monkeypatch.setenv("DIREVO_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setenv("DIREVO_AUTH_HOME", str(auth_home))
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)

    RuntimeSetup(FakeRunner()).prepare(config)

    assert (runtime_root / "trial-0" / "home" / ".codex" / "auth.json").exists()
    assert not (runtime_root / "trial-0" / "home" / ".codex" / "AGENTS.md").exists()


def test_runtime_setup_restores_existing_worktree_git_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(_write_config(tmp_path))
    existing_worktree = config.workspace_root / "worktrees" / "wt-1"
    existing_worktree.mkdir(parents=True, exist_ok=True)

    restored: list[tuple[Path, int, str]] = []
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("shutil.chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "eden.runtime.secure_worktree_git_metadata",
        lambda workspace_root, slot, user: restored.append((workspace_root, slot, user)),
    )

    RuntimeSetup(FakeRunner()).prepare(config)

    assert restored == [(config.workspace_root, 1, "trial-1")]
