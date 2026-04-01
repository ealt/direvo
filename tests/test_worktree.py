from pathlib import Path

import pytest

from eden.worktree import secure_worktree_git_metadata, secure_worktree_root


def test_secure_worktree_root_skips_restrictive_mode_when_chown_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    worktree_path = tmp_path / "wt-0"
    worktree_path.mkdir()
    original_mode = worktree_path.stat().st_mode & 0o777

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("shutil.chown", lambda path, user=None: (_ for _ in ()).throw(PermissionError()))

    secure_worktree_root(worktree_path, "trial-0")

    assert worktree_path.stat().st_mode & 0o777 == original_mode


def test_secure_worktree_root_skips_missing_user(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    worktree_path = tmp_path / "wt-0"
    worktree_path.mkdir()
    original_mode = worktree_path.stat().st_mode & 0o777

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: (_ for _ in ()).throw(KeyError(user)))

    secure_worktree_root(worktree_path, "trial-0")

    assert worktree_path.stat().st_mode & 0o777 == original_mode


def test_secure_worktree_git_metadata_exposes_shared_git_and_secures_slot_gitdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    workspace_root = tmp_path / "workspace"
    git_root = workspace_root / ".git"
    slot_gitdir = git_root / "worktrees" / "wt-0"
    other_gitdir = git_root / "worktrees" / "wt-1"
    (git_root / "objects").mkdir(parents=True)
    slot_gitdir.mkdir(parents=True)
    other_gitdir.mkdir(parents=True)
    (git_root / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (slot_gitdir / "HEAD").write_text("ref: refs/heads/trial\n", encoding="utf-8")
    (other_gitdir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: object())
    monkeypatch.setattr("shutil.chown", lambda path, user=None: None)

    secure_worktree_git_metadata(workspace_root, 0, "trial-0")

    assert git_root.stat().st_mode & 0o777 == 0o755
    assert (git_root / "HEAD").stat().st_mode & 0o777 == 0o644
    assert (git_root / "worktrees").stat().st_mode & 0o777 == 0o711
    assert slot_gitdir.stat().st_mode & 0o777 == 0o700
    assert (slot_gitdir / "HEAD").stat().st_mode & 0o777 == 0o600
    assert (other_gitdir / "HEAD").stat().st_mode & 0o777 != 0o600


def test_secure_worktree_git_metadata_ignores_vanishing_lockfiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    workspace_root = tmp_path / "workspace"
    git_root = workspace_root / ".git"
    refs_root = git_root / "refs" / "heads" / "trial"
    lock_path = refs_root / "1-smoke.lock"
    slot_gitdir = git_root / "worktrees" / "wt-0"
    refs_root.mkdir(parents=True)
    slot_gitdir.mkdir(parents=True)
    lock_path.write_text("", encoding="utf-8")

    original_chmod = Path.chmod

    def flaky_chmod(self: Path, mode: int) -> None:
        if self == lock_path:
            lock_path.unlink(missing_ok=True)
            raise FileNotFoundError(self)
        original_chmod(self, mode)

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: object())
    monkeypatch.setattr("shutil.chown", lambda path, user=None: None)
    monkeypatch.setattr(Path, "chmod", flaky_chmod)

    secure_worktree_git_metadata(workspace_root, 0, "trial-0")
