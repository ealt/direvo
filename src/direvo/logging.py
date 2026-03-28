"""Structured logging helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from uuid import uuid4


class JsonFormatter(logging.Formatter):
    """Render log records as JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = event
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, sort_keys=True)


def configure_logging(log_path: Path, *, session_id: str | None = None) -> logging.Logger:
    """Create the project logger.

    Args:
        log_path: Destination for the session log file.

    Returns:
        Configured project logger.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("direvo")
    logger.setLevel(logging.INFO)
    _close_handlers(logger)
    logger.handlers.clear()
    logger.propagate = False

    formatter = JsonFormatter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    setattr(logger, "session_id", session_id or uuid4().hex)

    return logger


def _close_handlers(logger: logging.Logger) -> None:
    """Close existing handlers before replacing them."""
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Emit a structured event."""
    session_id = getattr(logger, "session_id", None)
    if session_id is not None and "session_id" not in fields:
        fields["session_id"] = session_id
    logger.info(event, extra={"event": event, "fields": fields})
