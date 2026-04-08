---
name: navigate-workspace
description: Explore the EDEN workspace git graph and trial branches
---

# Navigate Workspace

The workspace at `workspace/` is a git repository that serves as the base for
all trial implementations. Each trial runs in a temporary worktree branched
from a parent commit specified in the proposal.

## Key Concepts

### HEAD

The current tip of the workspace repository at planner startup. The planner
script captures this as `ctx.head_sha` and uses it as the default parent
for initial proposals. Note: HEAD advances as trials commit to the repo,
so later trials may see a different HEAD than the one captured at startup.

```bash
git -C workspace rev-parse HEAD
```

### Trial Commits

Each trial creates a commit in the workspace repository. After the trial
completes, the temporary worktree is cleaned up but the commit remains
reachable. This means you can inspect any trial's code changes.

### Parent Commit Strategy

When creating a reactive proposal, the choice of parent commit determines
what code the implementer starts from:

- **Build on the best**: use `commit_sha` from the highest-scoring trial
  to iterate on a successful approach
- **Fresh start**: use HEAD to try a completely different direction from
  the baseline
- **Build on a specific trial**: use any trial's `commit_sha` to branch
  from a particular implementation

## Useful Commands

### View the full commit graph

```bash
git -C workspace log --oneline --graph --all
```

### Compare a trial's changes against baseline

```bash
git -C workspace diff HEAD {commit_sha}
```

### See what a specific trial changed

```bash
git -C workspace show {commit_sha}
```

### Read a file from a specific trial's commit

```bash
git -C workspace show {commit_sha}:model.py
```

### List all files in a trial's commit

```bash
git -C workspace ls-tree --name-only {commit_sha}
```

## Connecting Commits to Trials

Cross-reference the git history with the results database to understand
which commits produced which results:

```sql
SELECT trial_id, commit_sha, status, r_squared
FROM trials WHERE status = 'success'
ORDER BY r_squared DESC;
```

Then inspect the best-performing code:

```bash
git -C workspace show {commit_sha}:model.py
```

## Tracing a Line of Improvement

To understand how a strategy evolved across trials:

1. Find a high-performing trial in the results database
2. Read its `parent_commits` to find what it branched from
3. Diff the two commits to see what changed
4. Read artifacts for both trials to understand the strategy evolution

```bash
# What changed between parent and child trial
git -C workspace diff {parent_sha} {child_sha}
```
