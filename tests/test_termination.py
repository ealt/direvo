import tempfile
from pathlib import Path

from direvo.db import DatabaseManager
from direvo.models import ObjectiveDirection, TrialStatus, TrialUpdate
from direvo.termination import has_converged, should_terminate


def test_maximize_convergence() -> None:
    assert has_converged(
        [0.5, 0.6, 0.6, 0.6],
        direction=ObjectiveDirection.MAXIMIZE,
        window=2,
    )


def test_minimize_no_convergence() -> None:
    assert not has_converged(
        [10.0, 9.0, 8.0, 7.0],
        direction=ObjectiveDirection.MINIMIZE,
        window=2,
    )


def test_should_terminate_on_target_condition() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manager = DatabaseManager(
            results_db=root / "results.db",
            proposals_db=root / "proposals.db",
            metrics_schema={"test_pass_rate": "real"},
            busy_timeout_ms=5000,
        )
        manager.initialize()
        trial_id = manager.reserve_trial_id()
        manager.update_trial(
            TrialUpdate(
                trial_id=trial_id,
                status=TrialStatus.SUCCESS,
                metrics={"test_pass_rate": 0.95},
            )
        )

        decision = should_terminate(
            claimed_count=1,
            max_trials=10,
            elapsed_seconds=1,
            max_wall_time_seconds=3600,
            database_manager=manager,
            objective_expr="test_pass_rate",
            objective_direction=ObjectiveDirection.MAXIMIZE,
            convergence_window=None,
            target_condition="test_pass_rate >= 0.9",
        )

        assert decision.should_stop
        assert decision.reason == "target_condition"
