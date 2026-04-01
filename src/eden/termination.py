"""Termination and convergence helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .db import DatabaseManager
from .models import ObjectiveDirection


@dataclass(frozen=True)
class TerminationDecision:
    """Termination evaluation result."""

    should_stop: bool
    reason: str | None = None


def has_converged(
    values: list[float], *, direction: ObjectiveDirection, window: int | None
) -> bool:
    """Return whether the recent objective window shows no improvement."""
    if window is None or window <= 0:
        return False
    if len(values) < window * 2:
        return False
    previous = values[:-window]
    recent = values[-window:]
    if direction is ObjectiveDirection.MAXIMIZE:
        return max(recent) <= max(previous)
    return min(recent) >= min(previous)


def should_terminate(
    *,
    claimed_count: int,
    max_trials: int,
    elapsed_seconds: float,
    max_wall_time_seconds: int,
    database_manager: DatabaseManager,
    objective_expr: str,
    objective_direction: ObjectiveDirection,
    convergence_window: int | None,
    target_condition: str | None,
) -> TerminationDecision:
    """Evaluate whether the session should stop dispatching new work."""
    if claimed_count >= max_trials:
        return TerminationDecision(True, "max_trials")
    if elapsed_seconds >= max_wall_time_seconds:
        return TerminationDecision(True, "max_wall_time")
    if target_condition and database_manager.target_condition_met(target_condition):
        return TerminationDecision(True, "target_condition")
    objective_values = database_manager.objective_values(objective_expr)
    if has_converged(
        objective_values,
        direction=objective_direction,
        window=convergence_window,
    ):
        return TerminationDecision(True, "convergence")
    return TerminationDecision(False, None)
