# Plan: Extract planner_kit library module from both planners

## Context

Both planner implementations (`tests/fixtures/experiment/planner/plan.py` and
`example/data-fitting/planner/plan.py`) duplicate identical utility functions
(`get_head_sha`, `connect_db`, `create_proposal`, `get_trial`) and follow the
same main loop pattern (startup → initial batch → stdin notification loop →
reactive proposals). The goal is to extract these into a reusable library module
so both planners become thin wrappers importing from the library.

## Scope and non-goals

This extraction preserves current planner behavior, including the existing
direct-insert-as-ready pattern for proposal creation. The v0 contract
(`docs/plans/v0.md` §Claim protocol) specifies a two-step `drafting → ready`
lifecycle, but both existing planners insert directly with `status='ready'`.
Aligning with the drafting protocol is a separate concern — this plan extracts
shared code into the library without changing behavior.

One intentional change: the data-fitting planner currently uses a
`_JSONLineFormatter` that includes a `message` field in log output. The shared
library formatter will not emit `message` — it serializes only the keyword
fields passed to `log_event()`, matching the seed-sum fixture's format. This
is acceptable because the data-fitting test does not constrain log format.

## Approach: `src/eden/planner_kit.py`

Create a new module providing both low-level utilities (for planners needing
custom loops) and a high-level `run_planner()` function that handles all
boilerplate. The module name `planner_kit` distinguishes it from `planner.py`
(subprocess lifecycle management).

### Key design decisions

1. **Callback-based runner** — `run_planner()` takes two function callbacks:
   `make_initial_proposals(ctx) -> list[Proposal]` and
   `make_reactive_proposal(ctx, proposal_index, trial) -> Proposal | None`.
   This covers both simple (seed-sum) and complex (Claude-powered) planners.

2. **Runner manages the proposal counter** — `proposal_index` is passed to the
   reactive callback. **Invariant**: the first reactive call receives
   `proposal_index = len(initial_proposals)`, and it increments by 1 for each
   subsequent reactive call. This preserves the global sequential seed property
   checked by `test_e2e.py::_verify_plan_log` (line 118).

3. **`SELECT *` for trial queries** — `get_trial` and `get_all_trials` return
   all columns (including dynamic metric columns), making them schema-agnostic.

4. **Runner handles all logging** — `startup`, `notify`, `propose`, `result`,
   and `react` events are logged by the runner. Experiment-specific fields are
   passed via `Proposal.log_fields`. **Reserved keys**: `log_fields` must not
   contain keys the runner already emits — `event`, `slug`, `priority`,
   `parent` for `propose` events; those plus `trial_id` for `react` events.
   The runner must raise `ValueError` at runtime if `log_fields` contains a
   reserved key — this contract is enforced, not just documented.
   Result events include `trial_id`, `commit` (mapped from `commit_sha`), and
   all metric columns automatically (from `SELECT *`).

5. **Convention-based paths with overrides** — Default paths (`.eden/proposals.db`,
   `.eden/results.db`, `.eden/proposals`, `workspace`) work via bootstrap
   symlinks. `run_planner()` accepts path overrides for non-standard layouts.

6. **Deduplication inside `iter_trial_notifications()`** — The seen-trials set
   is encapsulated in the iterator, removing boilerplate from callers.

7. **Explicit DB connection helpers** — Instead of a single `connect_db(path)`
   that infers access mode from the filename (breaks if config overrides the
   default DB path), provide `connect_results_db(path)` (read-only; expects
   the DB to already use DELETE journal mode) and `connect_proposals_db(path)`
   (read-write, WAL journal). This avoids silent wrong-mode connections when
   `results_db` or `proposals_db`
   paths are customized in config.

## API surface

```python
# --- Data types ---

@dataclass(frozen=True)
class Proposal:
    slug: str
    priority: float
    plan_text: str
    parent_commits: list[str]
    log_fields: dict[str, object] = field(default_factory=dict)

@dataclass
class PlannerContext:
    head_sha: str
    parallel_trials: int
    results_db: str
    proposals_db: str
    proposals_dir: str
    workspace: str
    logger: logging.Logger
    def get_trial(self, trial_id: int) -> dict | None: ...
    def get_all_trials(self, *, order_by: str | None = None) -> list[dict]: ...

# --- Logging ---
def configure_logging(name: str = "planner") -> logging.Logger
def log_event(logger: logging.Logger, **fields: object) -> None

# --- Low-level utilities ---
def get_head_sha(workspace: str = "workspace") -> str
def connect_results_db(path: str) -> sqlite3.Connection   # read-only, DELETE journal
def connect_proposals_db(path: str) -> sqlite3.Connection  # read-write, WAL journal
def create_proposal(*, proposals_db, proposals_dir, priority, slug, parent_commits, plan_text) -> None
def get_trial(results_db: str, trial_id: int) -> dict | None
def get_all_trials(results_db: str, *, order_by: str | None = None) -> list[dict]
    # order_by is a raw SQL fragment — trusted internal input only, not user-facing
def iter_trial_notifications() -> Iterator[int]
    # Deduplication is process-local; resets on planner restart (matches current behavior)

# --- High-level runner ---
def run_planner(
    *,
    make_initial_proposals: Callable[[PlannerContext], list[Proposal]],
    make_reactive_proposal: Callable[[PlannerContext, int, dict], Proposal | None],
    parallel_trials: int = 1,
    workspace: str = "workspace",
    proposals_db: str = ".eden/proposals.db",
    results_db: str = ".eden/results.db",
    proposals_dir: str = ".eden/proposals",
) -> None
```

## Files to create/modify

### 1. Create `src/eden/planner_kit.py` (~180 lines)

Sections:
- `_PlannerFormatter` — JSON-line formatter matching existing `_log()` output
- `configure_logging` / `log_event` — structured logging to `$EDEN_LOG_DIR/plan.log`
- `Proposal` / `PlannerContext` dataclasses
- Utility functions: `get_head_sha`, `connect_results_db`, `connect_proposals_db`, `create_proposal`, `get_trial`, `get_all_trials`, `iter_trial_notifications`
- `run_planner` — the main loop

`run_planner` flow:
1. `configure_logging()`, `get_head_sha(workspace)`, build `PlannerContext`
2. Log `startup` event
3. Call `make_initial_proposals(ctx)` → iterate returned proposals
4. For each: call `create_proposal(...)`, log `propose` with `slug`, `priority`, `parent`, plus `proposal.log_fields`
5. Enter `iter_trial_notifications()` loop
6. For each `trial_id`: log `notify`, call `get_trial()`, log `result` (with `commit=trial["commit_sha"]` + all metric columns), call `make_reactive_proposal(ctx, counter, trial)`
7. If proposal returned: `create_proposal(...)`, log `react` with `slug`, `priority`, `parent`, `trial_id`, plus `proposal.log_fields`

Metric extraction for `result` logging: filter trial dict keys to exclude standard columns (`trial_id`, `commit_sha`, `status`, `parent_commits`, `branch`, `artifacts_uri`, `description`, `timestamp`), log the rest.

### 2. Rewrite `tests/fixtures/experiment/planner/plan.py` (~35 lines)

```python
"""Planner for the seed-sum experiment."""
from __future__ import annotations
from eden.planner_kit import Proposal, PlannerContext, run_planner

def _make_initial_proposals(ctx: PlannerContext) -> list[Proposal]:
    return [
        Proposal(
            slug=f"seed-{i}-init",
            priority=float(i),
            plan_text=f"Append seed {i}",
            parent_commits=[ctx.head_sha],
            log_fields={"seed": i},
        )
        for i in range(ctx.parallel_trials + 2)
    ]

def _make_reactive_proposal(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal:
    return Proposal(
        slug=f"seed-{proposal_index}-t{trial['trial_id']}",
        priority=float(trial["score"]),
        plan_text=f"Append seed {proposal_index}",
        parent_commits=[trial["commit_sha"]],
        log_fields={"seed": proposal_index},
    )

if __name__ == "__main__":
    run_planner(
        make_initial_proposals=_make_initial_proposals,
        make_reactive_proposal=_make_reactive_proposal,
    )
```

### 3. Rewrite `example/data-fitting/planner/plan.py` (~120 lines)

Imports from `eden.planner_kit`. Uses `run_planner()` with callbacks.
Keeps experiment-specific helpers in the file:
- `INITIAL_STRATEGIES` list
- `_SYSTEM_PROMPT` constant
- `read_trial_artifact()`, `format_history()`, `generate_claude_proposal()`
- `_session_started` state for Claude CLI session continuity

```python
"""Planner for the data-fitting experiment."""
from __future__ import annotations
from eden.planner_kit import Proposal, PlannerContext, run_planner

# ... INITIAL_STRATEGIES, _SYSTEM_PROMPT, helper functions stay ...

def _make_initial_proposals(ctx: PlannerContext) -> list[Proposal]:
    batch = INITIAL_STRATEGIES[:ctx.parallel_trials + 2]
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
```

The data-fitting test (`test_data_fitting_demo.py`) does not check log field
names — it only verifies eval math, model correctness, and trial counts in
the AI integration test. No log format constraints.

### 4. Create `tests/test_planner_kit.py` (~150 lines)

Unit tests for:
- `get_head_sha` — mock subprocess, verify output
- `connect_results_db` — verify read-only mode, DELETE journal
- `connect_proposals_db` — verify read-write mode, WAL journal
- `connect_results_db` with non-default filename — verify mode is correct regardless of path
- `create_proposal` — verify plan.md written and DB row inserted
- `get_trial` / `get_all_trials` — verify row returns with all columns
- `iter_trial_notifications` — mock stdin, verify deduplication and parsing
- `configure_logging` / `log_event` — verify JSON format matches fixture expectations
- `run_planner` — integration test with mock callbacks and temp DB
- `run_planner` counter invariant — verify first reactive `proposal_index == len(initial_proposals)`
- `Proposal.log_fields` reserved key rejection — verify `ValueError` raised for `slug`, `priority`, `parent`, `event`, `trial_id`

### 5. No changes to existing files

- `src/eden/planner.py` — subprocess lifecycle, unchanged
- `src/eden/orchestrator.py` — unchanged
- `tests/test_e2e.py` — must pass as-is (the correctness constraint)
- `tests/test_data_fitting_demo.py` — must pass as-is

## Logging compatibility constraint

`test_e2e.py::_verify_plan_log` parses `plan.log` and checks exact field names:
- `startup`: `event`, `parallel_trials`, `head`
- `propose`: `event`, `seed`, `slug`, `priority`, `parent`
- `notify`: `event`, `trial_id`
- `result`: `event`, `trial_id`, `commit`, `score`
- `react`: `event`, `seed`, `slug`, `priority`, `parent`, `trial_id`

The runner must produce these exact fields. `seed` comes from `Proposal.log_fields`.
`commit` is mapped from `trial["commit_sha"]`. `score` comes from metric columns in the trial dict.

## Verification

1. `uv run -m pytest -q tests/test_planner_kit.py` — new unit tests pass
2. `uv run -m pytest -q tests/test_e2e.py` — E2E test passes unchanged
3. `uv run -m pytest -q tests/test_data_fitting_demo.py` — data-fitting tests pass unchanged
4. `uv run ruff check .` — lint clean
5. `uv run pyright` — type checking passes
