import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from eden.db import DatabaseManager
from eden.models import ObjectiveDirection, ProposalStatus, TrialStatus, TrialUpdate


def _manager(root: Path) -> DatabaseManager:
    manager = DatabaseManager(
        results_db=root / "results.db",
        proposals_db=root / "proposals.db",
        metrics_schema={"test_pass_rate": "real", "latency_ms": "real"},
        busy_timeout_ms=5000,
    )
    manager.initialize()
    return manager


def test_reserve_trial_id_and_update(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    trial_id = manager.reserve_trial_id()
    manager.update_trial(
        TrialUpdate(
            trial_id=trial_id,
            status=TrialStatus.SUCCESS,
            commit_sha="abc123",
            branch="trial/1-smoke",
            artifacts_uri="artifacts/trial-1",
            metrics={"test_pass_rate": 0.9, "latency_ms": 10.0},
        )
    )

    row = manager.get_trial_row(trial_id)
    assert row is not None
    assert row["status"] == TrialStatus.SUCCESS.value
    assert row["commit_sha"] == "abc123"
    assert row["test_pass_rate"] == 0.9


def test_claim_ready_proposal(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_proposal(
        priority=1.0,
        slug="draft",
        parent_commits=["a"],
        artifacts_uri="p1",
        status=ProposalStatus.DRAFTING,
    )
    ready_id = manager.create_proposal(
        priority=2.0,
        slug="ready",
        parent_commits=["b"],
        artifacts_uri="p2",
        status=ProposalStatus.READY,
    )

    proposal = manager.claim_ready_proposal()
    assert proposal is not None
    assert proposal.proposal_id == ready_id
    row = manager.get_proposal_row(ready_id)
    assert row is not None
    assert row["status"] == ProposalStatus.DISPATCHED.value


def test_claim_returns_none_when_no_ready_proposals(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proposal = manager.claim_ready_proposal()
    assert proposal is None


def test_atomic_claim_under_concurrency(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proposal_id = manager.create_proposal(
        priority=1.0,
        slug="ready",
        parent_commits=["a"],
        artifacts_uri="p1",
        status=ProposalStatus.READY,
    )

    claimed_ids: list[int | None] = []
    start_barrier = threading.Barrier(3)
    result_lock = threading.Lock()

    def claim_once() -> None:
        local_manager = _manager(tmp_path)
        start_barrier.wait()
        proposal = local_manager.claim_ready_proposal()
        with result_lock:
            claimed_ids.append(None if proposal is None else proposal.proposal_id)

    threads = [threading.Thread(target=claim_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    start_barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(claimed_ids, key=lambda value: value is not None) == [None, proposal_id]


def test_database_manager_closes_connections_after_operations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def recording_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    manager = _manager(tmp_path)
    trial_id = manager.reserve_trial_id()
    manager.get_trial_row(trial_id)
    manager.claim_ready_proposal()

    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_database_manager_uses_split_journal_modes(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with sqlite3.connect(manager.results_db) as results_conn:
        results_mode = results_conn.execute("PRAGMA journal_mode").fetchone()[0]
    with sqlite3.connect(manager.proposals_db) as proposals_conn:
        proposals_mode = proposals_conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert results_mode == "delete"
    assert proposals_mode == "wal"


def test_best_trial_returns_maximize_winner(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = manager.reserve_trial_id()
    second = manager.reserve_trial_id()
    manager.update_trial(
        TrialUpdate(trial_id=first, status=TrialStatus.SUCCESS, metrics={"test_pass_rate": 0.7, "latency_ms": 10.0})
    )
    manager.update_trial(
        TrialUpdate(trial_id=second, status=TrialStatus.SUCCESS, metrics={"test_pass_rate": 0.9, "latency_ms": 20.0})
    )

    best = manager.best_trial("test_pass_rate", ObjectiveDirection.MAXIMIZE)

    assert best is not None
    assert best["trial_id"] == second


def test_best_trial_returns_minimize_winner(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = manager.reserve_trial_id()
    second = manager.reserve_trial_id()
    manager.update_trial(
        TrialUpdate(trial_id=first, status=TrialStatus.SUCCESS, metrics={"test_pass_rate": 0.7, "latency_ms": 10.0})
    )
    manager.update_trial(
        TrialUpdate(trial_id=second, status=TrialStatus.SUCCESS, metrics={"test_pass_rate": 0.9, "latency_ms": 5.0})
    )

    best = manager.best_trial("latency_ms", ObjectiveDirection.MINIMIZE)

    assert best is not None
    assert best["trial_id"] == second


def test_best_trial_returns_none_without_successful_trials(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    trial_id = manager.reserve_trial_id()
    manager.update_trial(TrialUpdate(trial_id=trial_id, status=TrialStatus.ERROR))

    assert manager.best_trial("test_pass_rate", ObjectiveDirection.MAXIMIZE) is None
