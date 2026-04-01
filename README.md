# EDEN

> **E**ric's **D**irected **E**volution **N**exus

**Intelligent evolution.**

EDEN is an orchestration engine for directed code evolution. A planner proposes
experiments, parallel trials run in isolated environments, and results feed back
into the next round — an automated loop of diversify, evaluate, amplify.

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

## Quick start

```bash
uv sync --dev
uv run -m pytest -q        # run the test suite
eden doctor --config .eden/config.yaml  # validate a config
eden run    --config .eden/config.yaml  # run an experiment
```

## Configuration

An experiment lives in a directory with a `.eden/config.yaml`:

```yaml
planner_root: "./planner"
workspace: "./workspace"
parallel_trials: 2
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

```
experiment/                  # experiment root
├── .eden/
│   └── config.yaml
├── eval.py                  # evaluation script (runs as root, reads committed result)
├── implement.py             # implementer script (runs as trial-N user in worktree)
└── planner/                 # planner root
    ├── plan.py              # planner script (long-running, reads results, writes proposals)
    └── workspace/           # the git repo trials branch from
```

## Documentation

- [AGENTS.md](AGENTS.md) — architecture deep-dive, data flow, isolation model
- [docs/plans/v0.md](docs/plans/v0.md) — full configuration contract
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup and workflow

## License

[MIT](LICENSE)
