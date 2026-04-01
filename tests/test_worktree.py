import pytest

from eden.worktree import secure_worktree_root


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
