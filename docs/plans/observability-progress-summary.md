# Plan: Observability — Progress, Summary, and Result Persistence

## Context

Running `direvo run` today produces no human-readable output. The JSON session log goes to stderr and a file, but there's no progress indication, no end-of-run summary, and in Docker the entire experiment state is destroyed when the container exits (`--rm`). This makes the data-fitting demo (and any Docker-based experiment) effectively silent and ephemeral.

Three changes, listed by user-facing priority (most important first). Implementation order differs — see the Implementation Order section.

1. **Result persistence** — volume mounts in the demo so experiment state survives the container
2. **Post-run summary** — library-level summary printed to stdout when the session ends
3. **Live progress** — library-level human-readable progress events to stdout during the run

## 1. Result Persistence (Demo — run.sh + auth-setup.sh)

The library already writes all state to well-defined paths under the experiment root. For Docker, the run script just needs to mount a host directory and copy results there after the run.

### run.sh changes

Create a timestamped output directory on the host, mount it at `/output`. Capture the docker exit code so the output path prints even on failure (the script uses `set -euo pipefail`):

```bash
OUTPUT_DIR="${DIREVO_OUTPUT_DIR:-./direvo-output-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUTPUT_DIR"
rc=0
docker run --rm --privileged \
    -v "$(pwd)/$OUTPUT_DIR:/output" \
    "${AUTH_MOUNTS[@]}" \
    "$IMAGE_NAME" || rc=$?
echo ""
echo "Results: $OUTPUT_DIR/"
exit $rc
```

### auth-setup.sh changes

Currently does `exec direvo-entrypoint "$@"` which replaces the process. The new design must:
1. Preserve signal delivery — `direvo-entrypoint` eventually `exec`s the Python CLI, which installs SIGINT/SIGTERM handlers for graceful shutdown. If the shell stays as PID 1, Docker sends signals to the shell, not the Python process.
2. Export results after the run completes (including after signal-triggered shutdown).

**Approach**: run `direvo-entrypoint` in the background, forward signals to it, wait for it, then export results.

```sh
export_results() {
    if [ -d /output ]; then
        cp -a /experiment/.direvo /output/ 2>/dev/null || true
        mkdir -p /output/planner 2>/dev/null || true
        # Copy planner-owned files (proposals.db + proposal docs).
        # Skip symlinks (results.db, artifacts) — those point at container paths
        # and are already captured under /output/.direvo/.
        cp /experiment/planner/.direvo/proposals.db /output/planner/.direvo/ 2>/dev/null || true
        cp -a /experiment/planner/.direvo/proposals /output/planner/.direvo/ 2>/dev/null || true
        (cd /experiment/planner/workspace && git bundle create /output/workspace.bundle --all 2>/dev/null) || true
    fi
}

direvo-entrypoint "$@" &
child_pid=$!

# Forward signals to the child so the Python orchestrator receives them.
# The trap interrupts `wait`, so we re-wait in a loop until the child exits.
trap 'kill -INT $child_pid 2>/dev/null' INT
trap 'kill -TERM $child_pid 2>/dev/null' TERM

# Wait for the child, retrying if interrupted by a trapped signal.
# Capture exit code from the wait that actually reaps the child.
exit_code=0
while kill -0 "$child_pid" 2>/dev/null; do
    wait "$child_pid" 2>/dev/null && exit_code=$? && break
    exit_code=$?
done

export_results
exit $exit_code
```

**Signal flow**: Docker sends SIGTERM to PID 1 (the shell) → trap forwards SIGTERM to the child → first `wait` is interrupted by the trap and returns immediately → `kill -0` check shows child is still alive (draining in-flight trials) → loop re-enters `wait` → child eventually exits → `wait` reaps it and `$?` captures the real exit code → loop exits → `export_results` runs with complete state.

**Limitations**:
- `SIGKILL` and OOM kills bypass all traps — no export happens. Graceful shutdown via SIGTERM is Docker's default stop mechanism (`docker stop` sends SIGTERM, waits 10s, then SIGKILL).
- If in-flight trials are long-running (demo's `implement_timeout_sec` is 300s), Docker's default 10s stop grace period may not be enough for drain + export. Users can extend with `docker stop -t 60` or `docker run --stop-timeout 60`. This is a documentation note, not a code change.

**Output directory structure on host:**
```
direvo-output-20260331-143000/
├── .direvo/
│   ├── results.db          # Trial results (SQLite)
│   ├── session.log         # Structured event log (JSONL)
│   └── artifacts/          # Per-trial artifacts
│       ├── trial-1/
│       │   ├── plan.md
│       │   ├── notes.md
│       │   └── eval_report.json
│       └── ...
├── planner/
│   └── .direvo/
│       ├── proposals.db    # Planner's proposals (SQLite)
│       └── proposals/      # Proposal docs (plan.md per proposal)
│           ├── linear-regression/
│           │   └── plan.md
│           └── ...
└── workspace.bundle        # Git bundle with all trial commits
```

The user can `git clone workspace.bundle workspace` to inspect the full history.

**Mid-run inspection**: The `/output` volume is populated at exit (by `export_results`), not continuously. For live inspection during a run, use stdout progress output (printed by the library) or `docker exec -it <container> sh` to inspect the container's `/experiment/.direvo/` directly (tail session.log, query results.db, browse artifacts).

### Files to modify
- `example/data-fitting/run.sh`
- `example/data-fitting/auth-setup.sh`

## 2. Post-Run Summary (Library — cli.py + orchestrator.py)

After `orchestrator.run()` returns in `cli.py`, print a human-readable summary to stdout.

### Data needed

All available from existing state after `orchestrator.run()`:
- `orchestrator.last_termination_reason` — why the session ended
- `orchestrator.database_manager.list_trials()` — all trial rows with status and metrics
- `orchestrator.config.objective` — `.expr` (metric name/SQL) and `.direction`
- `orchestrator.config.metrics_schema` — all metric names
- Wall clock time — need to add `orchestrator.wall_time_seconds` (set in `_run_async` finally block from `time.monotonic() - session_started_at`)

### Summary format

```
── Session complete ──────────────────────────────
Reason:   max_trials
Duration: 12m 34s
Trials:   12 success · 2 eval_error · 1 error

Best trial: #7 (fourier-features)
  r_squared  0.9312
  rmse       0.1823
──────────────────────────────────────────────────
```

- "Best trial" determined by the objective expression and direction
- All metrics from `metrics_schema` shown for the best trial (not just the objective)
- The slug comes from the branch name (`trial/{id}-{slug}` format, parse after the first `-`)
- If no successful trials, show "No successful trials" instead of best trial block
- **Metric rendering**: `real` → 4 decimal places (e.g. `0.9312`), `integer` → no decimals, `text` → quoted string, `None`/missing → `-`

### Implementation

**orchestrator.py** — Add `self.wall_time_seconds: float = 0.0` to `__init__`. Set it in `_run_async`'s finally block:

```python
self.wall_time_seconds = time.monotonic() - session_started_at
```

**New file: `src/direvo/summary.py`** — Keeps summary logic separate from CLI:

```python
def print_summary(
    orchestrator: Orchestrator,
) -> None:
    """Print a human-readable session summary to stdout."""
```

Logic:
1. Get all trials via `list_trials()`
2. Count by status (success, error, eval_error, starting)
3. Find best trial: query trials with status=success, sort by objective expression and direction
4. Format duration from `orchestrator.wall_time_seconds`
5. Print to stdout

For finding the best trial, add a `best_trial()` method to `DatabaseManager`:

```python
def best_trial(self, expression: str, direction: ObjectiveDirection) -> sqlite3.Row | None:
    """Return the trial with the best objective value, or None."""
    order = "DESC" if direction == ObjectiveDirection.MAXIMIZE else "ASC"
    query = f"""
        SELECT * FROM trials
        WHERE status = ?
          AND ({expression}) IS NOT NULL
        ORDER BY ({expression}) {order}, trial_id ASC
        LIMIT 1
    """
    ...
```

**cli.py** — After `orchestrator.run()`, call `print_summary(orchestrator)`:

```python
if args.command == "run":
    result = bootstrap(Path(args.config))
    orchestrator = Orchestrator(result.config, result.database_manager, result.logger)
    orchestrator.run()
    print_summary(orchestrator)
    return 0
```

### Files to modify
- `src/direvo/orchestrator.py` — add `wall_time_seconds` attribute
- `src/direvo/db.py` — add `best_trial()` method
- `src/direvo/summary.py` — **new file**, summary formatting and printing
- `src/direvo/cli.py` — call `print_summary()` after run

## 3. Live Progress (Library — logging.py + orchestrator.py)

Add a human-readable progress handler to stdout alongside the existing JSON file logger. The current `StreamHandler` writes JSON to stderr (Python's default stream for `StreamHandler()`). Add a second handler that writes compact progress lines to stdout for key milestone events only.

### Progress format

```
[00:32] Trial #1 started (linear-regression) [slot 0]
[01:45] Trial #1 complete — r_squared=0.4210 rmse=0.3812 [slot 0]
[01:47] Trial #2 started (polynomial-degree3) [slot 1]
[02:12] Trial #3 failed — timeout [slot 2]
[03:01] Trial #2 complete — r_squared=0.7834 rmse=0.2145 [slot 1]
```

- Elapsed time prefix `[MM:SS]` — relative to session start
- Only milestone events: `trial_started`, `trial_complete`, `trial_failed`
- Metrics shown inline for successful trials; `eval_error` trials show status instead (no metrics available since `evaluation_result.metrics` is `{}` for failures)
- Slug extracted from branch name
- Example `eval_error` line: `[02:12] Trial #3 complete — eval_error [slot 2]`

### Event data changes (orchestrator.py)

The progress formatter needs slug and metrics from the log events, but current events are missing this data:

- `trial_started` (line 355): already has `branch` (contains slug) — **no change needed**
- `trial_complete` (line 439): has `trial_id`, `slot`, `commit_sha`, `status` but **no metrics or branch**. Add `metrics=evaluation_result.metrics` and `branch=branch_name` (both are in scope at that point in `_run_claimed_trial`).
- `trial_failed` (line 517): has `trial_id`, `slot`, `proposal_id`, `error` but **no branch**. Add `branch=branch_name` (in scope).

These are additive changes to existing `log_event` calls — no new events, no breaking changes to the JSON schema.

### Implementation

**logging.py** — Add `ProgressFormatter` class and `ProgressFilter`:

```python
class ProgressFormatter(logging.Formatter):
    """Render milestone events as compact human-readable lines."""

    def __init__(self, start_time: float) -> None:
        super().__init__()
        self.start_time = start_time

    def format(self, record: logging.LogRecord) -> str:
        elapsed = time.monotonic() - self.start_time
        minutes, seconds = divmod(int(elapsed), 60)
        fields = getattr(record, "fields", {}) or {}
        event = getattr(record, "event", "")
        slot = fields.get("slot", "?")
        trial_id = fields.get("trial_id", "?")
        # Format based on event type...


class ProgressFilter(logging.Filter):
    """Only pass milestone events to the progress handler."""

    _MILESTONE_EVENTS = {"trial_started", "trial_complete", "trial_failed"}

    def filter(self, record: logging.LogRecord) -> bool:
        event = getattr(record, "event", None)
        return event in self._MILESTONE_EVENTS
```

**logging.py** — Update `configure_logging()` to accept an optional `progress` flag and `start_time`:

```python
def configure_logging(
    log_path: Path,
    *,
    session_id: str | None = None,
    progress: bool = False,
    start_time: float | None = None,
) -> logging.Logger:
```

When `progress=True`, add a stdout handler with `ProgressFormatter` and `ProgressFilter`. The existing stderr JSON handler remains for machine consumption.

**orchestrator.py** — Pass `start_time=time.monotonic()` when the session starts. Since `configure_logging` is called in `bootstrap()` (before the orchestrator's timer), we need to either:
- Have bootstrap accept a start_time and pass it through, or
- Add the progress handler later in the orchestrator's `__init__` or `run()`

Simplest: add the progress handler in `bootstrap()` since that's where the logger is configured. The start_time won't perfectly match `session_started_at` in `_run_async`, but the difference is negligible (milliseconds of bootstrap time).

Alternatively, add a `start_progress()` method on the logger or use `configure_logging` with progress=True. Since `bootstrap` already calls `configure_logging`, just pass `progress=True` there.

**cli.py** — Pass `progress=True` to bootstrap (or have it always on — progress output is always useful):

Default: enable progress when stdout is a TTY (`sys.stdout.isatty()`), suppress when piped. This keeps scripted callers clean while giving interactive users live feedback. The summary always prints (it's the primary output of a run).

### Extracting slug from branch

The branch format is `trial/{id}-{slug}`. To get the slug:
```python
branch = fields.get("branch", "")
slug = branch.split("-", 1)[1] if "-" in branch else ""
```

### Files to modify
- `src/direvo/logging.py` — add `ProgressFormatter`, `ProgressFilter`, update `configure_logging`
- `src/direvo/orchestrator.py` — pass start_time context (or just use the existing bootstrap call)

## 4. Future Work (not implementing now)

- **Post-run hook**: An optional `summary_command` config field that runs after the session ends, with environment variables for results_db path, session_id, termination reason, etc. Lets experiment authors add custom summaries.

- **Web UI**: A browser-based interface for inspecting experiments — live progress dashboard during a run (trial status, metrics over time, resource usage) and post-run exploration (artifact viewer, metric comparisons, git history browser). Would replace terminal-based observability for users who want richer visualization.

- **Human-as-implementer**: The host user can already inspect and modify the experiment directory during a run, effectively playing the role of planner (creating proposals manually). A future feature could let users also play the implementer role — get assigned a worktree, coordinate with the orchestrator on claim/commit/eval lifecycle, work alongside AI agents in parallel. Would require some mechanism for the orchestrator to allocate a slot to an interactive human session (dedicated worktree, skip `su` isolation, wait for manual commit signal instead of timeout).

## Implementation Order

Build order (dependencies flow downward):

1. **orchestrator.py** — add `wall_time_seconds` attribute and enrich `trial_complete`/`trial_failed` events with metrics and branch fields (prerequisite for both #2 and #3)
2. **db.py + summary.py + cli.py** — post-run summary (self-contained, testable)
3. **logging.py** — progress output (uses enriched events from #1)
4. **run.sh + auth-setup.sh** — volume mounts and result export (demo-specific, needs Docker to test)

## Critical Files

| File | Change |
|------|--------|
| `src/direvo/summary.py` | **New** — summary formatting and printing |
| `src/direvo/cli.py` | Call `print_summary()` after `orchestrator.run()` |
| `src/direvo/db.py` | Add `best_trial()` method |
| `src/direvo/orchestrator.py` | Add `wall_time_seconds`; enrich `trial_complete` (add metrics, branch) and `trial_failed` (add branch) events |
| `src/direvo/logging.py` | Add `ProgressFormatter`, `ProgressFilter`, update `configure_logging` |
| `example/data-fitting/run.sh` | Create output dir, mount at `/output`, print path |
| `example/data-fitting/auth-setup.sh` | Copy state to `/output` after run |

## Verification

### Automated tests

1. `uv run -m pytest tests/ -q` — existing tests still pass
2. `uv run ruff check . && uv run pyright` — lint and type check clean

New tests to add:

- **`tests/test_summary.py`**: Unit tests for `print_summary()` formatting:
  - No trials → "No successful trials" block
  - Mixed statuses → correct counts ("X success · Y eval_error · Z error")
  - Best trial selection respects objective direction (maximize vs minimize)
  - Metric rendering: real (4dp), integer, text, None
  - Duration formatting (seconds, minutes, hours)

- **`tests/test_logging.py`** (extend existing): Tests for `ProgressFormatter`:
  - `trial_started` → correct format with slug and slot
  - `trial_complete` with metrics → inline metrics
  - `trial_complete` with eval_error status → shows "eval_error" instead of metrics
  - `trial_failed` → shows error reason
  - Non-milestone events are filtered by `ProgressFilter`
  - Elapsed time formatting

- **`tests/test_e2e.py`** (extend existing): Verify the e2e test produces:
  - Summary output on stdout (capture and check for "Session complete" header)

- **`tests/test_db.py`** (extend existing): Test `best_trial()`:
  - Returns best by maximize direction
  - Returns best by minimize direction
  - Returns None when no successful trials

### Manual checks

4. `uv run direvo run --config tests/fixtures/experiment/.direvo/config.yaml` — observe progress lines and summary on stdout
5. (Docker) `./example/data-fitting/run.sh --build-only` — image builds successfully
6. (Docker, if CLIs available) Full demo run → verify `direvo-output-*/` directory contains results.db, artifacts, proposals, workspace.bundle
