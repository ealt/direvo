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
| `uv run eden cleanup --config <path>` | Hard reset experiment state: worktrees, `trial/*` branches, SQLite DBs and journals, `session.log`, proposals and artifact trees, planner `.eden` symlinks |
| `uv run eden docker build --config <path>` | Build Docker image from config |
| `uv run eden docker run --config <path>` | Build and run experiment in Docker |
| `uv run eden ui --config <path>` | Start the Web UI for a live experiment |
| `uv run eden ui --experiment-dir <path>` | Start the Web UI for an exported experiment |
| `cd packages/web-ui && npm run build` | Build the Web UI frontend |
| `cd packages/web-ui && npm test` | Run Web UI frontend tests |
| `./scripts/run_docker_integration.sh` | Docker smoke test |
| `./scripts/run_privileged_validation.sh` | Root-only container validation |

## Architecture Overview

EDEN is an orchestration system that runs concurrent research trials inside a single Docker container. A **planner** proposes experiments via a shared SQLite database, and the **orchestrator** dispatches them as parallel git worktrees, each executed by an isolated Linux user. A **Web UI** provides browser-based observability into experiments, both during live runs and for post-hoc exploration of completed experiments.

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

### Web UI

The Web UI (`eden ui`) provides browser-based experiment observability. It is a thin Python file server (Starlette) that serves experiment data files over HTTP, paired with a React SPA that queries SQLite databases directly in the browser using sql.js (SQLite compiled to WebAssembly).

**Architecture**: The backend serves raw files — `results.db` is served as-is (DELETE journal mode), `proposals.db` is served as a WAL-checkpointed snapshot via `sqlite3.backup()`, and artifacts/logs are served via standard static file serving. The frontend downloads the database files and runs SQL queries in-browser, enabling a SQL Console for arbitrary exploration. Live updates use HEAD-based polling (every 3 seconds) to detect file changes.

**Two modes**:
- **Live** (`eden ui --config .eden/config.yaml`): reads from the experiment root alongside a running orchestrator
- **Post-run** (`eden ui --experiment-dir /path/to/exported/`): reads from an exported experiment directory

**Key files**: `src/eden/web/server.py` (Starlette app), `packages/web-ui/` (React frontend)

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
│   ├── docker/         # Shipped Docker scripts (entrypoint, auth, export)
│   ├── web/            # Web UI server (Starlette file server)
│   ├── cli.py          # CLI entry point (run, doctor, docker build/run, ui)
│   ├── bootstrap.py    # Session bootstrap: config load, DB init, runtime setup
│   ├── orchestrator.py # Async dispatch loop
│   ├── docker_runner.py # Dockerfile generation, image build, container run
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
├── packages/web-ui/    # React/Vite/TypeScript frontend SPA
├── docker/             # Backward-compat entrypoint wrapper
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
├── plan.py              # persistent planner subprocess (experiment-scoped)
└── planner/             # planner_root
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
- The Web UI cannot serve `proposals.db` as a raw file because it uses WAL journal mode — recent writes may live in the `-wal` sidecar, and sql.js (browser SQLite) has no WAL support. The backend serves a checkpointed snapshot via `sqlite3.backup()` instead. `results.db` uses DELETE mode and is served as-is.
- The Web UI frontend requires building before `eden ui` can serve it: run `cd packages/web-ui && npm run build` first. Use `--dev` to proxy to the Vite dev server during frontend development.

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
