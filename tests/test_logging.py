import json
import logging
from pathlib import Path

import pytest

from direvo.logging import ProgressFormatter, configure_logging, log_event


def _record(event: str, **fields: object) -> logging.LogRecord:
    record = logging.LogRecord("direvo", logging.INFO, __file__, 1, event, (), None)
    record.event = event
    record.fields = fields
    return record


def test_log_event_includes_session_id(tmp_path: Path) -> None:
    log_path = tmp_path / "session.log"
    logger = configure_logging(log_path, session_id="session-123")

    log_event(logger, "trial_started", trial_id=1, slot=0)

    payload = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["event"] == "trial_started"
    assert payload["session_id"] == "session-123"
    assert payload["trial_id"] == 1
    assert payload["slot"] == 0


def test_configure_logging_generates_session_id(tmp_path: Path) -> None:
    log_path = tmp_path / "session.log"
    logger = configure_logging(log_path)

    log_event(logger, "session_started")

    payload = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["event"] == "session_started"
    assert isinstance(payload["session_id"], str)
    assert payload["session_id"]


def test_configure_logging_closes_replaced_file_handlers(tmp_path: Path) -> None:
    first_log = tmp_path / "first.log"
    second_log = tmp_path / "second.log"

    logger = configure_logging(first_log)
    old_file_handlers = [handler for handler in logger.handlers if isinstance(handler, logging.FileHandler)]

    logger = configure_logging(second_log)

    assert len(old_file_handlers) == 1
    assert old_file_handlers[0].stream is None


def test_progress_formatter_formats_started_completed_and_failed_events() -> None:
    formatter = ProgressFormatter(100.0, clock=lambda: 132.0)

    started = formatter.format(_record("trial_started", trial_id=1, slot=0, branch="trial/1-linear-regression"))
    completed = formatter.format(
        _record(
            "trial_complete",
            trial_id=1,
            slot=0,
            status="success",
            metrics={"r_squared": 0.421, "rmse": 0.3812},
        )
    )
    failed = formatter.format(_record("trial_failed", trial_id=2, slot=1, error="timeout"))

    assert started == "[00:32] Trial #1 started (linear-regression) [slot 0]"
    assert completed == "[00:32] Trial #1 complete - r_squared=0.4210 rmse=0.3812 [slot 0]"
    assert failed == "[00:32] Trial #2 failed - timeout [slot 1]"


def test_progress_formatter_uses_status_for_non_successful_completion() -> None:
    formatter = ProgressFormatter(0.0, clock=lambda: 132.0)

    rendered = formatter.format(_record("trial_complete", trial_id=3, slot=2, status="eval_error", metrics={}))

    assert rendered == "[02:12] Trial #3 complete - eval_error [slot 2]"


def test_configure_logging_progress_handler_filters_non_milestone_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("direvo.logging.time.monotonic", lambda: 100.0)
    log_path = tmp_path / "session.log"
    logger = configure_logging(log_path, progress=True, progress_start_time=0.0)

    log_event(logger, "session_started", workspace_root="/tmp/workspace")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"event": "session_started"' in captured.err
