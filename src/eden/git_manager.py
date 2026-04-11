"""Small git subprocess wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class GitManager:
    """Run non-interactive git commands."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def is_git_repo(self) -> bool:
        """Return whether the workspace is a git repository."""
        result = self._run_git(["rev-parse", "--is-inside-work-tree"], repo_path=self.workspace_root, check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def is_git_repo_path(self, repo_path: Path) -> bool:
        """Return whether a path is a git repository or worktree."""
        result = self._run_git(["rev-parse", "--is-inside-work-tree"], repo_path=repo_path, check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def status_porcelain(self, repo_path: Path) -> str:
        """Return `git status --porcelain` output."""
        result = self._run_git(["status", "--porcelain"], repo_path=repo_path)
        return result.stdout

    def ensure_worktree(self, slot: int) -> Path:
        """Ensure the persistent worktree for a slot exists."""
        worktree_root = self.workspace_root / "worktrees"
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_path = worktree_root / f"wt-{slot}"
        if worktree_path.exists():
            return worktree_path
        self._run_git(["worktree", "add", str(worktree_path), "--detach", "HEAD"])
        return worktree_path

    def initialize_worktree(self, slot: int) -> Path:
        """Ensure a slot worktree exists and starts from a clean state."""
        worktree_path = self.workspace_root / "worktrees" / f"wt-{slot}"
        self.prune_worktrees()
        if worktree_path.exists() and not self.is_git_repo_path(worktree_path):
            shutil.rmtree(worktree_path)
        worktree_path = self.ensure_worktree(slot)
        self.abort_in_progress_git_state(worktree_path)
        self.reset_hard(worktree_path)
        self.clean_untracked(worktree_path)
        self.require_clean_status(worktree_path)
        return worktree_path

    def prune_worktrees(self) -> None:
        """Prune stale worktree metadata."""
        self._run_git(["worktree", "prune"], check=False)

    def remove_worktree(self, slot: int) -> None:
        """Remove a persistent worktree for a slot if it exists."""
        worktree_path = self.workspace_root / "worktrees" / f"wt-{slot}"
        if not worktree_path.exists():
            return
        self._run_git(["worktree", "remove", "--force", str(worktree_path)])

    def commit_exists(self, sha: str) -> bool:
        """Return whether a commit object exists in the repository."""
        result = self._run_git(["cat-file", "-t", sha], check=False)
        return result.returncode == 0 and result.stdout.strip() == "commit"

    def create_branch(self, branch_name: str, parent_sha: str) -> None:
        """Create a new branch from a parent commit."""
        self._run_git(["branch", branch_name, parent_sha])

    def checkout_branch(self, worktree_path: Path, branch_name: str) -> None:
        """Check out a branch in a worktree."""
        self._run_git(["checkout", branch_name], repo_path=worktree_path)

    def merge_no_commit(self, worktree_path: Path, other_parent: str) -> None:
        """Start a non-committing merge in the worktree."""
        result = self._run_git(
            ["merge", "--no-commit", other_parent],
            repo_path=worktree_path,
            check=False,
        )
        if result.returncode == 0:
            return
        if self._is_merge_conflict(result):
            return
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        details = stderr or stdout or f"exit code {result.returncode}"
        raise RuntimeError(f"git merge failed: {details}")

    def abort_in_progress_git_state(self, worktree_path: Path) -> None:
        """Abort merge or rebase state if present."""
        self._run_git(["merge", "--abort"], repo_path=worktree_path, check=False)
        self._run_git(["rebase", "--abort"], repo_path=worktree_path, check=False)

    def reset_hard(self, worktree_path: Path) -> None:
        """Reset a worktree to HEAD."""
        self._run_git(["reset", "--hard", "HEAD"], repo_path=worktree_path)

    def clean_untracked(self, worktree_path: Path) -> None:
        """Remove untracked files from a worktree."""
        self._run_git(["clean", "-fd"], repo_path=worktree_path)

    def commit_all(self, worktree_path: Path, message: str) -> None:
        """Stage and commit all changes in a worktree."""
        self._run_git(["add", "-A"], repo_path=worktree_path)
        env = {
            "GIT_AUTHOR_NAME": "EDEN",
            "GIT_AUTHOR_EMAIL": "eden@example.local",
            "GIT_COMMITTER_NAME": "EDEN",
            "GIT_COMMITTER_EMAIL": "eden@example.local",
        }
        self._run_git(["commit", "-m", message], repo_path=worktree_path, env=env)

    def current_head_sha(self, worktree_path: Path) -> str:
        """Return the current HEAD commit sha for a worktree."""
        result = self._run_git(["rev-parse", "HEAD"], repo_path=worktree_path)
        return result.stdout.strip()

    def require_clean_status(self, repo_path: Path) -> None:
        """Require a repository to have an empty porcelain status."""
        status = self.status_porcelain(repo_path).strip()
        if status:
            raise RuntimeError(f"Worktree is not clean after recovery: {status}")

    def _run_git(
        self,
        args: list[str],
        *,
        repo_path: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command."""
        command = ["git"]
        safe_directory = repo_path or self.workspace_root
        command.extend(["-c", f"safe.directory={safe_directory}"])
        if repo_path is not None:
            command.extend(["-C", str(repo_path)])
        command.extend(args)
        merged_env = os.environ.copy()
        if env is not None:
            merged_env.update(env)
        result = subprocess.run(
            command,
            cwd=repo_path or self.workspace_root,
            capture_output=True,
            text=True,
            check=False,
            env=merged_env,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            details = stderr or stdout or f"exit code {result.returncode}"
            raise RuntimeError(f"git command failed ({' '.join(command)}): {details}")
        return result

    def _is_merge_conflict(self, result: subprocess.CompletedProcess[str]) -> bool:
        """Return whether a failed merge exited due to ordinary content conflicts."""
        combined_output = "\n".join(part for part in [result.stdout, result.stderr] if part)
        return "Automatic merge failed; fix conflicts and then commit the result." in combined_output
