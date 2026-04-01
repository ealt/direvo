"""Human-readable run summaries."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

from .models import TrialStatus

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


_STATUS_ORDER = (
    TrialStatus.SUCCESS,
    TrialStatus.EVAL_ERROR,
    TrialStatus.ERROR,
    TrialStatus.STARTING,
)


def print_summary(orchestrator: Orchestrator) -> None:
    """Print a human-readable session summary to stdout."""
    print(render_summary(orchestrator))


def render_summary(orchestrator: Orchestrator) -> str:
    """Render a human-readable session summary."""
    trials = orchestrator.database_manager.list_trials()
    status_counts = Counter(str(row["status"]) for row in trials)
    counts = _format_status_counts(status_counts)

    lines = [
        "-" * 50,
        "Session complete",
        f"Reason:   {orchestrator.last_termination_reason or 'unknown'}",
        f"Duration: {_format_duration(orchestrator.wall_time_seconds)}",
        f"Trials:   {counts}",
    ]

    best_trial = orchestrator.database_manager.best_trial(
        orchestrator.config.objective.expr,
        orchestrator.config.objective.direction,
    )
    if best_trial is None:
        lines.append("")
        lines.append("No successful trials")
        lines.append("-" * 50)
        return "\n".join(lines)

    trial_id = int(best_trial["trial_id"])
    slug = _slug_from_branch(best_trial["branch"])
    headline = f"Best trial: #{trial_id}"
    if slug:
        headline += f" ({slug})"
    lines.extend(["", headline])
    for metric_name, metric_type in orchestrator.config.metrics_schema.items():
        lines.append(f"  {metric_name:<12} {_format_metric_value(best_trial[metric_name], metric_type)}")
    lines.append("-" * 50)
    return "\n".join(lines)


def _format_status_counts(status_counts: Counter[str]) -> str:
    """Render trial counts in a stable order."""
    parts: list[str] = []
    for status in _STATUS_ORDER:
        count = status_counts.get(status.value, 0)
        if count == 0 and status is not TrialStatus.SUCCESS:
            continue
        parts.append(f"{count} {status.value}")
    return " | ".join(parts) or "0 success"


def _format_duration(seconds: float) -> str:
    """Render a compact wall-clock duration."""
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _format_metric_value(value: object, metric_type: str) -> str:
    """Render a metric value according to the configured SQLite type."""
    if value is None:
        return "-"
    if metric_type == "real":
        if isinstance(value, (int, float)):
            return f"{float(value):.4f}"
        return str(value)
    if metric_type == "integer":
        if isinstance(value, (int, float)):
            return str(int(value))
        return str(value)
    if metric_type == "text":
        return json.dumps(str(value))
    return str(value)


def _slug_from_branch(branch: object) -> str:
    """Extract the slug from a trial branch name."""
    if not isinstance(branch, str) or "-" not in branch:
        return ""
    return branch.split("-", 1)[1]
