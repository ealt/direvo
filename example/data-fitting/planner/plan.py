"""Planner for the data-fitting experiment.

A persistent subprocess that proposes model improvement strategies.
On startup, creates an initial batch of proposals with diverse approaches.
Then listens for trial completion notifications and creates follow-up
proposals using Claude CLI to analyze results and suggest new approaches.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from eden.planner_kit import PlannerContext, Proposal, run_planner


def read_trial_artifact(trial_id: int, filename: str) -> str | None:
    """Read an artifact file from a completed trial."""
    path = Path(f".eden/artifacts/trial-{trial_id}/{filename}")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def format_history(trials: list[dict]) -> str:
    """Format trial history for the Claude prompt.

    Reads three artifact types per trial (all produced during the trial lifecycle):
    - plan.md: the strategy the planner proposed (written by create_proposal)
    - notes.md: the implementer's description of what it actually did
    - eval_report.json: detailed evaluation diagnostics (residual stats, etc.)
    """
    if not trials:
        return "No completed trials yet."
    lines = []
    for t in trials[:10]:
        tid = t["trial_id"]
        plan_text = read_trial_artifact(tid, "plan.md")
        notes = read_trial_artifact(tid, "notes.md")
        eval_report = read_trial_artifact(tid, "eval_report.json")

        summary = (notes or plan_text or "Unknown approach")[:200]
        line = f"- Trial {tid}: R²={t['r_squared']:.4f}, RMSE={t['rmse']:.4f} | {summary}"

        if eval_report:
            try:
                report = json.loads(eval_report)
                line += f" [residual μ={report['residual_mean']:.4f}, σ={report['residual_std']:.4f}]"
            except (json.JSONDecodeError, KeyError):
                pass

        lines.append(line)
    return "\n".join(lines)


INITIAL_STRATEGIES = [
    {
        "slug": "linear-regression",
        "priority": 1.0,
        "plan": (
            "Implement linear regression. Use numpy's least squares (np.linalg.lstsq) "
            "to fit y = a*x + b. Construct a design matrix with columns [x, 1] for "
            "both train and test."
        ),
    },
    {
        "slug": "polynomial-degree3",
        "priority": 2.0,
        "plan": (
            "Implement polynomial regression of degree 3. Build a Vandermonde matrix "
            "using np.vander(x, N=4) for features [x^3, x^2, x, 1]. Fit with "
            "np.linalg.lstsq and predict on test."
        ),
    },
    {
        "slug": "polynomial-degree5",
        "priority": 3.0,
        "plan": (
            "Implement polynomial regression of degree 5. Build features using "
            "np.vander(x, N=6). Fit with np.linalg.lstsq. This should capture both "
            "the quadratic and sinusoidal components of the data."
        ),
    },
    {
        "slug": "fourier-features",
        "priority": 4.0,
        "plan": (
            "Implement regression with Fourier features. Create a design matrix with "
            "columns [sin(x), cos(x), sin(2x), cos(2x), x, x^2, 1]. Fit with "
            "np.linalg.lstsq. The trigonometric terms should capture periodic patterns."
        ),
    },
    {
        "slug": "rbf-kernel-regression",
        "priority": 5.0,
        "plan": (
            "Implement kernel ridge regression with RBF kernels. Compute the kernel "
            "matrix K[i,j] = exp(-||x_i - x_j||^2 / (2 * sigma^2)) with sigma=1.0. "
            "Solve (K + lambda*I) @ alpha = y_train with lambda=0.01. "
            "Predict using K_test @ alpha."
        ),
    },
]


_SYSTEM_PROMPT = (
    "You are a data science strategist for a data-fitting experiment.\n"
    "The task is to predict y from x values. The training data has 150 points "
    "with x in [-3, 3].\n"
    "The model must implement predict(X_train, y_train, X_test) -> np.ndarray.\n"
    "Only numpy is available (no sklearn, scipy, or other libraries).\n\n"
    "When asked, propose ONE specific modeling approach in 2-4 sentences. "
    "Be specific about the mathematical formulation. Output ONLY the strategy "
    "description, no code."
)

# Accumulated session: first call starts a new conversation, subsequent calls
# continue it with -c so Claude retains context of all prior proposals/results.
_session_started = False


def generate_claude_proposal(history: str) -> str | None:
    """Use Claude CLI to generate a new model strategy based on trial history.

    Maintains a single Claude session across the planner's lifetime so that
    Claude accumulates context about all strategies it has proposed and their
    outcomes, rather than starting cold each time.
    """
    global _session_started  # noqa: PLW0603

    prompt = (
        f"Latest trial results (sorted by R²):\n{history}\n\n"
        "Propose the next approach. It must be different from everything you "
        "have already suggested."
    )

    cmd = ["claude", "-p", prompt, "--no-input"]
    if _session_started:
        cmd.append("-c")
    else:
        cmd.extend(["-s", _SYSTEM_PROMPT])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            _session_started = True
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _fallback_text(best: dict | None) -> str:
    """Generate fallback plan text when Claude CLI is unavailable."""
    if best:
        return (
            f"Improve on the best approach so far (Trial {best['trial_id']}, "
            f"R²={best['r_squared']:.4f}). Try adding more features, adjusting "
            f"regularization, or combining multiple basis functions. Read the current "
            f"model.py and make targeted improvements."
        )
    return "Try a polynomial regression of degree 4 with np.vander."


def _make_initial_proposals(ctx: PlannerContext) -> list[Proposal]:
    batch = INITIAL_STRATEGIES[: ctx.parallel_trials + 2]
    return [
        Proposal(
            slug=s["slug"],
            priority=s["priority"],
            plan_text=s["plan"],
            parent_commits=[ctx.head_sha],
        )
        for s in batch
    ]


def _make_reactive_proposal(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal:
    all_trials = ctx.get_all_trials(order_by="r_squared DESC")
    history = format_history(all_trials)
    best = all_trials[0] if all_trials else None
    parent_sha = best["commit_sha"] if best else ctx.head_sha

    plan_text = generate_claude_proposal(history) or _fallback_text(best)
    priority = (float(trial["r_squared"]) + 1.0) if trial["r_squared"] is not None else 0.0

    return Proposal(
        slug=f"strategy-{proposal_index}-t{trial['trial_id']}",
        priority=priority,
        plan_text=plan_text,
        parent_commits=[parent_sha],
    )


if __name__ == "__main__":
    run_planner(
        make_initial_proposals=_make_initial_proposals,
        make_reactive_proposal=_make_reactive_proposal,
        parallel_trials=3,
    )
