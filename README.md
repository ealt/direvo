# EDEN

> **E**ric's **D**irected **E**volution **N**exus

**Intelligent evolution.**

EDEN is an orchestration engine for directed code evolution. A planner proposes
experiments, parallel trials run in isolated environments, and results feed back
into the next round — an automated loop of diversify, evaluate, amplify.

## Install

```bash
pip install direvo
```

This gives you both `eden` and `direvo` as CLI commands. The package is called
`direvo` on PyPI; the tool is called EDEN.

## Quick start

```bash
# Scaffold a new experiment
eden init my-experiment
cd my-experiment

# Edit eval.py, implement.py, and .eden/config.yaml for your use case

# Validate your setup
eden doctor --config .eden/config.yaml

# Run in Docker (recommended)
eden docker run --config .eden/config.yaml
```

## What it does

Most research automation tools are either a single-agent loop (try something,
check if it worked, repeat) or a static job runner (execute a fixed matrix of
experiments). EDEN is neither.

A **planner** — human, AI, or hybrid — proposes experiments by writing to a
shared database. An **orchestrator** claims proposals and dispatches them as
parallel git worktrees inside Docker, each running under an isolated Linux user.
Every trial is evaluated against a fitness function, and results flow back to the
planner to inform the next generation of proposals.

The architecture mirrors directed evolution in the lab:

| Directed Evolution         | EDEN                                              |
| -------------------------- | ------------------------------------------------- |
| Library of variants        | Parallel trials in isolated git worktrees         |
| Screening / assay          | Eval script producing JSON metrics                |
| Amplification (next round) | Planner reads results, proposes next batch        |
| Iterative rounds           | The propose → execute → evaluate loop             |
| Intelligent guidance       | Planner is strategic, not random                  |

## Configuration

An experiment lives in a directory with a `.eden/config.yaml`:

```yaml
planner_root: "./planner"
workspace: "./workspace"
parallel_trials: 3
implement_command: "python3 implement.py"
evaluate_command:  "python3 eval.py"
plan_command:      "python3 plan.py"
max_trials: 50
max_wall_time: "1h"
metrics_schema:
  score: real
objective:
  expr: "score"
  direction: "maximize"
```

The planner script runs as a persistent subprocess and writes proposals to
`proposals.db`. The orchestrator dispatches them into worktrees, runs the
implement command, commits the result, runs the evaluate command, and writes
outcomes to `results.db` — then notifies the planner to propose the next batch.

## Directory layout

After `eden init`, your experiment looks like:

```
my-experiment/
├── .eden/
│   └── config.yaml
├── eval.py                  # evaluation script
├── implement.py             # implementer script
└── planner/
    ├── plan.py              # planner script
    ├── AGENTS.md            # planner agent guidance
    ├── .agents/skills/      # planner skill docs
    └── workspace/           # git repo trials branch from
```

## Planner SDK

Planner scripts import helpers from `eden.planner_kit`:

```python
from eden.planner_kit import PlannerContext, Proposal, run_planner
```

The SDK provides database access, artifact reading, notification parsing, and
a convenience main loop. See the [data-fitting example](example/data-fitting/)
for a complete planner using Claude CLI.

A TypeScript SDK (`@direvo/planner-kit`) is planned for writing planners in
TypeScript or other Node.js languages.

## Web UI

EDEN includes a browser-based dashboard for observing experiments in real time or
exploring completed runs:

```bash
# Install the web extras
pip install 'direvo[web]'

# Build the frontend (once)
cd packages/web-ui && npm install && npm run build && cd ../..

# View a live experiment
eden ui --config .eden/config.yaml

# View an exported experiment
eden ui --experiment-dir ./direvo-output-20260401-143000/
```

The UI provides five views: **Metrics** (charts with convergence tracking),
**Timeline** (per-slot trial status), **Artifacts** (markdown/JSON viewer),
**Proposals** (queue table), and **Explorer** (SQL console, raw DB tables,
session log, git DAG).

The dashboard runs entirely in the browser — it downloads the SQLite databases
and queries them locally using sql.js (SQLite compiled to WebAssembly), so there
is no server-side query layer.

## CLI commands

| Command | Purpose |
|---------|---------|
| `eden init [directory]` | Scaffold a new experiment |
| `eden run --config <path>` | Run an experiment |
| `eden doctor --config <path>` | Validate experiment setup |
| `eden docker build --config <path>` | Build Docker image |
| `eden docker run --config <path>` | Build and run in Docker |
| `eden ui --config <path>` | Open Web UI for a live experiment |
| `eden ui --experiment-dir <path>` | Open Web UI for an exported experiment |

## Documentation

- [AGENTS.md](AGENTS.md) — architecture deep-dive, data flow, isolation model
- [docs/plans/v0.md](docs/plans/v0.md) — full configuration contract
- [docs/plans/web-ui-observability.md](docs/plans/web-ui-observability.md) — Web UI design decisions
- [example/data-fitting/](example/data-fitting/) — complete working example
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup and workflow

## Development

```bash
git clone <repo-url>
cd eden
uv sync --dev
uv run -m pytest -q

# Web UI development (optional)
cd packages/web-ui
npm install
npm run dev          # Vite dev server on localhost:5173
# In another terminal:
uv run eden ui --config .eden/config.yaml --dev  # backend on localhost:8741
```

## License

[MIT](LICENSE)
