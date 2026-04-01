"""Planner for the seed-sum experiment.

A persistent subprocess that proposes seeds to append to the workspace.
On startup, creates an initial batch of proposals. Then listens on stdin
for trial completion notifications and creates one follow-up proposal per
completed trial, building on that trial's checkpoint.

Seed selection: seeds are assigned sequentially (0, 1, 2, ...) and never
repeated. Priority for initial proposals equals the seed value. Priority
for follow-up proposals equals the completed trial's score.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_LOG_DIR = os.environ.get("EDEN_LOG_DIR")


def _log(**fields: object) -> None:
    """Append a JSON log line to plan.log if EDEN_LOG_DIR is set."""
    if _LOG_DIR is None:
        return
    with open(os.path.join(_LOG_DIR, "plan.log"), "a") as f:
        f.write(json.dumps(fields, sort_keys=True) + "\n")
def get_head_sha(workspace: str) -> str:
    """Return the current HEAD commit SHA of the workspace repo."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def connect_db(path: str) -> sqlite3.Connection:
    """Connect to a SQLite database with the planner's access mode."""
    if Path(path).name == "results.db":
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def create_proposal(
    *,
    proposals_db: str,
    proposals_dir: str,
    priority: float,
    slug: str,
    parent_commits: list[str],
    seed: int,
) -> None:
    """Create a proposal with its plan.md and database row."""
    proposal_path = Path(proposals_dir) / slug
    proposal_path.mkdir(parents=True, exist_ok=True)
    (proposal_path / "plan.md").write_text(f"Append seed {seed}\n")

    conn = connect_db(proposals_db)
    try:
        conn.execute(
            """
            INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at)
            VALUES (?, ?, ?, ?, 'ready', datetime('now'))
            """,
            (priority, slug, json.dumps(parent_commits), str(proposal_path)),
        )
        conn.commit()
    finally:
        conn.close()


def get_trial(results_db: str, trial_id: int) -> dict | None:
    """Fetch a completed trial's commit_sha and score."""
    conn = connect_db(results_db)
    try:
        row = conn.execute(
            "SELECT trial_id, commit_sha, score FROM trials WHERE trial_id = ? AND status = 'success'",
            (trial_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def main() -> None:
    """Run the planner loop."""
    workspace = "workspace"
    parallel_trials = 1

    head_sha = get_head_sha(workspace)
    _log(event="startup", parallel_trials=parallel_trials, head=head_sha)

    proposals_db = ".eden/proposals.db"
    results_db = ".eden/results.db"
    proposals_dir = ".eden/proposals"

    initial_batch = parallel_trials + 2
    next_seed = 0

    for _ in range(initial_batch):
        slug = f"seed-{next_seed}-init"
        create_proposal(
            proposals_db=proposals_db,
            proposals_dir=proposals_dir,
            priority=float(next_seed),
            slug=slug,
            parent_commits=[head_sha],
            seed=next_seed,
        )
        _log(event="propose", seed=next_seed, slug=slug, priority=float(next_seed), parent=head_sha)
        next_seed += 1

    seen_trials: set[int] = set()

    for line in sys.stdin:
        line = line.strip()
        if not line or "Trial completed" not in line:
            continue

        try:
            trial_id = int(line.split(":")[-1].strip())
        except (ValueError, IndexError):
            continue

        _log(event="notify", trial_id=trial_id)

        if trial_id in seen_trials:
            continue
        seen_trials.add(trial_id)

        trial = get_trial(results_db, trial_id)
        if trial is None or trial["commit_sha"] is None:
            continue

        _log(event="result", trial_id=trial_id, commit=trial["commit_sha"], score=trial["score"])

        slug = f"seed-{next_seed}-t{trial_id}"
        create_proposal(
            proposals_db=proposals_db,
            proposals_dir=proposals_dir,
            priority=float(trial["score"]),
            slug=slug,
            parent_commits=[trial["commit_sha"]],
            seed=next_seed,
        )
        _log(
            event="react",
            seed=next_seed,
            slug=slug,
            priority=float(trial["score"]),
            parent=trial["commit_sha"],
            trial_id=trial_id,
        )
        next_seed += 1


if __name__ == "__main__":
    main()
