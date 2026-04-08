"""Tests for the EDEN Web UI server."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from eden.web.server import (
    _detect_status,
    create_app,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def experiment_dir(tmp_path: Path) -> Path:
    """Create a minimal experiment directory layout."""
    eden_dir = tmp_path / ".eden"
    eden_dir.mkdir()
    planner_eden = tmp_path / "planner" / ".eden"
    planner_eden.mkdir(parents=True)

    # Config
    config = {
        "planner_root": "planner",
        "workspace": "workspace",
        "parallel_trials": 2,
        "max_trials": 20,
        "max_wall_time": "1h",
        "evaluate_command": "echo ok",
        "implement_command": "echo ok",
        "metrics_schema": {"score": "real", "accuracy": "real"},
        "objective": {"expr": "score", "direction": "maximize"},
    }
    (eden_dir / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

    # Results DB (DELETE journal mode)
    results_db = eden_dir / "results.db"
    conn = sqlite3.connect(str(results_db))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trials (
            trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
            commit_sha TEXT,
            parent_commits TEXT,
            branch TEXT,
            status TEXT NOT NULL,
            artifacts_uri TEXT,
            description TEXT,
            timestamp TEXT NOT NULL,
            score REAL,
            accuracy REAL,
            CHECK (status IN ('starting', 'success', 'error', 'eval_error'))
        )
    """)
    conn.execute(
        "INSERT INTO trials (status, timestamp, branch, score, accuracy) VALUES (?, datetime('now'), ?, ?, ?)",
        ("success", "trial/1-baseline", 0.85, 0.92),
    )
    conn.execute(
        "INSERT INTO trials (status, timestamp, branch, score, accuracy) VALUES (?, datetime('now'), ?, ?, ?)",
        ("error", "trial/2-experiment", None, None),
    )
    conn.commit()
    conn.close()

    # Proposals DB (WAL journal mode)
    proposals_db = planner_eden / "proposals.db"
    conn = sqlite3.connect(str(proposals_db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            priority REAL NOT NULL,
            slug TEXT NOT NULL,
            parent_commits TEXT NOT NULL,
            artifacts_uri TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (status IN ('drafting', 'ready', 'dispatched', 'completed'))
        )
    """)
    conn.execute(
        "INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (1.0, "baseline", '["abc123"]', "proposals/baseline", "completed"),
    )
    conn.commit()
    conn.close()

    # Session log
    events = [
        {"timestamp": "2026-01-01T00:00:00Z", "event": "session_started", "level": "info"},
        {"timestamp": "2026-01-01T00:01:00Z", "event": "trial_started", "trial_id": 1, "slot": 0, "level": "info"},
        {"timestamp": "2026-01-01T00:02:00Z", "event": "trial_complete", "trial_id": 1, "slot": 0, "level": "info"},
    ]
    log_lines = "\n".join(json.dumps(e) for e in events) + "\n"
    (eden_dir / "session.log").write_text(log_lines, encoding="utf-8")

    # Artifacts
    artifacts = eden_dir / "artifacts"
    trial_1 = artifacts / "trial-1"
    trial_1.mkdir(parents=True)
    (trial_1 / "plan.md").write_text("# Trial 1 Plan\n\nBaseline approach.", encoding="utf-8")
    (trial_1 / "eval_report.json").write_text('{"score": 0.85}', encoding="utf-8")

    # Proposal docs
    proposals_dir = planner_eden / "proposals"
    baseline = proposals_dir / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "plan.md").write_text("# Baseline Proposal", encoding="utf-8")

    return tmp_path


@pytest.fixture
def client(experiment_dir: Path) -> TestClient:
    """Create a test client for the experiment directory."""
    app = create_app(experiment_dir=experiment_dir)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /experiment/info
# ---------------------------------------------------------------------------


class TestInfoEndpoint:
    def test_returns_config_metadata(self, client: TestClient) -> None:
        response = client.get("/experiment/info")
        assert response.status_code == 200
        data = response.json()
        assert data["metrics_schema"] == {"score": "real", "accuracy": "real"}
        assert data["objective"] == {"expr": "score", "direction": "maximize"}
        assert data["parallel_trials"] == 2

    def test_reports_file_availability(self, client: TestClient) -> None:
        response = client.get("/experiment/info")
        data = response.json()
        files = data["files"]
        assert files["results_db"]["available"] is True
        assert files["proposals_db"]["available"] is True
        assert files["session_log"]["available"] is True
        assert files["artifacts_dir"]["available"] is True
        assert files["proposals_dir"]["available"] is True

    def test_reports_unavailable_when_missing(self, experiment_dir: Path) -> None:
        # Remove proposals.db
        (experiment_dir / "planner" / ".eden" / "proposals.db").unlink()
        wal = experiment_dir / "planner" / ".eden" / "proposals.db-wal"
        if wal.exists():
            wal.unlink()
        shm = experiment_dir / "planner" / ".eden" / "proposals.db-shm"
        if shm.exists():
            shm.unlink()

        app = create_app(experiment_dir=experiment_dir)
        client = TestClient(app)
        response = client.get("/experiment/info")
        data = response.json()
        assert data["files"]["proposals_db"]["available"] is False


# ---------------------------------------------------------------------------
# Static file serving (results.db, session.log, artifacts)
# ---------------------------------------------------------------------------


class TestStaticFileServing:
    def test_results_db(self, client: TestClient) -> None:
        response = client.get("/experiment/data/results.db")
        assert response.status_code == 200
        assert len(response.content) > 0

    def test_session_log(self, client: TestClient) -> None:
        response = client.get("/experiment/data/session.log")
        assert response.status_code == 200
        assert b"session_started" in response.content

    def test_artifact_file(self, client: TestClient) -> None:
        response = client.get("/experiment/data/artifacts/trial-1/plan.md")
        assert response.status_code == 200
        assert b"Trial 1 Plan" in response.content

    def test_artifact_json(self, client: TestClient) -> None:
        response = client.get("/experiment/data/artifacts/trial-1/eval_report.json")
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["score"] == 0.85

    def test_artifact_listing(self, client: TestClient) -> None:
        response = client.get("/experiment/data/artifacts/1/_list")
        assert response.status_code == 200
        data = response.json()
        assert "eval_report.json" in data["files"]
        assert "plan.md" in data["files"]

    def test_artifact_listing_missing_trial(self, client: TestClient) -> None:
        response = client.get("/experiment/data/artifacts/999/_list")
        assert response.status_code == 200
        assert response.json()["files"] == []

    def test_proposal_docs(self, client: TestClient) -> None:
        response = client.get("/experiment/data/proposals/baseline/plan.md")
        assert response.status_code == 200
        assert b"Baseline Proposal" in response.content

    def test_range_request_on_session_log(self, client: TestClient) -> None:
        response = client.get("/experiment/data/session.log", headers={"Range": "bytes=0-50"})
        assert response.status_code == 206
        assert len(response.content) <= 51


# ---------------------------------------------------------------------------
# proposals.db WAL snapshot route
# ---------------------------------------------------------------------------


def _load_sqlite_from_bytes(data: bytes) -> sqlite3.Connection:
    """Write response bytes to a temp file and open as SQLite."""
    fd, path = tempfile.mkstemp(suffix=".db")
    try:
        os.write(fd, data)
        os.close(fd)
        return sqlite3.connect(path)
    except Exception:
        os.unlink(path)
        raise


class TestProposalsSnapshot:
    def test_returns_valid_sqlite(self, client: TestClient, experiment_dir: Path) -> None:
        response = client.get("/experiment/data/proposals.db")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-sqlite3"

        conn = _load_sqlite_from_bytes(response.content)
        try:
            rows = conn.execute("SELECT slug, status FROM proposals").fetchall()
            assert len(rows) == 1
            assert rows[0] == ("baseline", "completed")
        finally:
            db_path = conn.execute("PRAGMA database_list").fetchone()[2]
            conn.close()
            if db_path:
                Path(db_path).unlink(missing_ok=True)

    def test_includes_wal_data(self, experiment_dir: Path) -> None:
        """Write a row after WAL checkpoint and verify the snapshot includes it."""
        proposals_db = experiment_dir / "planner" / ".eden" / "proposals.db"
        conn = sqlite3.connect(str(proposals_db))
        conn.execute(
            "INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (2.0, "new-idea", '["def456"]', "proposals/new-idea", "ready"),
        )
        conn.commit()
        conn.close()

        app = create_app(experiment_dir=experiment_dir)
        client = TestClient(app)
        response = client.get("/experiment/data/proposals.db")
        assert response.status_code == 200

        conn = _load_sqlite_from_bytes(response.content)
        try:
            rows = conn.execute("SELECT slug FROM proposals ORDER BY id").fetchall()
            assert len(rows) == 2
            assert rows[1][0] == "new-idea"
        finally:
            db_path = conn.execute("PRAGMA database_list").fetchone()[2]
            conn.close()
            if db_path:
                Path(db_path).unlink(missing_ok=True)

    def test_conditional_get_304(self, client: TestClient) -> None:
        response1 = client.get("/experiment/data/proposals.db")
        assert response1.status_code == 200
        etag = response1.headers.get("etag", "")
        assert etag

        response2 = client.get("/experiment/data/proposals.db", headers={"If-None-Match": etag})
        assert response2.status_code == 304

    def test_etag_changes_after_write(self, experiment_dir: Path) -> None:
        app = create_app(experiment_dir=experiment_dir)
        client = TestClient(app)

        response1 = client.get("/experiment/data/proposals.db")
        etag1 = response1.headers.get("etag", "")

        # Write a new row.
        proposals_db = experiment_dir / "planner" / ".eden" / "proposals.db"
        conn = sqlite3.connect(str(proposals_db))
        conn.execute(
            "INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (3.0, "another", '["ghi789"]', "proposals/another", "drafting"),
        )
        conn.commit()
        conn.close()

        response2 = client.get("/experiment/data/proposals.db")
        etag2 = response2.headers.get("etag", "")
        assert etag2 != etag1

    def test_head_request(self, client: TestClient) -> None:
        response = client.head("/experiment/data/proposals.db")
        assert response.status_code == 200
        assert response.headers.get("etag")
        assert response.headers.get("content-type") == "application/x-sqlite3"

    def test_404_when_missing(self, experiment_dir: Path) -> None:
        (experiment_dir / "planner" / ".eden" / "proposals.db").unlink()
        wal = experiment_dir / "planner" / ".eden" / "proposals.db-wal"
        if wal.exists():
            wal.unlink()
        shm = experiment_dir / "planner" / ".eden" / "proposals.db-shm"
        if shm.exists():
            shm.unlink()

        app = create_app(experiment_dir=experiment_dir)
        client = TestClient(app)
        response = client.get("/experiment/data/proposals.db")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Liveness heuristic
# ---------------------------------------------------------------------------


class TestLivenessDetection:
    def test_ended_when_session_ended_present(self, experiment_dir: Path) -> None:
        log_path = experiment_dir / ".eden" / "session.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "session_ended", "reason": "max_trials"}) + "\n")
        assert _detect_status(log_path) == "ended"

    def test_live_when_recently_modified(self, experiment_dir: Path) -> None:
        log_path = experiment_dir / ".eden" / "session.log"
        # Touch the file to make it recent.
        log_path.touch()
        assert _detect_status(log_path) == "live"

    def test_unknown_when_stale(self, tmp_path: Path) -> None:
        log_path = tmp_path / "session.log"
        log_path.write_text(json.dumps({"event": "trial_started"}) + "\n", encoding="utf-8")
        # Set mtime to 10 minutes ago.

        old_time = time.time() - 600
        os.utime(log_path, (old_time, old_time))
        assert _detect_status(log_path) == "unknown"

    def test_unknown_when_missing(self, tmp_path: Path) -> None:
        assert _detect_status(tmp_path / "nonexistent.log") == "unknown"

    def test_info_endpoint_reports_status(self, client: TestClient) -> None:
        response = client.get("/experiment/info")
        data = response.json()
        assert data["status"] in ("live", "ended", "unknown")


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_minimal_experiment_dir(self, tmp_path: Path) -> None:
        """An experiment dir with only results.db and config.yaml should work."""
        eden_dir = tmp_path / ".eden"
        eden_dir.mkdir()

        config = {
            "metrics_schema": {"score": "real"},
            "objective": {"expr": "score", "direction": "maximize"},
        }
        (eden_dir / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

        results_db = eden_dir / "results.db"
        conn = sqlite3.connect(str(results_db))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("""
            CREATE TABLE trials (
                trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                score REAL
            )
        """)
        conn.commit()
        conn.close()

        app = create_app(experiment_dir=tmp_path)
        client = TestClient(app)

        # Info works.
        response = client.get("/experiment/info")
        assert response.status_code == 200
        data = response.json()
        assert data["files"]["proposals_db"]["available"] is False
        assert data["files"]["session_log"]["available"] is False

        # results.db still served.
        response = client.get("/experiment/data/results.db")
        assert response.status_code == 200


