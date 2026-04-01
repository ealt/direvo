import subprocess
from pathlib import Path

from eden.git_manager import GitManager


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _current_branch(cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> GitManager:
    (tmp_path / "tracked.txt").write_text("seed\n", encoding="utf-8")
    _run(["git", "init"], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    _run(["git", "config", "user.name", "Test User"], cwd=tmp_path)
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "seed"], cwd=tmp_path)
    return GitManager(tmp_path)


def test_initialize_worktree_resets_dirty_state(tmp_path: Path) -> None:
    git = _init_repo(tmp_path)
    worktree_path = git.ensure_worktree(0)
    (worktree_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (worktree_path / "untracked.txt").write_text("leftover\n", encoding="utf-8")

    git.initialize_worktree(0)

    assert git.status_porcelain(worktree_path) == ""
    assert not (worktree_path / "untracked.txt").exists()
    assert (worktree_path / "tracked.txt").read_text(encoding="utf-8") == "seed\n"


def test_initialize_worktree_recreates_stale_directory(tmp_path: Path) -> None:
    git = _init_repo(tmp_path)
    stale_path = tmp_path / "worktrees" / "wt-0"
    stale_path.mkdir(parents=True)
    (stale_path / "not-a-worktree.txt").write_text("stale\n", encoding="utf-8")

    worktree_path = git.initialize_worktree(0)

    assert worktree_path == stale_path
    assert git.is_git_repo_path(worktree_path)
    assert not (worktree_path / "not-a-worktree.txt").exists()


def test_remove_worktree_forces_dirty_teardown(tmp_path: Path) -> None:
    git = _init_repo(tmp_path)
    worktree_path = git.ensure_worktree(0)
    (worktree_path / "untracked.txt").write_text("leftover\n", encoding="utf-8")

    git.remove_worktree(0)

    assert not worktree_path.exists()


def test_merge_no_commit_allows_conflict_state(tmp_path: Path) -> None:
    git = _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    base_branch = _current_branch(tmp_path)

    _run(["git", "checkout", "-b", "left"], cwd=tmp_path)
    tracked.write_text("left\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], cwd=tmp_path)
    _run(["git", "commit", "-m", "left"], cwd=tmp_path)

    _run(["git", "checkout", base_branch], cwd=tmp_path)
    _run(["git", "checkout", "-b", "right"], cwd=tmp_path)
    tracked.write_text("right\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], cwd=tmp_path)
    _run(["git", "commit", "-m", "right"], cwd=tmp_path)
    right_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    worktree_path = git.ensure_worktree(0)
    git.checkout_branch(worktree_path, "left")

    git.merge_no_commit(worktree_path, right_sha)

    status = git.status_porcelain(worktree_path)
    assert "UU tracked.txt" in status
