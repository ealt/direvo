---
name: write-proposal
description: Create and submit a new experiment proposal in EDEN
---

# Write Proposal

## Recommended: Use `planner_kit.create_proposal()`

The safest way to create a proposal is through the Python API, which handles
file creation and database insertion in one call:

```python
from eden.planner_kit import create_proposal

create_proposal(
    proposals_db=".eden/proposals.db",
    proposals_dir=".eden/proposals",
    priority=2.0,
    slug="polynomial-degree5",
    parent_commits=["abc123def456"],
    plan_text="Implement polynomial regression of degree 5...",
)
```

Or via the `Proposal` dataclass returned from planner callbacks (see plan.py).

## Manual Steps (when not using the Python API)

### 1. Choose a slug

The slug becomes the trial's branch name. It must be unique across all
proposals (by convention -- the schema does not enforce uniqueness, but
duplicate slugs will collide in the proposals directory).

Conventions:
- Initial proposals: descriptive name (e.g., `linear-regression`,
  `polynomial-degree3`)
- Reactive proposals: `{strategy}-{index}-t{trial_id}` (e.g.,
  `strategy-5-t3`)

### 2. Write the plan file

```bash
mkdir -p .eden/proposals/{slug}
```

Write your strategy to `.eden/proposals/{slug}/plan.md`. The implementer
agent will read this file and follow it, so be specific:

- What algorithm or approach to use
- Mathematical formulation if applicable
- What to change relative to the parent code
- Any constraints the implementer should respect

Good plan example:
> Implement polynomial regression of degree 5. Build features using
> np.vander(x, N=6). Fit with np.linalg.lstsq. This should capture both
> the quadratic and sinusoidal components of the data.

Vague plan (avoid):
> Try a better model.

### 3. Insert the database row

Use parameterized queries to avoid SQL injection:

```python
import json
import sqlite3

conn = sqlite3.connect(".eden/proposals.db")
conn.execute(
    """INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at)
       VALUES (?, ?, ?, ?, 'ready', datetime('now'))""",
    (priority, slug, json.dumps(parent_commits), f".eden/proposals/{slug}"),
)
conn.commit()
conn.close()
```

## Priority

Priority determines dispatch order (higher = first). Conventions:

- **Initial batch**: sequential integers (1.0, 2.0, 3.0, ...)
- **Reactive proposals**: base on the triggering trial's performance
  (e.g., `best_metric + 1.0`) so promising lines of inquiry get priority

## Parent Commits

The `parent_commits` field is a JSON array of commit SHAs. The trial's
worktree will branch from this commit:

- **Build on success**: use `commit_sha` from the best-performing trial
- **Fresh start**: use the workspace HEAD to try a new direction
- **Multiple parents**: supported but rarely used in practice

## Two-Step Creation (drafting protocol)

For safety, proposals can be created in two steps:

1. Insert with `status='drafting'` (invisible to orchestrator)
2. Write plan docs to the proposal directory
3. Update to `status='ready'` when docs are complete

This prevents the orchestrator from claiming a proposal before its plan
file is written. For simple proposals where the plan is written first,
inserting directly as `ready` is acceptable.
