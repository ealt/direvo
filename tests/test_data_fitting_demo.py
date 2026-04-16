"""Tests for the data-fitting demo.

Always-run tests verify eval.py math and baseline model correctness.
Integration test (gated by EDEN_RUN_AI_DEMO=1) runs the full orchestrator
with real AI tools.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

DEMO_DIR = Path(__file__).parent.parent / "example" / "data-fitting"

# eval.py's module-level code doesn't import model (that happens in main()),
# so this is safe outside a worktree context.
sys.path.insert(0, str(DEMO_DIR))
sys.path.insert(0, str(DEMO_DIR / "planner" / "workspace"))

from eval import r_squared, rmse  # type: ignore[import-not-found]  # noqa: E402, I001
from model import predict  # type: ignore[import-not-found]  # noqa: E402, I001


# ---------------------------------------------------------------------------
# eval.py math
# ---------------------------------------------------------------------------


class TestRSquared:
    def test_perfect(self) -> None:
        y = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert r_squared(y, y) == 1.0

    def test_mean_prediction(self) -> None:
        y_true = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y_pred = np.full_like(y_true, np.mean(y_true))
        assert abs(r_squared(y_true, y_pred)) < 1e-10

    def test_negative(self) -> None:
        y_true = np.array([1.0, 2.0, 3.0])
        y_pred = np.array([10.0, 10.0, 10.0])
        assert r_squared(y_true, y_pred) < 0.0


class TestRMSE:
    def test_perfect(self) -> None:
        y = np.array([1.0, 2.0, 3.0])
        assert rmse(y, y) == 0.0

    def test_known_error(self) -> None:
        y_true = np.zeros(3)
        y_pred = np.ones(3)
        assert abs(rmse(y_true, y_pred) - 1.0) < 1e-10


# ---------------------------------------------------------------------------
# Baseline model
# ---------------------------------------------------------------------------


class TestBaselineModel:
    def test_predicts_mean(self) -> None:
        X_train = np.array([1.0, 2.0, 3.0])
        y_train = np.array([10.0, 20.0, 30.0])
        X_test = np.array([4.0, 5.0])
        preds = predict(X_train, y_train, X_test)
        assert len(preds) == 2
        np.testing.assert_allclose(preds, 20.0)


# ---------------------------------------------------------------------------
# End-to-end eval with baseline model
# ---------------------------------------------------------------------------


class TestEvalEndToEnd:
    def test_baseline_model_scores_near_zero(self, tmp_path: Path) -> None:
        """Run eval.py against the baseline model and committed data."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        shutil.copy(DEMO_DIR / "planner" / "workspace" / "model.py", workspace / "model.py")
        result = subprocess.run(
            [sys.executable, str(DEMO_DIR / "eval.py")],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        metrics = json.loads(result.stdout)
        assert "r_squared" in metrics
        assert "rmse" in metrics
        assert -0.1 < metrics["r_squared"] < 0.1
        assert metrics["rmse"] > 0

    def test_eval_writes_report_artifact(self, tmp_path: Path) -> None:
        """Eval should write a detailed report to .eden/trial/eval_report.json."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".eden" / "trial").mkdir(parents=True)
        shutil.copy(DEMO_DIR / "planner" / "workspace" / "model.py", workspace / "model.py")
        result = subprocess.run(
            [sys.executable, str(DEMO_DIR / "eval.py")],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        report_path = workspace / ".eden" / "trial" / "eval_report.json"
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert report["n_test"] == 50
        assert "residual_mean" in report
        assert "residual_std" in report

    def test_missing_model_exits_nonzero(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(DEMO_DIR / "eval.py")],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Data generation determinism
# ---------------------------------------------------------------------------


class TestDataGeneration:
    def test_deterministic(self, tmp_path: Path) -> None:
        gen_script = DEMO_DIR / "generate_data.py"
        for run_dir in ("run1", "run2"):
            d = tmp_path / run_dir
            d.mkdir()
            shutil.copy(gen_script, d / "generate_data.py")
            subprocess.run([sys.executable, "generate_data.py"], cwd=d, check=True, capture_output=True)
        train1 = np.load(tmp_path / "run1" / "train.npz")
        train2 = np.load(tmp_path / "run2" / "train.npz")
        np.testing.assert_array_equal(train1["x"], train2["x"])
        np.testing.assert_array_equal(train1["y"], train2["y"])


# ---------------------------------------------------------------------------
# Data-fitting planner unit test
# ---------------------------------------------------------------------------


class TestDataFittingPlanner:
    def test_reactive_proposal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Refactored data-fitting planner builds reactive proposals correctly."""
        from unittest import mock

        from eden.planner_kit import PlannerContext, configure_logging

        # Set up artifacts for a trial
        artifacts = tmp_path / "artifacts" / "trial-1"
        artifacts.mkdir(parents=True)
        (artifacts / "plan.md").write_text("Linear regression\n")
        (artifacts / "notes.md").write_text("Implemented basic linear fit\n")

        ctx = PlannerContext(
            head_sha="abc123",
            parallel_trials=3,
            results_db=str(tmp_path / "results.db"),
            proposals_db=str(tmp_path / "proposals.db"),
            proposals_dir=str(tmp_path / "proposals"),
            artifacts_dir=str(tmp_path / "artifacts"),
            workspace=str(tmp_path / "workspace"),
            logger=configure_logging("test_df_planner"),
        )

        trial = {
            "trial_id": 1,
            "commit_sha": "sha1",
            "r_squared": 0.85,
            "rmse": 1.5,
            "status": "success",
        }
        all_trials = [trial]

        # Import the planner module and patch its session + ctx methods
        sys.path.insert(0, str(DEMO_DIR))
        try:
            import importlib

            # Reimport to get fresh module
            import plan as planner_mod  # type: ignore[import-not-found]

            importlib.reload(planner_mod)

            mock_generate = mock.patch.object(
                planner_mod.session, "generate", return_value="Try polynomial degree 7"
            )
            with (
                mock.patch.object(ctx, "get_all_trials", return_value=all_trials),
                mock.patch.object(planner_mod, "_current_session_trials", return_value=all_trials),
                mock_generate as gen_mock,
            ):
                proposal = planner_mod._make_reactive_proposal(ctx, 5, trial)

            assert proposal.slug == "strategy-5-t1"
            assert proposal.plan_text == "Try polynomial degree 7"
            assert proposal.parent_commits == ["sha1"]
            assert proposal.priority == pytest.approx(1.85)

            # Verify prompt passed to generate() contains trial metrics and artifact content
            prompt_arg = gen_mock.call_args[0][0]
            assert "R²=0.8500" in prompt_arg
            assert "RMSE=1.5000" in prompt_arg
            assert "Implemented basic linear fit" in prompt_arg

            # Test fallback when session.generate returns None
            with (
                mock.patch.object(ctx, "get_all_trials", return_value=all_trials),
                mock.patch.object(planner_mod, "_current_session_trials", return_value=all_trials),
                mock.patch.object(planner_mod.session, "generate", return_value=None),
            ):
                proposal = planner_mod._make_reactive_proposal(ctx, 6, trial)

            assert "Improve on the best approach" in proposal.plan_text
        finally:
            sys.path.pop(0)
            if "plan" in sys.modules:
                del sys.modules["plan"]

    def test_reactive_proposal_ignores_stale_trials_from_previous_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest import mock

        from eden.planner_kit import PlannerContext, configure_logging

        artifacts = tmp_path / "artifacts" / "trial-1"
        artifacts.mkdir(parents=True)
        (artifacts / "notes.md").write_text("Implemented current-session fit\n")

        ctx = PlannerContext(
            head_sha="abc123",
            parallel_trials=3,
            results_db=str(tmp_path / "results.db"),
            proposals_db=str(tmp_path / "proposals.db"),
            proposals_dir=str(tmp_path / "proposals"),
            artifacts_dir=str(tmp_path / "artifacts"),
            workspace=str(tmp_path / "workspace"),
            logger=configure_logging("test_df_planner"),
        )

        stale_trial = {
            "trial_id": 99,
            "commit_sha": "stale-sha",
            "r_squared": 0.99,
            "rmse": 0.01,
            "status": "success",
        }
        current_trial = {
            "trial_id": 1,
            "commit_sha": "sha1",
            "r_squared": 0.85,
            "rmse": 1.5,
            "status": "success",
        }

        sys.path.insert(0, str(DEMO_DIR))
        try:
            import importlib

            import plan as planner_mod  # type: ignore[import-not-found]

            importlib.reload(planner_mod)

            with (
                mock.patch.object(ctx, "get_all_trials", return_value=[stale_trial, current_trial]),
                mock.patch.object(planner_mod, "_current_session_trials", return_value=[current_trial]),
                mock.patch.object(
                    planner_mod.session, "generate", return_value="Try current-session refinement"
                ) as gen_mock,
            ):
                proposal = planner_mod._make_reactive_proposal(ctx, 5, current_trial)

            assert proposal.parent_commits == ["sha1"]
            prompt_arg = gen_mock.call_args[0][0]
            assert "Trial 1" in prompt_arg
            assert "Trial 99" not in prompt_arg
        finally:
            sys.path.pop(0)
            if "plan" in sys.modules:
                del sys.modules["plan"]


# ---------------------------------------------------------------------------
# Full AI integration (opt-in)
# ---------------------------------------------------------------------------

_SKIP_AI = not (os.environ.get("EDEN_RUN_AI_DEMO") == "1" and shutil.which("codex") and shutil.which("claude"))


@pytest.mark.skipif(_SKIP_AI, reason="Requires EDEN_RUN_AI_DEMO=1, codex, and claude on PATH")
def test_data_fitting_demo(tmp_path: Path) -> None:
    """Run the full data-fitting demo with real AI agents."""
    from eden.orchestrator import Orchestrator, bootstrap

    experiment_root = tmp_path / "experiment"
    shutil.copytree(DEMO_DIR, experiment_root)
    workspace = experiment_root / "planner" / "workspace"

    # Init workspace git repo
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.local"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=workspace, check=True, capture_output=True)

    # Patch python path
    config_path = experiment_root / ".eden" / "config.yaml"
    config_text = config_path.read_text().replace("python3", sys.executable)
    config_path.write_text(config_text)

    result = bootstrap(str(config_path))
    orchestrator = Orchestrator(result.config, result.database_manager, result.logger)
    orchestrator.run()

    trials = result.database_manager.list_trials()
    successful = [t for t in trials if t["status"] == "success"]
    assert len(successful) > 0, "Expected at least one successful trial"

    best_r2 = max(t["r_squared"] for t in successful)
    assert best_r2 > 0, f"Expected positive R², got {best_r2}"
