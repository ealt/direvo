import json
import logging
from pathlib import Path

from direvo.logging import configure_logging, log_event


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
