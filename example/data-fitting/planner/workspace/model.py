"""Prediction model for the data-fitting experiment."""

from __future__ import annotations

import numpy as np


def predict(X_train: np.ndarray, y_train: np.ndarray, X_test: np.ndarray) -> np.ndarray:
    """Fit a model on training data and return predictions for test inputs.

    Args:
        X_train: 1D array of training x values.
        y_train: 1D array of training y values.
        X_test: 1D array of test x values.

    Returns:
        1D array of predicted y values for X_test.
    """
    return np.full_like(X_test, np.mean(y_train), dtype=float)
