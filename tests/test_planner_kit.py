"""Tests for eden.planner_kit."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from eden.planner_kit import (
    AgentSession,
    ClaudeSession,
    PlannerContext,
    Proposal,
    configure_logging,
    connect_proposals_db,
    connect_results_db,
    create_proposal,
    get_all_trials,
    get_head_sha,
    get_trial,
    iter_trial_notifications,
    log_event,
    read_trial_artifact,
    run_planner,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_results_db(path: Path, *, metric_columns: str = "score REAL") -> None:
    """Create a minimal results database at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS trials (
            trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
            commit_sha TEXT,
            parent_commits TEXT,
            branch TEXT,
            status TEXT NOT NULL,
            artifacts_uri TEXT,
            description TEXT,
            timestamp TEXT NOT NULL,
            {metric_columns},
            CHECK (status IN ('starting', 'success', 'error', 'eval_error'))
        )
        """
    )
    conn.commit()
    conn.close()


def _create_proposals_db(path: Path) -> None:
    """Create a minimal proposals database at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
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
        """
    )
    conn.commit()
    conn.close()


def _insert_trial(path: Path, *, commit_sha: str, score: float) -> int:
    """Insert a successful trial row and return its ID."""
    conn = sqlite3.connect(path)
    cursor = conn.execute(
        "INSERT INTO trials (commit_sha, status, score, timestamp) VALUES (?, 'success', ?, datetime('now'))",
        (commit_sha, score),
    )
    trial_id = cursor.lastrowid
    conn.commit()
    conn.close()
    assert trial_id is not None
    return int(trial_id)


# ---------------------------------------------------------------------------
# get_head_sha
# ---------------------------------------------------------------------------


def test_get_head_sha() -> None:
    with mock.patch("eden.planner_kit.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(stdout="abc123def456\n")
        sha = get_head_sha("/some/workspace")
    assert sha == "abc123def456"
    mock_run.assert_called_once()
    assert mock_run.call_args[1]["cwd"] == "/some/workspace"


# ---------------------------------------------------------------------------
# connect_results_db / connect_proposals_db
# ---------------------------------------------------------------------------


def test_connect_results_db_readonly(tmp_path: Path) -> None:
    db_path = tmp_path / "results.db"
    _create_results_db(db_path)
    conn = connect_results_db(str(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO trials (status, timestamp) VALUES ('starting', datetime('now'))")
    finally:
        conn.close()


def test_connect_proposals_db_writable(tmp_path: Path) -> None:
    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    conn = connect_proposals_db(str(db_path))
    try:
        conn.execute(
            "INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at) "
            "VALUES (1.0, 'test', '[]', '/tmp', 'ready', datetime('now'))"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM proposals").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_connect_proposals_db_wal_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    conn = connect_proposals_db(str(db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_connect_results_db_nondefault_filename(tmp_path: Path) -> None:
    """Mode is correct regardless of the filename."""
    db_path = tmp_path / "my_custom_results.sqlite"
    _create_results_db(db_path)
    conn = connect_results_db(str(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO trials (status, timestamp) VALUES ('starting', datetime('now'))")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# create_proposal
# ---------------------------------------------------------------------------


def test_create_proposal(tmp_path: Path) -> None:
    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    proposals_dir = tmp_path / "proposals"

    create_proposal(
        proposals_db=str(db_path),
        proposals_dir=str(proposals_dir),
        priority=5.0,
        slug="test-slug",
        parent_commits=["abc123"],
        plan_text="Do something",
    )

    # Verify plan.md
    plan_file = proposals_dir / "test-slug" / "plan.md"
    assert plan_file.exists()
    assert plan_file.read_text() == "Do something\n"

    # Verify DB row
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM proposals WHERE slug = 'test-slug'").fetchone()
    conn.close()
    assert row is not None
    assert row["priority"] == 5.0
    assert row["status"] == "ready"
    assert json.loads(row["parent_commits"]) == ["abc123"]


# ---------------------------------------------------------------------------
# get_trial / get_all_trials
# ---------------------------------------------------------------------------


def test_get_trial_returns_all_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "results.db"
    _create_results_db(db_path)
    tid = _insert_trial(db_path, commit_sha="sha1", score=42.0)

    trial = get_trial(str(db_path), tid)
    assert trial is not None
    assert trial["trial_id"] == tid
    assert trial["commit_sha"] == "sha1"
    assert trial["score"] == 42.0
    assert "status" in trial


def test_get_trial_returns_none_for_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "results.db"
    _create_results_db(db_path)
    assert get_trial(str(db_path), 999) is None


def test_get_all_trials(tmp_path: Path) -> None:
    db_path = tmp_path / "results.db"
    _create_results_db(db_path)
    _insert_trial(db_path, commit_sha="sha1", score=10.0)
    _insert_trial(db_path, commit_sha="sha2", score=20.0)

    trials = get_all_trials(str(db_path))
    assert len(trials) == 2
    assert trials[0]["score"] == 10.0
    assert trials[1]["score"] == 20.0


def test_get_all_trials_custom_order(tmp_path: Path) -> None:
    db_path = tmp_path / "results.db"
    _create_results_db(db_path)
    _insert_trial(db_path, commit_sha="sha1", score=10.0)
    _insert_trial(db_path, commit_sha="sha2", score=20.0)

    trials = get_all_trials(str(db_path), order_by="score DESC")
    assert trials[0]["score"] == 20.0
    assert trials[1]["score"] == 10.0


# ---------------------------------------------------------------------------
# iter_trial_notifications
# ---------------------------------------------------------------------------


def test_iter_trial_notifications_parses_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        "Trial completed. ID: 1\n",
        "Trial completed. ID: 2\n",
        "Trial completed. ID: 1\n",  # duplicate
        "some other line\n",
        "Trial completed. ID: 3\n",
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(lines)))

    ids = list(iter_trial_notifications())
    assert ids == [1, 2, 3]


def test_iter_trial_notifications_handles_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        "Trial completed. ID: not_a_number\n",
        "Trial completed. ID: 5\n",
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(lines)))

    ids = list(iter_trial_notifications())
    assert ids == [5]


# ---------------------------------------------------------------------------
# configure_logging / log_event
# ---------------------------------------------------------------------------


def test_log_event_json_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDEN_LOG_DIR", str(tmp_path))
    logger = configure_logging("test_planner")

    log_event(logger, event="startup", parallel_trials=1, head="abc123")

    log_file = tmp_path / "plan.log"
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())
    assert entry == {"event": "startup", "head": "abc123", "parallel_trials": 1}


def test_configure_logging_no_log_dir() -> None:
    """Logger has no handlers when EDEN_LOG_DIR is unset."""
    logger = configure_logging("test_nolog")
    assert len(logger.handlers) == 0


def test_configure_logging_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated calls do not add duplicate handlers."""
    monkeypatch.setenv("EDEN_LOG_DIR", str(tmp_path))
    logger1 = configure_logging("test_idempotent")
    handler_count = len(logger1.handlers)
    logger2 = configure_logging("test_idempotent")
    assert logger2 is logger1
    assert len(logger2.handlers) == handler_count


# ---------------------------------------------------------------------------
# Proposal.log_fields reserved key rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reserved_key", ["event", "slug", "priority", "parent"])
def test_reserved_log_fields_rejected_for_propose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reserved_key: str
) -> None:
    """run_planner raises ValueError if initial proposal log_fields contains a propose-reserved key."""
    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    results_path = tmp_path / "results.db"
    _create_results_db(results_path)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.delenv("EDEN_LOG_DIR", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    def make_initial(ctx: PlannerContext) -> list[Proposal]:
        return [
            Proposal(
                slug="test",
                priority=1.0,
                plan_text="test",
                parent_commits=["abc"],
                log_fields={reserved_key: "bad"},
            )
        ]

    with mock.patch("eden.planner_kit.get_head_sha", return_value="abc123"), pytest.raises(
        ValueError, match="reserved keys"
    ):
        run_planner(
            make_initial_proposals=make_initial,
            make_reactive_proposal=lambda ctx, idx, t: None,
            proposals_db=str(db_path),
            results_db=str(results_path),
            proposals_dir=str(tmp_path / "proposals"),
            workspace=str(workspace),
        )


def test_reserved_log_fields_trial_id_rejected_for_react(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_planner raises ValueError if reactive proposal log_fields contains trial_id."""
    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    results_path = tmp_path / "results.db"
    _create_results_db(results_path)
    tid = _insert_trial(results_path, commit_sha="sha1", score=42.0)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.delenv("EDEN_LOG_DIR", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"Trial completed. ID: {tid}\n"))

    def make_initial(ctx: PlannerContext) -> list[Proposal]:
        return []

    def make_reactive(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal:
        return Proposal(
            slug="test",
            priority=1.0,
            plan_text="test",
            parent_commits=[trial["commit_sha"]],
            log_fields={"trial_id": tid},
        )

    with mock.patch("eden.planner_kit.get_head_sha", return_value="abc123"), pytest.raises(
        ValueError, match="reserved keys"
    ):
        run_planner(
            make_initial_proposals=make_initial,
            make_reactive_proposal=make_reactive,
            proposals_db=str(db_path),
            results_db=str(results_path),
            proposals_dir=str(tmp_path / "proposals"),
            workspace=str(workspace),
            )


# ---------------------------------------------------------------------------
# run_planner integration
# ---------------------------------------------------------------------------


def test_run_planner_counter_invariant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First reactive proposal_index equals len(initial_proposals)."""
    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    results_path = tmp_path / "results.db"
    _create_results_db(results_path)
    tid = _insert_trial(results_path, commit_sha="sha1", score=42.0)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.delenv("EDEN_LOG_DIR", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"Trial completed. ID: {tid}\n"))

    observed_indices: list[int] = []

    def make_initial(ctx: PlannerContext) -> list[Proposal]:
        return [
            Proposal(slug=f"init-{i}", priority=float(i), plan_text=f"p{i}", parent_commits=["abc"])
            for i in range(3)
        ]

    def make_reactive(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal:
        observed_indices.append(proposal_index)
        return Proposal(
            slug=f"react-{proposal_index}",
            priority=1.0,
            plan_text="react",
            parent_commits=[trial["commit_sha"]],
        )

    with mock.patch("eden.planner_kit.get_head_sha", return_value="abc123"):
        run_planner(
            make_initial_proposals=make_initial,
            make_reactive_proposal=make_reactive,
            proposals_db=str(db_path),
            results_db=str(results_path),
            proposals_dir=str(tmp_path / "proposals"),
            workspace=str(workspace),
        )

    assert observed_indices == [3]  # len(initial) == 3


def test_run_planner_counter_increments_on_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proposal_index increments even when the callback returns None."""
    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    results_path = tmp_path / "results.db"
    _create_results_db(results_path)
    tid1 = _insert_trial(results_path, commit_sha="sha1", score=10.0)
    tid2 = _insert_trial(results_path, commit_sha="sha2", score=20.0)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.delenv("EDEN_LOG_DIR", raising=False)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(f"Trial completed. ID: {tid1}\nTrial completed. ID: {tid2}\n")
    )

    observed_indices: list[int] = []

    def make_initial(ctx: PlannerContext) -> list[Proposal]:
        return [Proposal(slug="init-0", priority=0.0, plan_text="p", parent_commits=["abc"])]

    def make_reactive(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal | None:
        observed_indices.append(proposal_index)
        if trial["trial_id"] == tid1:
            return None  # skip first
        return Proposal(
            slug=f"react-{proposal_index}",
            priority=1.0,
            plan_text="react",
            parent_commits=[trial["commit_sha"]],
        )

    with mock.patch("eden.planner_kit.get_head_sha", return_value="abc123"):
        run_planner(
            make_initial_proposals=make_initial,
            make_reactive_proposal=make_reactive,
            proposals_db=str(db_path),
            results_db=str(results_path),
            proposals_dir=str(tmp_path / "proposals"),
            workspace=str(workspace),
        )

    # First reactive gets index 1 (len(initial)=1), second gets 2 even though first returned None
    assert observed_indices == [1, 2]


def test_run_planner_logs_all_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_planner produces startup, propose, notify, result, react log events."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("EDEN_LOG_DIR", str(log_dir))

    db_path = tmp_path / "proposals.db"
    _create_proposals_db(db_path)
    results_path = tmp_path / "results.db"
    _create_results_db(results_path)
    tid = _insert_trial(results_path, commit_sha="sha1", score=42.0)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"Trial completed. ID: {tid}\n"))

    def make_initial(ctx: PlannerContext) -> list[Proposal]:
        return [
            Proposal(
                slug="seed-0-init",
                priority=0.0,
                plan_text="Append seed 0",
                parent_commits=[ctx.head_sha],
                log_fields={"seed": 0},
            )
        ]

    def make_reactive(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal:
        return Proposal(
            slug=f"seed-{proposal_index}-t{trial['trial_id']}",
            priority=float(trial["score"]),
            plan_text=f"Append seed {proposal_index}",
            parent_commits=[trial["commit_sha"]],
            log_fields={"seed": proposal_index},
        )

    with mock.patch("eden.planner_kit.get_head_sha", return_value="abc123"):
        run_planner(
            make_initial_proposals=make_initial,
            make_reactive_proposal=make_reactive,
            proposals_db=str(db_path),
            results_db=str(results_path),
            proposals_dir=str(tmp_path / "proposals"),
            workspace=str(workspace),
        )

    log_file = log_dir / "plan.log"
    entries = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    events = [e["event"] for e in entries]
    assert events == ["startup", "propose", "notify", "result", "react"]

    # Check field names match E2E expectations
    startup = entries[0]
    assert startup["parallel_trials"] == 1
    assert startup["head"] == "abc123"

    propose = entries[1]
    assert propose["seed"] == 0
    assert propose["slug"] == "seed-0-init"
    assert propose["priority"] == 0.0
    assert propose["parent"] == "abc123"

    result = entries[3]
    assert result["trial_id"] == tid
    assert result["commit"] == "sha1"
    assert result["score"] == 42.0

    react = entries[4]
    assert react["seed"] == 1
    assert react["slug"] == f"seed-1-t{tid}"
    assert react["priority"] == 42.0
    assert react["parent"] == "sha1"
    assert react["trial_id"] == tid


# ---------------------------------------------------------------------------
# read_trial_artifact
# ---------------------------------------------------------------------------


def test_read_trial_artifact_returns_content(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "trial-1"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "plan.md").write_text("Do something clever\n")
    result = read_trial_artifact(str(tmp_path / "artifacts"), 1, "plan.md")
    assert result == "Do something clever"


def test_read_trial_artifact_returns_none_for_missing(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    result = read_trial_artifact(str(tmp_path / "artifacts"), 99, "plan.md")
    assert result is None


def test_read_trial_artifact_strips_whitespace(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "trial-2"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "notes.md").write_text("  some notes  \n\n")
    result = read_trial_artifact(str(tmp_path / "artifacts"), 2, "notes.md")
    assert result == "some notes"


def test_read_trial_artifact_returns_none_for_directory(tmp_path: Path) -> None:
    """Returns None when the artifact path is a directory, not a file."""
    artifact_dir = tmp_path / "artifacts" / "trial-1"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "subdir").mkdir()
    result = read_trial_artifact(str(tmp_path / "artifacts"), 1, "subdir")
    assert result is None


def test_read_trial_artifact_returns_none_for_unreadable(tmp_path: Path) -> None:
    """Returns None when the file cannot be decoded as UTF-8."""
    artifact_dir = tmp_path / "artifacts" / "trial-1"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "binary.bin").write_bytes(b"\x80\x81\x82\xff")
    result = read_trial_artifact(str(tmp_path / "artifacts"), 1, "binary.bin")
    assert result is None


def test_planner_context_read_trial_artifact(tmp_path: Path) -> None:
    """PlannerContext.read_trial_artifact delegates to the module-level function."""
    artifact_dir = tmp_path / "artifacts" / "trial-1"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "plan.md").write_text("test content\n")

    ctx = PlannerContext(
        head_sha="abc",
        parallel_trials=1,
        results_db="",
        proposals_db="",
        proposals_dir="",
        artifacts_dir=str(tmp_path / "artifacts"),
        workspace="",
        logger=configure_logging("test_ctx_artifact"),
    )
    assert ctx.read_trial_artifact(1, "plan.md") == "test content"
    assert ctx.read_trial_artifact(1, "missing.md") is None


# ---------------------------------------------------------------------------
# AgentSession / ClaudeSession
# ---------------------------------------------------------------------------


class _DummySession(AgentSession):
    """Concrete subclass for testing the base class."""

    def _build_command(self, prompt: str) -> list[str]:
        return ["echo", prompt]


def test_claude_session_append_system_prompt_file() -> None:
    s = ClaudeSession(append_system_prompt_file=Path("CLAUDE.md"))
    cmd = s._build_command("hello")
    assert cmd == ["claude", "-p", "hello", "--append-system-prompt-file", "CLAUDE.md"]


def test_claude_session_append_system_prompt() -> None:
    s = ClaudeSession(append_system_prompt="Be helpful")
    cmd = s._build_command("hello")
    assert cmd == ["claude", "-p", "hello", "--append-system-prompt", "Be helpful"]


def test_claude_session_replace_system_prompt() -> None:
    s = ClaudeSession(system_prompt="You are a bot")
    cmd = s._build_command("hello")
    assert cmd == ["claude", "-p", "hello", "--system-prompt", "You are a bot"]


def test_claude_session_replace_system_prompt_file() -> None:
    s = ClaudeSession(system_prompt_file=Path("prompt.txt"))
    cmd = s._build_command("hello")
    assert cmd == ["claude", "-p", "hello", "--system-prompt-file", "prompt.txt"]


def test_claude_session_no_system_prompt() -> None:
    s = ClaudeSession()
    cmd = s._build_command("hello")
    assert cmd == ["claude", "-p", "hello"]


def test_claude_session_continuation() -> None:
    s = ClaudeSession(append_system_prompt="Be helpful")
    # Simulate a successful first call
    s._started = True
    cmd = s._build_command("followup")
    assert cmd == ["claude", "-p", "followup", "-c"]
    assert "--append-system-prompt" not in cmd


def test_claude_session_retry_after_failure() -> None:
    """System prompt is still sent if the first call failed."""
    s = ClaudeSession(append_system_prompt_file=Path("CLAUDE.md"))
    # _started is still False (no successful call yet)
    cmd = s._build_command("retry")
    assert "--append-system-prompt-file" in cmd


def test_claude_session_rejects_multiple_prompt_options() -> None:
    with pytest.raises(ValueError, match="at most one"):
        ClaudeSession(system_prompt="x", append_system_prompt="y")


def test_agent_session_returns_none_on_timeout() -> None:
    s = _DummySession(timeout=1)
    with mock.patch("eden.planner_kit.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=1)):
        result = s.generate("hello")
    assert result is None


def test_agent_session_returns_none_on_missing_binary() -> None:
    s = _DummySession()
    with mock.patch("eden.planner_kit.subprocess.run", side_effect=FileNotFoundError):
        result = s.generate("hello")
    assert result is None


def test_agent_session_generate_success() -> None:
    s = _DummySession()
    mock_result = mock.Mock(returncode=0, stdout="response text\n", stderr="")
    with mock.patch("eden.planner_kit.subprocess.run", return_value=mock_result):
        result = s.generate("hello")
    assert result == "response text"
    assert s._started is True


def test_agent_session_generate_nonzero_exit() -> None:
    s = _DummySession()
    mock_result = mock.Mock(returncode=1, stdout="", stderr="error")
    with mock.patch("eden.planner_kit.subprocess.run", return_value=mock_result):
        result = s.generate("hello")
    assert result is None
    assert s._started is False
