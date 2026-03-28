import sqlite3
from pathlib import Path
import threading

import pytest

from direvo.db import DatabaseManager
from direvo.models import ProposalStatus, TrialStatus, TrialUpdate


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

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
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
