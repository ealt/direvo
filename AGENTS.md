# AGENTS.md

This file provides guidance to AI agents working with this repository.

## Commands

| Command | Purpose |
|---------|---------|
| `uv run -m pytest -q` | Run all tests |
| `uv run -m pytest -q tests/test_<area>.py` | Run a single test module |
| `uv run -m pytest -q -k test_function_name` | Run a single test function |
| `uv run ruff check .` | Lint Python code |
| `uv run ruff check --fix .` | Lint and auto-fix |
| `uv run pyright` | Static type checking |
| `uv run direvo doctor --config <path>` | Validate a workspace config |
| `./scripts/run_docker_integration.sh` | Docker smoke test |
| `./scripts/run_privileged_validation.sh` | Root-only container validation |

## Architecture Overview

DirEvo is an orchestration system that runs concurrent research trials inside a single Docker container. A **planner** proposes experiments via a shared SQLite database, and the **orchestrator** dispatches them as parallel git worktrees, each executed by an isolated Linux user.

### Experiment Root vs Workspace Root

The **experiment root** is the top-level directory (inferred from the config path — parent of `.direvo/`). It contains the config, scripts, databases, and the workspace as a subdirectory. The **workspace root** is a git repo specified by the `workspace` config field (relative to experiment root). Trials operate on worktrees of the workspace repo.

This separation prevents reward hacking: the execution agent can only modify files in its worktree (a checkout of the workspace), while eval, execute, and plan scripts live outside the workspace at the experiment root.

All paths in the config (commands, databases, proposals/artifacts dirs) are relative to experiment root. Command strings have their file-path tokens resolved against experiment root at config load time.

### Data Flow

1. Planner process writes proposals to `proposals.db` (status: drafting → ready)
2. Orchestrator's async dispatch loop atomically claims a ready proposal (`BEGIN IMMEDIATE`)
3. For each claimed proposal, the orchestrator: reserves a trial ID in `results.db` → creates a git worktree → copies proposal docs → runs execute command → runs evaluate command → parses JSON metrics from eval stdout → commits results → records to `results.db`
4. Planner is notified of completed trials via stdin of its long-running subprocess

### Two-Database Design

- **results.db**: Orchestrator writes, planner reads. Stores trial outcomes with user-defined metric columns from `metrics_schema` in config.
- **proposals.db**: Planner writes, orchestrator reads/updates. Priority queue with atomic claiming. Proposals carry parent commits and a slug used for branch naming.

### Subprocess Isolation Model

The system runs inside Docker with multiple Linux users created at container startup by `runtime.py`:
- `planner` user: runs the planner subprocess, has read access to results.db and read/write to proposals.db
- `trial-{slot}` users (one per `parallel_trials`): each owns their worktree, isolated from other slots
- All subprocess commands (execution, evaluation, planner) are run via `su <user> -c` for permission enforcement

### Directory Structure

```
direvo/
├── src/direvo/   # Core package
│   ├── sql/            # SQL schema templates
│   ├── cli.py          # CLI entry point
│   ├── orchestrator.py # Async dispatch loop
│   ├── config.py       # YAML loading, path resolution against experiment root
│   ├── db.py           # SQLite manager for both databases
│   ├── git_manager.py  # Worktree lifecycle, branch ops
│   ├── execution.py    # Subprocess execution with user switching
│   ├── planner.py      # Persistent planner subprocess (CWD=experiment_root)
│   ├── termination.py  # Stop condition evaluation
│   ├── runtime.py      # Container user/permission bootstrap
│   ├── worktree.py     # Trial directory setup
│   ├── models.py       # Frozen dataclasses for config/results
│   └── logging.py      # Logging configuration
├── tests/              # pytest suite, mirrors src modules
│   └── fixtures/experiment/  # E2E test: seed-sum experiment
├── docker/             # entrypoint.sh
├── scripts/            # Docker integration/validation scripts
├── docs/plans/         # Implementation plans
└── docs/prds/          # Product requirement documents
```

An experiment directory (e.g., `tests/fixtures/experiment/`) has this layout:

```
experiment/              # experiment_root
├── .direvo/
│   └── config.yaml
├── plan.py              # planner script (outside workspace)
├── execute.py           # execution script (outside workspace)
├── eval.py              # evaluation script (outside workspace)
└── workspace/           # workspace_root (git repo)
    └── seeds.md         # trial data
```

## Key Patterns

- **Frozen dataclasses** (`models.py`): All config and result types are immutable.
- **Atomic claiming**: Proposals are claimed via `BEGIN IMMEDIATE` transactions to prevent double-dispatch.
- **Deterministic recovery**: Failed worktrees are cleaned with hard reset + clean untracked + porcelain status verification.
- **Entrypoint sequence** (`docker/entrypoint.sh`): Runs `runtime.py` for user/permission setup, then `cli.py run`.

## Gotchas

- Proposals use `BEGIN IMMEDIATE` for atomic claiming — regular `BEGIN` would allow double-dispatch under concurrent access.
- Git worktree cleanup requires both hard reset and clean; either alone leaves stale state.
- The planner subprocess is long-lived and receives trial notifications via stdin, not polling.
- Docker tests require privileged mode for user creation; use the scripts in `scripts/` rather than running Docker commands by hand.
- The planner subprocess introduces timing non-determinism: a fast execution agent can exhaust the proposal queue before the planner reacts to completion notifications. The orchestrator handles this via idle-polling, but tests must not assume deterministic proposal ordering when a subprocess planner is involved.

## Coding Style

- **Python 3.12+**, compatible with the `pyproject.toml` toolchain
- 4-space indentation, 120-character line limit
- Google-style docstrings where docstrings are needed
- `snake_case` for functions/modules, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants
- Explicit types on public APIs and dataclass fields
- Keep modules small and responsibility-focused

For detailed formatting rules, see [STYLE_GUIDE.md](STYLE_GUIDE.md).

## Testing Guidelines

- Name files `tests/test_<area>.py` and functions `test_<behavior>()`
- Add or update tests for any behavioral change, especially around git operations, SQLite coordination, orchestration flows, and Docker/runtime permissions
- Run the repo-local suite first, then Docker scripts in `scripts/` for environment-specific behavior
- Demos and examples must be exercised by the test suite — standalone scripts that aren't tested rot silently
- When test inputs produce deterministic outputs, assert exact values rather than loose properties (e.g., verify scores against actual committed data, not just "scores increase")
- When testing systems with async subprocesses, don't assume deterministic ordering — design assertions around structural invariants that hold regardless of timing

## Commit Guidelines

- Short imperative commit subjects (e.g., "Add proposal claiming", "Fix worktree cleanup")
- For pull requests, include: problem/solution summary, config/Docker/permission implications, commands run and test results, linked issue or plan doc when relevant

## Configuration

Workspace config lives at `.direvo/config.yaml`. See `docs/plans/v0.md` for the full configuration contract. Treat `docs/plans/v0.md` as the implementation contract unless code intentionally updates it.
