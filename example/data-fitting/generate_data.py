"""Generate synthetic dataset for the data-fitting demo.

Ground truth: y = 0.5 * sin(2x) + 0.1 * x^2 + noise
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

SEED = 42
N_TRAIN = 150
N_TEST = 50
X_MIN = -3.0
X_MAX = 3.0
NOISE_STD = 0.2


def ground_truth(x: np.ndarray) -> np.ndarray:
    """Compute y = 0.5 * sin(2x) + 0.1 * x^2."""
    return 0.5 * np.sin(2.0 * x) + 0.1 * x**2


def main() -> None:
    """Generate train.npz and test.npz in the script's directory."""
    rng = np.random.default_rng(SEED)
    script_dir = Path(__file__).resolve().parent

    x_all = rng.uniform(X_MIN, X_MAX, N_TRAIN + N_TEST)
    y_all = ground_truth(x_all) + rng.normal(0.0, NOISE_STD, len(x_all))

    np.savez(script_dir / "train.npz", x=x_all[:N_TRAIN], y=y_all[:N_TRAIN])
    np.savez(script_dir / "test.npz", x=x_all[N_TRAIN:], y=y_all[N_TRAIN:])
    print(f"Generated {N_TRAIN} train points and {N_TEST} test points.")


if __name__ == "__main__":
    main()
