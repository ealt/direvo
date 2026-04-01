"""Planner for the data-fitting experiment.

A persistent subprocess that proposes model improvement strategies.
On startup, creates an initial batch of proposals with diverse approaches.
Then listens for trial completion notifications and creates follow-up
proposals using Claude CLI to analyze results and suggest new approaches.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class _JSONLineFormatter(logging.Formatter):
    """Emit one JSON object per log record, merging ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = {}
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in logging.LogRecord.__dict__:
                continue
            fields[key] = value
        fields["message"] = record.getMessage()
        return json.dumps(fields, sort_keys=True)


def _configure_logging() -> None:
    _log_dir = os.environ.get("DIREVO_LOG_DIR")
    if _log_dir:
        handler = logging.FileHandler(os.path.join(_log_dir, "plan.log"))
        handler.setFormatter(_JSONLineFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def get_head_sha(workspace: str) -> str:
    """Return the current HEAD commit SHA of the workspace repo."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def connect_db(path: str) -> sqlite3.Connection:
    """Connect to a SQLite database with the planner's access mode."""
    if Path(path).name == "results.db":
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def create_proposal(
    *,
    proposals_db: str,
    proposals_dir: str,
    priority: float,
    slug: str,
    parent_commits: list[str],
    plan_text: str,
) -> None:
    """Create a proposal with its plan.md and database row."""
    proposal_path = Path(proposals_dir) / slug
    proposal_path.mkdir(parents=True, exist_ok=True)
    (proposal_path / "plan.md").write_text(plan_text + "\n")

    conn = connect_db(proposals_db)
    try:
        conn.execute(
            """
            INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at)
            VALUES (?, ?, ?, ?, 'ready', datetime('now'))
            """,
            (priority, slug, json.dumps(parent_commits), str(proposal_path)),
        )
        conn.commit()
    finally:
        conn.close()


def get_trial(results_db: str, trial_id: int) -> dict | None:
    """Fetch a completed trial by ID."""
    conn = connect_db(results_db)
    try:
        row = conn.execute(
            "SELECT trial_id, commit_sha, r_squared, rmse FROM trials WHERE trial_id = ? AND status = 'success'",
            (trial_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_trials(results_db: str) -> list[dict]:
    """Fetch all completed trials sorted by r_squared descending."""
    conn = connect_db(results_db)
    try:
        rows = conn.execute(
            "SELECT trial_id, commit_sha, r_squared, rmse FROM trials WHERE status = 'success' ORDER BY r_squared DESC",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def read_trial_artifact(trial_id: int, filename: str) -> str | None:
    """Read an artifact file from a completed trial."""
    path = Path(f".direvo/artifacts/trial-{trial_id}/{filename}")
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


def main() -> None:
    """Run the planner loop."""
    workspace = "workspace"
    parallel_trials = 3

    _configure_logging()

    head_sha = get_head_sha(workspace)
    logger.info("startup", extra={"event": "startup", "parallel_trials": parallel_trials, "head": head_sha})

    proposals_db = ".direvo/proposals.db"
    results_db = ".direvo/results.db"
    proposals_dir = ".direvo/proposals"

    initial_batch = parallel_trials + 2
    for _i, strategy in enumerate(INITIAL_STRATEGIES[:initial_batch]):
        create_proposal(
            proposals_db=proposals_db,
            proposals_dir=proposals_dir,
            priority=strategy["priority"],
            slug=strategy["slug"],
            parent_commits=[head_sha],
            plan_text=strategy["plan"],
        )
        logger.info("propose", extra={
            "event": "propose", "slug": strategy["slug"], "priority": strategy["priority"], "parent": head_sha,
        })

    seen_trials: set[int] = set()
    proposal_counter = initial_batch

    for line in sys.stdin:
        line = line.strip()
        if not line or "Trial completed" not in line:
            continue

        try:
            trial_id = int(line.split(":")[-1].strip())
        except (ValueError, IndexError):
            continue

        logger.info("notify", extra={"event": "notify", "trial_id": trial_id})

        if trial_id in seen_trials:
            continue
        seen_trials.add(trial_id)

        trial = get_trial(results_db, trial_id)
        if trial is None or trial["commit_sha"] is None:
            continue

        logger.info("result", extra={
            "event": "result",
            "trial_id": trial_id,
            "commit": trial["commit_sha"],
            "r_squared": trial["r_squared"],
            "rmse": trial["rmse"],
        })

        all_trials = get_all_trials(results_db)
        history = format_history(all_trials)
        best_trial = all_trials[0] if all_trials else None
        parent_sha = best_trial["commit_sha"] if best_trial else head_sha

        plan_text = generate_claude_proposal(history)
        if plan_text is None:
            plan_text = (
                (
                    f"Improve on the best approach so far (Trial {best_trial['trial_id']}, "
                    f"R²={best_trial['r_squared']:.4f}). Try adding more features, adjusting "
                    f"regularization, or combining multiple basis functions. Read the current "
                    f"model.py and make targeted improvements."
                )
                if best_trial
                else "Try a polynomial regression of degree 4 with np.vander."
            )

        slug = f"strategy-{proposal_counter}-t{trial_id}"
        priority = (float(trial["r_squared"]) + 1.0) if trial["r_squared"] is not None else 0.0

        create_proposal(
            proposals_db=proposals_db,
            proposals_dir=proposals_dir,
            priority=priority,
            slug=slug,
            parent_commits=[parent_sha],
            plan_text=plan_text,
        )
        logger.info("react", extra={
            "event": "react",
            "slug": slug,
            "priority": priority,
            "parent": parent_sha,
            "trial_id": trial_id,
        })
        proposal_counter += 1


if __name__ == "__main__":
    main()
