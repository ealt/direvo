# Data-Fitting Demo

AI agents collaborate to fit a regression model to synthetic data. Claude
proposes strategies, Codex implements them, and the orchestrator evaluates each
on a held-out test set — all running in parallel inside a Docker container with
Unix user isolation.

## Quick Start

```bash
# From the repo root:
uv sync --dev
uv run eden docker run --config example/data-fitting/.eden/config.yaml --output ./output
```

The `eden` CLI is a Python entry point installed in the virtualenv. Use
`uv run eden` to invoke it (or activate the venv first with `source .venv/bin/activate`,
then call `eden` directly).

The `eden docker` command reads the config's `docker:` section, generates a
Dockerfile (installs eden + both CLIs, initializes the workspace git repo,
copies the experiment), builds the image, and runs it with `--privileged` for
user isolation.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (or pip — `pip install -e .` also works)
- Docker
- Claude CLI logged in (`claude auth login`)
- Codex CLI logged in (`codex auth login`)

`eden docker` automatically detects and mounts your local CLI auth directories
into the container — no API keys needed.

## What Happens

1. **Planner** creates 5 initial proposals with diverse strategies (linear
   regression, polynomials, Fourier features, RBF kernels).
2. **Orchestrator** claims proposals, creates git worktrees, and runs 3 trials
   in parallel — each under an isolated `trial-{slot}` Unix user.
3. **Implementer** (Codex) reads `.eden/trial/plan.md`, modifies `model.py`,
   and writes `.eden/trial/notes.md` summarizing what it did.
4. **Orchestrator** commits the changes, then runs evaluation as root.
5. **Evaluator** imports `model.py`, calls `predict()` on the hidden test set,
   outputs R² and RMSE, and writes `.eden/trial/eval_report.json`.
6. **Planner** is notified. It reads trial artifacts (plan, notes, eval report)
   and continues its Claude session to propose the next strategy — parented on
   the best trial's commit.
7. Repeat until 15 trials complete or 30 minutes elapse.

## What to Expect

- The first batch runs immediately. Linear regression scores modestly
  (R² ~ 0.3–0.6). Fourier features should score high (R² > 0.9) because the
  ground truth contains `sin(2x)`.
- Follow-up proposals build on the best trial's committed code, so models
  improve iteratively.
- Check the artifacts for the full trail: `plan.md` (strategy), `notes.md`
  (implementer summary), `eval_report.json` (detailed diagnostics).

## Permission Model

- `train.npz` is granted to the implementer via `file_permissions` (symlinked
  into the worktree so Codex can inspect the data).
- `test.npz` is **not granted** — the implementer cannot see the test set.
- Inside Docker with `--privileged`, these boundaries are enforced by Unix
  users created by `runtime.py`.

## Authentication

`eden docker` mounts your host CLI auth directories into the container. The
generic entrypoint runs `eden-auth-setup` to make them traversable by trial
users so both Claude (planner) and Codex (implementer) can authenticate.

Mounted paths (when they exist on the host):

| Host path                     | Container path                       |
|-------------------------------|--------------------------------------|
| `~/.claude`                   | `/root/.claude`                      |
| `~/.claude.json`              | `/root/.claude.json`                 |
| `~/.config/claude`            | `/root/.config/claude`               |
| `~/.local/state/claude`       | `/root/.local/state/claude`          |
| `~/.local/share/claude`       | `/root/.local/share/claude`          |
| `~/.cache/claude`             | `/root/.cache/claude`                |
| `~/.codex`                    | `/root/.codex`                       |

## Directory Structure

```
.eden/config.yaml          Config: 3 parallel trials, 15 max, R² + RMSE metrics
eval.py                    Evaluator: scores on hidden test set, writes report
generate_data.py           Regenerate train/test data (deterministic, seed=42)
train.npz                  Training data (granted to implementer)
test.npz                   Test data (hidden from planner and implementer)
planner/                   Planner scope
  plan.py                  Persistent subprocess, single Claude session
  planner-prompt.md        Experiment-specific strategy role for Claude
  AGENTS.md                EDEN framework reference for the planner agent
  .agents/skills/          Reusable skill docs for planner agent operations
    read-trial-artifacts.md
    query-trial-results.md
    query-proposals.md
    write-proposal.md
    navigate-workspace.md
  workspace/               Git repo (initialized in Docker, not on host)
    model.py               Baseline model (predict-the-mean)
    AGENTS.md              Interface contract for the implementer
```

## Metrics

| Metric      | Type | Role                                            |
|-------------|------|-------------------------------------------------|
| `r_squared` | real | **Objective** — the orchestrator maximizes this  |
| `rmse`      | real | Informational — recorded but not optimized on    |

## Regenerating the Data

```bash
python example/data-fitting/generate_data.py
```

Ground truth: `y = 0.5 * sin(2x) + 0.1 * x²` with Gaussian noise (σ=0.2),
x ∈ [-3, 3]. 150 train points, 50 test points. Fixed seed (42).
