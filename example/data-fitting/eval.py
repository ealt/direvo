"""Evaluation script for the data-fitting demo.

Loads the workspace model.py, runs predict() on train+test data,
and outputs R-squared and RMSE as JSON metrics.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)


def r_squared(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Compute R-squared (coefficient of determination)."""
    ss_res = float(np.sum((y_true - y_pred) ** 2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    if ss_tot == 0.0:
        return 1.0 if ss_res == 0.0 else 0.0
    return 1.0 - ss_res / ss_tot


def rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Compute root mean squared error."""
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


def main() -> None:
    """Load model from CWD, evaluate on held-out test set, print JSON metrics."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # The evaluate_command resolves eval.py to an absolute path, so Python sets
    # sys.path[0] to the script's directory (experiment_root) rather than CWD
    # (the worktree). Add CWD so the model import finds model.py.
    sys.path.insert(0, os.getcwd())
    from model import predict  # type: ignore[import-not-found]  # noqa: PLC0415

    train: np.lib.npyio.NpzFile = np.load(SCRIPT_DIR / "train.npz")
    test: np.lib.npyio.NpzFile = np.load(SCRIPT_DIR / "test.npz")

    y_pred = predict(train["x"], train["y"], test["x"])
    y_pred = np.asarray(y_pred, dtype=float).ravel()

    if y_pred.shape != test["y"].shape:
        logger.error("Shape mismatch: predict returned %s, expected %s", y_pred.shape, test["y"].shape)
        sys.exit(1)

    y_test = test["y"]
    residuals = y_test - y_pred
    score = r_squared(y_test, y_pred)
    error = rmse(y_test, y_pred)

    # Write detailed report to trial artifacts (preserved by the orchestrator).
    # The planner reads this via the artifacts symlink to inform future proposals.
    report_dir = Path(".direvo/trial")
    if report_dir.exists():
        report = {
            "r_squared": round(score, 6),
            "rmse": round(error, 6),
            "n_test": len(y_test),
            "y_pred_mean": round(float(np.mean(y_pred)), 6),
            "y_pred_std": round(float(np.std(y_pred)), 6),
            "residual_mean": round(float(np.mean(residuals)), 6),
            "residual_std": round(float(np.std(residuals)), 6),
        }
        (report_dir / "eval_report.json").write_text(json.dumps(report, indent=2) + "\n")

    logger.info("R²=%.4f  RMSE=%.4f", score, error)
    print(json.dumps({"r_squared": round(score, 6), "rmse": round(error, 6)}))


if __name__ == "__main__":
    main()
