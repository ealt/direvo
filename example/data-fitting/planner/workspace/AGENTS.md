# Instructions

Read `.direvo/trial/plan.md` and implement exactly what it describes.

## Interface

Edit `model.py`. The `predict` function signature must not change:

```python
def predict(X_train: np.ndarray, y_train: np.ndarray, X_test: np.ndarray) -> np.ndarray:
```

- `X_train`: 1D array of training x values
- `y_train`: 1D array of training y values
- `X_test`: 1D array of test x values
- Returns: 1D array of predicted y values for `X_test`

## Constraints

- Only `numpy` is available. Do not use sklearn, scipy, torch, or other libraries.
- All code must be in `model.py`.
- `train.npz` is available in the working directory if needed (`np.load("train.npz")`).

## Artifacts

After implementing the plan, write a brief summary (2-3 sentences) to
`.direvo/trial/notes.md` describing what you implemented and any design
decisions you made.
