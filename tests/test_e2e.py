"""End-to-end test for the seed-sum experiment.

Uses parallel_trials=1. The planner is a real subprocess, so the exact
trial ordering depends on timing (planner may not create follow-ups
before the next claim). Assertions verify structural properties that
hold regardless of timing.
"""

from __future__ import annotations

import json
import random
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from eden.orchestrator import Orchestrator, bootstrap

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
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "seeds.md"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=workspace,
        check=True,
        capture_output=True,
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


def _parse_log(path: Path) -> list[dict[str, object]]:
    """Parse a JSONL log file into a list of event dicts."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _verify_plan_log(
    entries: list[dict[str, object]],
    *,
    initial_batch: int,
    max_trials: int,
    trials_by_id: dict[int, dict[str, object]],
) -> None:
    """Verify structural properties of plan.py's log."""
    by_event: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        by_event.setdefault(str(entry["event"]), []).append(entry)

    startups = by_event.get("startup", [])
    proposes = by_event.get("propose", [])
    notifies = by_event.get("notify", [])
    results = by_event.get("result", [])
    reacts = by_event.get("react", [])

    # Exactly one startup event
    assert len(startups) == 1
    head = startups[0]["head"]
    assert isinstance(head, str)
    assert len(head) == 40

    # Initial proposals: exact count and deterministic content
    assert len(proposes) == initial_batch
    for i, p in enumerate(proposes):
        assert p["seed"] == i
        assert p["slug"] == f"seed-{i}-init"
        assert p["priority"] == float(i)
        assert p["parent"] == head

    # Results and reacts are paired (one react per result)
    assert len(results) == len(reacts)
    n_reactive = len(results)
    assert 0 < n_reactive <= max_trials

    # Every processed trial was notified
    result_tids = {r["trial_id"] for r in results}
    notified_tids = {n["trial_id"] for n in notifies}
    assert result_tids <= notified_tids

    # Seeds are globally sequential across initial + reactive proposals
    all_seeds = [p["seed"] for p in proposes] + [r["seed"] for r in reacts]
    assert all_seeds == list(range(len(all_seeds)))

    # React entries match results.db
    for result_entry, react_entry in zip(results, reacts, strict=True):
        tid = result_entry["trial_id"]
        assert tid in trials_by_id, f"result references unknown trial {tid}"
        trial = trials_by_id[tid]
        assert result_entry["commit"] == trial["commit_sha"]
        assert result_entry["score"] == trial["score"]
        assert react_entry["priority"] == float(trial["score"])
        assert react_entry["parent"] == trial["commit_sha"]
        assert react_entry["trial_id"] == tid

    # Temporal ordering: all initial proposes before any reactive events
    event_list = [str(e["event"]) for e in entries]
    last_propose = max(i for i, t in enumerate(event_list) if t == "propose")
    first_reactive = min(
        (i for i, t in enumerate(event_list) if t in ("notify", "result", "react")),
        default=len(entries),
    )
    assert last_propose < first_reactive

    # Within each reactive group: notify < result < react for same trial_id
    for result_entry in results:
        tid = result_entry["trial_id"]
        notify_pos = next(
            i for i, e in enumerate(entries) if e["event"] == "notify" and e.get("trial_id") == tid
        )
        result_pos = next(
            i for i, e in enumerate(entries) if e["event"] == "result" and e.get("trial_id") == tid
        )
        react_pos = next(
            i for i, e in enumerate(entries) if e["event"] == "react" and e.get("trial_id") == tid
        )
        assert notify_pos < result_pos < react_pos, (
            f"Trial {tid}: expected notify({notify_pos}) < result({result_pos}) < react({react_pos})"
        )


def _verify_execute_log(
    entries: list[dict[str, object]],
    *,
    max_trials: int,
    trials: list[dict[str, object]],
    workspace: Path,
) -> None:
    """Verify structural properties of execute.py's log."""
    assert len(entries) == max_trials
    assert all(e["event"] == "execute" for e in entries)

    for entry in entries:
        seed = cast(str, entry["seed"])
        assert isinstance(seed, str)
        assert seed.isdigit()
        # Append invariant: seeds_after == seeds_before + [seed]
        seeds_before = cast(list[str], entry["seeds_before"])
        seeds_after = cast(list[str], entry["seeds_after"])
        assert seeds_after == seeds_before + [seed]

    # Cross-check: each trial's committed seeds match exactly one execute entry
    matched: set[int] = set()
    for trial_row in trials:
        seeds = _read_seeds_at_commit(workspace, cast(str, trial_row["commit_sha"]))
        matches = [i for i, e in enumerate(entries) if e["seeds_after"] == seeds]
        assert len(matches) == 1, (
            f"Trial {trial_row['trial_id']}: expected one execute entry for seeds {seeds}, found {len(matches)}"
        )
        matched.add(matches[0])
    assert len(matched) == max_trials


def _verify_eval_log(
    entries: list[dict[str, object]],
    *,
    max_trials: int,
    trials: list[dict[str, object]],
    workspace: Path,
) -> None:
    """Verify structural properties of eval.py's log."""
    assert len(entries) == max_trials
    assert all(e["event"] == "eval" for e in entries)

    for entry in entries:
        seeds = cast(list[str], entry["seeds"])
        values = cast(list[int], entry["values"])
        score = cast(int, entry["score"])
        assert len(seeds) == len(values)
        # Each value matches the deterministic random mapping
        for s, v in zip(seeds, values, strict=True):
            assert v == _random_int(s), f"seed {s!r}: expected {_random_int(s)}, got {v}"
        # Score is the sum of individual values
        assert score == sum(values)

    # Cross-check: each trial's committed seeds match exactly one eval entry with correct score
    for trial_row in trials:
        seeds = _read_seeds_at_commit(workspace, cast(str, trial_row["commit_sha"]))
        matches = [e for e in entries if e["seeds"] == seeds]
        assert len(matches) == 1, (
            f"Trial {trial_row['trial_id']}: expected one eval entry for seeds {seeds}, found {len(matches)}"
        )
        assert matches[0]["score"] == trial_row["score"]


def test_experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the seed-sum experiment end-to-end."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("EDEN_LOG_DIR", str(log_dir))

    experiment_root = tmp_path / "experiment"
    shutil.copytree(FIXTURE_DIR, experiment_root)
    workspace = experiment_root / "planner" / "workspace"

    # Patch config to use the current venv python (system python3 may lack deps)
    config_path = experiment_root / ".eden" / "config.yaml"
    config_text = config_path.read_text().replace("python3", sys.executable)
    config_path.write_text(config_text)

    _init_workspace_repo(workspace)
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
            f"Trial {trial_row['trial_id']}: seeds={seeds}, expected score {expected_score}, got {trial_row['score']}"
        )

    # --- Accumulation: planner builds on prior results ---

    scores = [t["score"] for t in successful]
    max_single_seed_score = max(_random_int(str(i)) for i in range(3))  # initial batch
    assert max(scores) > max_single_seed_score, "Expected at least one multi-seed trial with score > any single seed"

    # --- Proposals: planner created follow-up proposals ---

    proposals_db = config.proposals_db
    conn = sqlite3.connect(proposals_db)
    conn.row_factory = sqlite3.Row
    proposals = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    conn.close()

    initial_batch = config.parallel_trials + 2
    assert len(proposals) > initial_batch, "Planner should have created follow-up proposals beyond the initial batch"

    # Initial proposals have sequential seeds
    for i in range(initial_batch):
        assert proposals[i]["slug"] == f"seed-{i}-init"

    # Follow-up proposals reference trial IDs
    for p in proposals[initial_batch:]:
        slug = p["slug"]
        assert slug.startswith("seed-")
        assert "-t" in slug

    # --- Log verification ---

    successful_dicts = [dict(row) for row in successful]
    trials_by_id = {int(t["trial_id"]): t for t in successful_dicts}

    _verify_plan_log(
        _parse_log(log_dir / "plan.log"),
        initial_batch=initial_batch,
        max_trials=config.max_trials,
        trials_by_id=trials_by_id,
    )

    _verify_execute_log(
        _parse_log(log_dir / "execute.log"),
        max_trials=config.max_trials,
        trials=successful_dicts,
        workspace=workspace,
    )

    _verify_eval_log(
        _parse_log(log_dir / "eval.log"),
        max_trials=config.max_trials,
        trials=successful_dicts,
        workspace=workspace,
    )
