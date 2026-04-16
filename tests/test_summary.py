from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from eden.db import DatabaseManager
from eden.models import ObjectiveDirection, ObjectiveSpec, TrialStatus, TrialUpdate
from eden.summary import render_summary


def _manager(root: Path, metrics_schema: dict[str, str]) -> DatabaseManager:
    manager = DatabaseManager(
        results_db=root / "results.db",
        proposals_db=root / "proposals.db",
        metrics_schema=metrics_schema,
        busy_timeout_ms=5000,
    )
    manager.initialize()
    return manager


def test_render_summary_reports_best_trial_and_metrics(tmp_path: Path) -> None:
    metrics_schema = {"score": "real", "attempts": "integer", "label": "text"}
    manager = _manager(tmp_path, metrics_schema)
    first = manager.reserve_trial_id()
    second = manager.reserve_trial_id()
    third = manager.reserve_trial_id()

    manager.update_trial(
        TrialUpdate(
            trial_id=first,
            status=TrialStatus.SUCCESS,
            branch="trial/1-baseline",
            metrics={"score": 0.421, "attempts": 3, "label": "baseline"},
        )
    )
    manager.update_trial(
        TrialUpdate(
            trial_id=second,
            status=TrialStatus.SUCCESS,
            branch="trial/2-fourier-features",
            metrics={"score": 0.93123, "attempts": 7, "label": "fourier"},
        )
    )
    manager.update_trial(TrialUpdate(trial_id=third, status=TrialStatus.ERROR))

    orchestrator = SimpleNamespace(
        database_manager=manager,
        config=SimpleNamespace(
            objective=ObjectiveSpec(expr="score", direction=ObjectiveDirection.MAXIMIZE),
            metrics_schema=metrics_schema,
        ),
        last_termination_reason="max_trials",
        wall_time_seconds=754.0,
    )

    rendered = render_summary(cast(Any, orchestrator))

    assert "Session complete" in rendered
    assert "Reason:   max_trials" in rendered
    assert "Duration: 12m 34s" in rendered
    assert "Trials:   2 success | 1 error" in rendered
    assert "Best trial: #2 (fourier-features)" in rendered
    assert "score        0.9312" in rendered
    assert "attempts     7" in rendered
    assert 'label        "fourier"' in rendered


def test_render_summary_handles_no_successful_trials(tmp_path: Path) -> None:
    metrics_schema = {"loss": "real"}
    manager = _manager(tmp_path, metrics_schema)
    first = manager.reserve_trial_id()
    second = manager.reserve_trial_id()

    manager.update_trial(TrialUpdate(trial_id=first, status=TrialStatus.EVAL_ERROR))
    manager.update_trial(TrialUpdate(trial_id=second, status=TrialStatus.ERROR))

    orchestrator = SimpleNamespace(
        database_manager=manager,
        config=SimpleNamespace(
            objective=ObjectiveSpec(expr="loss", direction=ObjectiveDirection.MINIMIZE),
            metrics_schema=metrics_schema,
        ),
        last_termination_reason="queue_empty",
        wall_time_seconds=9.0,
    )

    rendered = render_summary(cast(Any, orchestrator))

    assert "Trials:   0 success | 1 eval_error | 1 error" in rendered
    assert "No successful trials" in rendered
    assert "Best trial:" not in rendered


def test_render_summary_scopes_counts_and_best_trial_to_current_session(tmp_path: Path) -> None:
    metrics_schema = {"score": "real"}
    manager = _manager(tmp_path, metrics_schema)
    old_success = manager.reserve_trial_id()
    old_error = manager.reserve_trial_id()
    current_success = manager.reserve_trial_id()
    current_error = manager.reserve_trial_id()

    manager.update_trial(
        TrialUpdate(
            trial_id=old_success,
            status=TrialStatus.SUCCESS,
            branch="trial/1-old-best",
            metrics={"score": 0.99},
        )
    )
    manager.update_trial(TrialUpdate(trial_id=old_error, status=TrialStatus.EVAL_ERROR))
    manager.update_trial(
        TrialUpdate(
            trial_id=current_success,
            status=TrialStatus.SUCCESS,
            branch="trial/3-current",
            metrics={"score": 0.5},
        )
    )
    manager.update_trial(TrialUpdate(trial_id=current_error, status=TrialStatus.ERROR))

    orchestrator = SimpleNamespace(
        database_manager=manager,
        config=SimpleNamespace(
            objective=ObjectiveSpec(expr="score", direction=ObjectiveDirection.MAXIMIZE),
            metrics_schema=metrics_schema,
        ),
        session_trial_ids=[current_success, current_error],
        last_termination_reason="queue_empty",
        wall_time_seconds=3.0,
    )

    rendered = render_summary(cast(Any, orchestrator))

    assert "Trials:   1 success | 1 error" in rendered
    assert "Best trial: #3 (current)" in rendered
    assert "Best trial: #1" not in rendered
