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
