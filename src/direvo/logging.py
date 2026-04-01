"""Structured logging helpers."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import TrialStatus


class JsonFormatter(logging.Formatter):
    """Render log records as JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a single log record as JSON."""
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


class ProgressFilter(logging.Filter):
    """Only pass human-readable milestone events."""

    _MILESTONE_EVENTS = {"trial_started", "trial_complete", "trial_failed"}

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether a record should be rendered as progress."""
        event = getattr(record, "event", None)
        return event in self._MILESTONE_EVENTS


class ProgressFormatter(logging.Formatter):
    """Render milestone events as compact human-readable lines."""

    def __init__(self, start_time: float, *, clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        self.start_time = start_time
        self._clock = clock or time.monotonic

    def format(self, record: logging.LogRecord) -> str:
        """Render a progress record."""
        elapsed = max(0, int(self._clock() - self.start_time))
        minutes, seconds = divmod(elapsed, 60)
        prefix = f"[{minutes:02d}:{seconds:02d}]"
        fields = getattr(record, "fields", {}) or {}
        if not isinstance(fields, dict):
            fields = {}
        event = getattr(record, "event", "")
        slot = fields.get("slot", "?")
        trial_id = fields.get("trial_id", "?")

        if event == "trial_started":
            slug = _slug_from_branch(fields.get("branch"))
            detail = f"Trial #{trial_id} started"
            if slug:
                detail += f" ({slug})"
            return f"{prefix} {detail} [slot {slot}]"

        if event == "trial_complete":
            status = str(fields.get("status", ""))
            if status == TrialStatus.SUCCESS.value:
                metrics = _format_metrics(fields.get("metrics"))
                detail = metrics or TrialStatus.SUCCESS.value
            else:
                detail = status or "unknown"
            return f"{prefix} Trial #{trial_id} complete - {detail} [slot {slot}]"

        if event == "trial_failed":
            error = str(fields.get("error", "trial execution failed"))
            return f"{prefix} Trial #{trial_id} failed - {error} [slot {slot}]"

        return ""


def configure_logging(
    log_path: Path,
    *,
    session_id: str | None = None,
    progress: bool = False,
    progress_start_time: float | None = None,
) -> logging.Logger:
    """Create the project logger.

    Args:
        log_path: Destination for the session log file.
        session_id: Optional session identifier to include in emitted events.
        progress: Whether to add a human-readable stdout progress handler.
        progress_start_time: Monotonic session start time for elapsed progress output.

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

    if progress:
        progress_handler = logging.StreamHandler(sys.stdout)
        start_time = time.monotonic() if progress_start_time is None else progress_start_time
        progress_handler.setFormatter(ProgressFormatter(start_time))
        progress_handler.addFilter(ProgressFilter())
        logger.addHandler(progress_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.session_id = session_id or uuid4().hex  # type: ignore[attr-defined]

    return logger


def _close_handlers(logger: logging.Logger) -> None:
    """Close existing handlers before replacing them."""
    for handler in list(logger.handlers):
        with suppress(ValueError):
            handler.flush()
        with suppress(ValueError):
            handler.close()


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Emit a structured event."""
    session_id = getattr(logger, "session_id", None)
    if session_id is not None and "session_id" not in fields:
        fields["session_id"] = session_id
    logger.info(event, extra={"event": event, "fields": fields})


def _slug_from_branch(branch: object) -> str:
    """Extract the proposal slug from a trial branch name."""
    if not isinstance(branch, str) or "-" not in branch:
        return ""
    return branch.split("-", 1)[1]


def _format_metrics(metrics: object) -> str:
    """Render metric fields for progress output."""
    if not isinstance(metrics, dict):
        return ""
    parts: list[str] = []
    for key, value in metrics.items():
        if value is None:
            rendered = "-"
        elif isinstance(value, float):
            rendered = f"{value:.4f}"
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return " ".join(parts)
