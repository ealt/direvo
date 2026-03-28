"""End-to-end test for the seed-sum experiment.

Uses parallel_trials=1. The planner is a real subprocess, so the exact
trial ordering depends on timing (planner may not create follow-ups
before the next claim). Assertions verify structural properties that
hold regardless of timing.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
import subprocess
from pathlib import Path

from direvo.orchestrator import Orchestrator, bootstrap


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "experiment"


def _random_int(seed: str) -> int:
    """Reproduce the deterministic mapping used by eval.py."""
    random.seed(seed)
    return random.randint(0, 100)


def _init_workspace_repo(workspace: Path) -> None:
    """Initialize the workspace as a git repo with seeds.md committed."""
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=workspace, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace, check=True, capture_output=True,
    )
    subprocess.run(["git", "add", "seeds.md"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=workspace, check=True, capture_output=True,
    )


def _read_seeds_at_commit(workspace: Path, commit_sha: str) -> list[str]:
    """Read seeds.md from a specific commit."""
    result = subprocess.run(
        ["git", "show", f"{commit_sha}:seeds.md"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_experiment(tmp_path: Path) -> None:
    """Run the seed-sum experiment end-to-end."""
    experiment_root = tmp_path / "experiment"
    shutil.copytree(FIXTURE_DIR, experiment_root)
    workspace = experiment_root / "workspace"

    _init_workspace_repo(workspace)

    config_path = experiment_root / ".direvo" / "config.yaml"
    result = bootstrap(str(config_path))
    config = result.config
    db = result.database_manager
    orchestrator = Orchestrator(config, db, result.logger)

    total_claimed = orchestrator.run()

    # --- Structural assertions ---

    assert total_claimed == config.max_trials
    assert orchestrator.last_termination_reason == "max_trials"

    trials = db.list_trials()
    successful = [t for t in trials if t["status"] == "success"]
    assert len(successful) == config.max_trials

    # Every trial has a valid commit and score
    for trial_row in successful:
        assert trial_row["commit_sha"] is not None
        assert trial_row["score"] is not None
        assert trial_row["score"] > 0

    # --- Score correctness: each score equals sum(random_int(s) for s in seeds) ---

    for trial_row in successful:
        seeds = _read_seeds_at_commit(workspace, trial_row["commit_sha"])
        expected_score = sum(_random_int(s) for s in seeds)
        assert trial_row["score"] == expected_score, (
            f"Trial {trial_row['trial_id']}: seeds={seeds}, "
            f"expected score {expected_score}, got {trial_row['score']}"
        )

    # --- Accumulation: planner builds on prior results ---

    scores = [t["score"] for t in successful]
    max_single_seed_score = max(_random_int(str(i)) for i in range(3))  # initial batch
    assert max(scores) > max_single_seed_score, (
        "Expected at least one multi-seed trial with score > any single seed"
    )

    # --- Proposals: planner created follow-up proposals ---

    proposals_db = config.proposals_db
    conn = sqlite3.connect(proposals_db)
    conn.row_factory = sqlite3.Row
    proposals = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    conn.close()

    initial_batch = config.parallel_trials + 2
    assert len(proposals) > initial_batch, (
        "Planner should have created follow-up proposals beyond the initial batch"
    )

    # Initial proposals have sequential seeds
    for i in range(initial_batch):
        assert proposals[i]["slug"] == f"seed-{i}-init"

    # Follow-up proposals reference trial IDs
    for p in proposals[initial_batch:]:
        slug = p["slug"]
        assert slug.startswith("seed-") and "-t" in slug
