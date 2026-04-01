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
| `uv run eden doctor --config <path>` | Validate a workspace config |
| `./scripts/run_docker_integration.sh` | Docker smoke test |
| `./scripts/run_privileged_validation.sh` | Root-only container validation |

## Architecture Overview

EDEN is an orchestration system that runs concurrent research trials inside a single Docker container. A **planner** proposes experiments via a shared SQLite database, and the **orchestrator** dispatches them as parallel git worktrees, each executed by an isolated Linux user.

### Experiment Root, Planner Root, And Workspace Root

The **experiment root** is the top-level directory (inferred from the config path
as the parent of `.eden/`). It contains shared session state and acts as the
outer trust boundary.

The **planner root** is configured by `planner_root`, must live under the
experiment root, and contains planner-owned state such as `proposals.db`,
proposal docs, and the planner subprocess working directory.

The **workspace root** is a git repo configured by `workspace`, resolved
relative to `planner_root`, and must live under the planner root. Trials
operate on worktrees of the workspace repo.

This separation prevents reward hacking: the implementer can only modify files
in its worktree, planner assets live under `planner_root`, and evaluation runs
after the trial commit so eval-side writes can be reset without contaminating
the committed result.

Paths resolve by scope:
- experiment-scoped paths resolve from `experiment_root`
- planner-scoped paths resolve from `planner_root`
- command file tokens are resolved against the command's scope root at config load time

### Data Flow

1. Planner process writes proposals to `proposals.db` (status: drafting → ready)
2. Orchestrator's async dispatch loop atomically claims a ready proposal (`BEGIN IMMEDIATE`)
3. For each claimed proposal, the orchestrator: reserves a trial ID in `results.db` → creates a git worktree → copies proposal docs → creates transient implementer grants → runs implement command → removes grants → commits results → runs evaluate command as root → resets eval-side writes → records to `results.db`
4. Planner is notified of completed trials via stdin of its long-running subprocess

### Two-Database Design

- **results.db**: Orchestrator writes, planner reads. Uses SQLite `DELETE` journal mode and stores trial outcomes with user-defined metric columns from `metrics_schema`.
- **proposals.db**: Planner writes, orchestrator reads/updates. Uses SQLite `WAL` journal mode and acts as the proposal queue. Proposals carry parent commits and a slug used for branch naming.

### Subprocess Isolation Model

The system runs inside Docker with multiple Linux users created at container startup by `runtime.py`:
- `planner` user: runs the planner subprocess, has read access to results.db and read/write to proposals.db
- `trial-{slot}` users (one per `parallel_trials`): each owns their worktree, isolated from other slots
- Planner subprocesses run as `planner` with `planner_root` as the working directory
- Implement commands run as `trial-{slot}` from the slot worktree
- Evaluation runs from the committed worktree as root, then the worktree is reset to `HEAD`

### Directory Structure

```
eden/
├── src/eden/   # Core package
│   ├── sql/            # SQL schema templates
│   ├── cli.py          # CLI entry point
│   ├── orchestrator.py # Async dispatch loop
│   ├── config.py       # YAML loading, scoped path resolution, validation
│   ├── db.py           # SQLite manager for both databases
│   ├── git_manager.py  # Worktree lifecycle, branch ops
│   ├── execution.py    # Implement/evaluate subprocess execution with user switching
│   ├── grants.py       # Transient cross-scope grant symlink helpers
│   ├── planner.py      # Persistent planner subprocess (CWD=planner_root)
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
├── .eden/
│   └── config.yaml
├── eval.py              # evaluation script (experiment-scoped)
├── implement.py         # implementer entry script (experiment-scoped)
└── planner/             # planner_root
    ├── plan.py          # planner script
    └── workspace/       # workspace_root (git repo)
        └── seeds.md     # trial data
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
- The planner subprocess introduces timing non-determinism: a fast implementer can exhaust the proposal queue before the planner reacts to completion notifications. The orchestrator handles this via idle-polling, but tests must not assume deterministic proposal ordering when a subprocess planner is involved.

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

Workspace config lives at `.eden/config.yaml`. See `docs/plans/v0.md` for the full configuration contract. Treat `docs/plans/v0.md` as the implementation contract unless code intentionally updates it.
