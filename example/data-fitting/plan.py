"""Data-fitting experiment planner subprocess.

Lives at the experiment root (orchestrator-owned, like eval.py). The
orchestrator still starts this script as the persistent ``planner`` user with
``planner_root`` as the process working directory.

On startup, creates an initial batch of proposals with diverse approaches.
Then listens for trial completion notifications and creates follow-up proposals
using Claude CLI to analyze results and suggest new approaches.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from eden.planner_kit import ClaudeSession, PlannerContext, Proposal, run_planner

_EXPERIMENT_ROOT = Path(__file__).resolve().parent
session = ClaudeSession(
    append_system_prompt_file=_EXPERIMENT_ROOT / "planner" / "planner-prompt.md"
)


def format_history(ctx: PlannerContext, trials: list[dict]) -> str:
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
        plan_text = ctx.read_trial_artifact(tid, "plan.md")
        notes = ctx.read_trial_artifact(tid, "notes.md")
        eval_report = ctx.read_trial_artifact(tid, "eval_report.json")

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


def _current_session_trials(ctx: PlannerContext, trials: list[dict]) -> list[dict]:
    """Return only trials whose commits are reachable from this session's HEAD."""
    return [trial for trial in trials if _commit_is_reachable(ctx.workspace, ctx.head_sha, trial.get("commit_sha"))]


def _commit_is_reachable(workspace: str, head_sha: str, commit_sha: object) -> bool:
    """Return whether a commit exists in the workspace and descends from HEAD."""
    if not isinstance(commit_sha, str) or not commit_sha.strip():
        return False
    exists = subprocess.run(
        ["git", "-C", workspace, "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        return False
    relation = subprocess.run(
        ["git", "-C", workspace, "merge-base", "--is-ancestor", head_sha, commit_sha],
        check=False,
        capture_output=True,
        text=True,
    )
    return relation.returncode == 0


def _make_reactive_proposal(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal:
    all_trials = ctx.get_all_trials(order_by="r_squared DESC")
    session_trials = _current_session_trials(ctx, all_trials)
    history = format_history(ctx, session_trials)
    best = session_trials[0] if session_trials else None
    parent_sha = best["commit_sha"] if best else str(trial.get("commit_sha") or ctx.head_sha)

    prompt = (
        f"Latest trial results (sorted by R²):\n{history}\n\n"
        "Propose the next approach. It must be different from everything you "
        "have already suggested."
    )
    plan_text = session.generate(prompt) or _fallback_text(best)
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
