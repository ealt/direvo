"""Toolkit for building EDEN planner scripts.

Provides utilities and a high-level runner for planner subprocesses.
Planners propose experiments via a shared SQLite database and receive
trial completion notifications on stdin from the orchestrator.

Low-level utilities (connect_results_db, create_proposal, get_trial, etc.)
can be used independently for planners that need custom control flow.
The run_planner() function handles the boilerplate main loop.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_TRIAL_META_COLUMNS = frozenset(
    {"trial_id", "commit_sha", "status", "parent_commits", "branch", "artifacts_uri", "description", "timestamp"}
)

_PROPOSE_RESERVED = frozenset({"event", "slug", "priority", "parent"})
_REACT_RESERVED = _PROPOSE_RESERVED | {"trial_id"}


class _PlannerFormatter(logging.Formatter):
    """Emit one JSON object per log record from the ``_planner_fields`` extra."""

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = getattr(record, "_planner_fields", {})
        return json.dumps(fields, sort_keys=True)


def configure_logging(name: str = "planner") -> logging.Logger:
    """Configure a JSON-line logger that writes to ``plan.log``.

    Reads ``EDEN_LOG_DIR`` from the environment.  If unset, returns a
    logger with no handlers (logging calls become no-ops).

    Idempotent: repeated calls for the same *name* do not add duplicate
    handlers.
    """
    logger = logging.getLogger(name)
    logger.propagate = False
    log_dir = os.environ.get("EDEN_LOG_DIR")
    if log_dir and not logger.handlers:
        handler = logging.FileHandler(os.path.join(log_dir, "plan.log"))
        handler.setFormatter(_PlannerFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def log_event(logger: logging.Logger, **fields: object) -> None:
    """Emit a structured JSON log line with the given fields."""
    logger.info("", extra={"_planner_fields": fields})


# ---------------------------------------------------------------------------
# Artifact reading
# ---------------------------------------------------------------------------


def read_trial_artifact(artifacts_dir: str, trial_id: int, filename: str) -> str | None:
    """Read a text artifact file from a completed trial.

    Intended for text artifacts (plan.md, notes.md, eval_report.json).
    Returns the stripped file contents, or None if the file does not exist,
    is not a regular file, or cannot be decoded as UTF-8.
    """
    path = Path(artifacts_dir) / f"trial-{trial_id}" / filename
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        logging.debug("Failed to read artifact %s", path)
        return None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """A single experiment proposal to submit."""

    slug: str
    priority: float
    plan_text: str
    parent_commits: list[str]
    log_fields: dict[str, object] = field(default_factory=dict)


@dataclass
class PlannerContext:
    """Runtime context passed to planner callbacks."""

    head_sha: str
    parallel_trials: int
    results_db: str
    proposals_db: str
    proposals_dir: str
    artifacts_dir: str
    workspace: str
    logger: logging.Logger

    def get_trial(self, trial_id: int) -> dict | None:
        """Fetch a completed trial by ID."""
        return get_trial(self.results_db, trial_id)

    def get_all_trials(self, *, order_by: str | None = None) -> list[dict]:
        """Fetch all completed trials."""
        return get_all_trials(self.results_db, order_by=order_by)

    def read_trial_artifact(self, trial_id: int, filename: str) -> str | None:
        """Read a text artifact file from a completed trial."""
        return read_trial_artifact(self.artifacts_dir, trial_id, filename)


# ---------------------------------------------------------------------------
# Low-level utilities
# ---------------------------------------------------------------------------


def get_head_sha(workspace: str = "workspace") -> str:
    """Return the current HEAD commit SHA of the workspace repo."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def connect_results_db(path: str) -> sqlite3.Connection:
    """Open the results database read-only.

    Expects the database to already use DELETE journal mode (set by the
    orchestrator at initialization time).
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_proposals_db(path: str) -> sqlite3.Connection:
    """Open the proposals database for read-write access with WAL journal."""
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

    conn = connect_proposals_db(proposals_db)
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
    """Fetch a completed trial by ID, returning all columns."""
    conn = connect_results_db(results_db)
    try:
        row = conn.execute(
            "SELECT * FROM trials WHERE trial_id = ? AND status = 'success'",
            (trial_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_trials(results_db: str, *, order_by: str | None = None) -> list[dict]:
    """Fetch all completed trials.

    Args:
        results_db: Path to the results database.
        order_by: Optional raw SQL ORDER BY clause (trusted internal input).
    """
    suffix = f" ORDER BY {order_by}" if order_by else " ORDER BY trial_id ASC"
    conn = connect_results_db(results_db)
    try:
        rows = conn.execute(
            f"SELECT * FROM trials WHERE status = 'success'{suffix}",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def iter_trial_notifications() -> Iterator[int]:
    """Yield deduplicated trial IDs from stdin completion notifications.

    Parses the default notification template
    ``"Trial completed. ID: {trial_id}"``.  Deduplication is process-local
    and resets on planner restart.

    Planners that use a custom ``plan_notify_template`` in config should
    use the low-level utilities with a custom parsing loop instead.
    """
    seen: set[int] = set()
    for line in sys.stdin:
        line = line.strip()
        if not line or "Trial completed" not in line:
            continue
        try:
            trial_id = int(line.split(":")[-1].strip())
        except (ValueError, IndexError):
            continue
        if trial_id in seen:
            continue
        seen.add(trial_id)
        yield trial_id


# ---------------------------------------------------------------------------
# Agent session
# ---------------------------------------------------------------------------


@dataclass
class AgentSession(ABC):
    """Base class for persistent CLI agent sessions.

    Subclasses define how to build the CLI command for a given prompt.
    The base class handles subprocess execution, timeout, error handling,
    and session-started state tracking.
    """

    timeout: int = 120
    _started: bool = field(default=False, init=False, repr=False)

    @abstractmethod
    def _build_command(self, prompt: str) -> list[str]:
        """Build the CLI command list for the given prompt."""
        ...

    def generate(self, prompt: str) -> str | None:
        """Send a prompt to the agent and return the response.

        Returns None on timeout, missing CLI binary, non-zero exit, or
        empty output.  Logs failures at DEBUG level for diagnostics.
        """
        cmd = self._build_command(prompt)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
            if result.returncode == 0 and result.stdout.strip():
                self._started = True
                return result.stdout.strip()
            logging.debug("Agent CLI returned rc=%d: %s", result.returncode, result.stderr[:200])
        except subprocess.TimeoutExpired:
            logging.debug("Agent CLI timed out after %ds", self.timeout)
        except FileNotFoundError:
            logging.debug("Agent CLI binary not found: %s", cmd[0])
        return None


@dataclass
class ClaudeSession(AgentSession):
    """Claude CLI session with automatic session continuity.

    On the first call, starts a new session with optional system prompt
    configuration.  Subsequent calls append ``-c`` to continue the session.

    System prompt flags (applied only on the first successful call):

    - *append_system_prompt_file*: ``--append-system-prompt-file`` (additive,
      preserves Claude's built-in capabilities — recommended)
    - *append_system_prompt*: ``--append-system-prompt`` (additive, inline)
    - *system_prompt*: ``--system-prompt`` (full replacement)
    - *system_prompt_file*: ``--system-prompt-file`` (full replacement from file)

    At most one should be set.  If multiple are set, ``__post_init__``
    raises ``ValueError``.
    """

    append_system_prompt_file: Path | None = None
    append_system_prompt: str | None = None
    system_prompt: str | None = None
    system_prompt_file: Path | None = None

    def __post_init__(self) -> None:
        opts = [
            self.append_system_prompt_file,
            self.append_system_prompt,
            self.system_prompt,
            self.system_prompt_file,
        ]
        if sum(o is not None for o in opts) > 1:
            raise ValueError("ClaudeSession accepts at most one system prompt option")

    def _build_command(self, prompt: str) -> list[str]:
        cmd = ["claude", "-p", prompt]
        if self._started:
            cmd.append("-c")
        else:
            if self.append_system_prompt_file is not None:
                cmd.extend(["--append-system-prompt-file", str(self.append_system_prompt_file)])
            elif self.append_system_prompt is not None:
                cmd.extend(["--append-system-prompt", self.append_system_prompt])
            elif self.system_prompt_file is not None:
                cmd.extend(["--system-prompt-file", str(self.system_prompt_file)])
            elif self.system_prompt is not None:
                cmd.extend(["--system-prompt", self.system_prompt])
        return cmd


# ---------------------------------------------------------------------------
# High-level runner
# ---------------------------------------------------------------------------


def _check_reserved_keys(log_fields: dict[str, object], reserved: frozenset[str], event: str) -> None:
    """Raise ValueError if log_fields contains runner-owned keys."""
    conflicts = reserved & log_fields.keys()
    if conflicts:
        raise ValueError(f"Proposal.log_fields contains reserved keys for '{event}' event: {sorted(conflicts)}")


def run_planner(
    *,
    make_initial_proposals: Callable[[PlannerContext], list[Proposal]],
    make_reactive_proposal: Callable[[PlannerContext, int, dict], Proposal | None],
    parallel_trials: int = 1,
    workspace: str = "workspace",
    proposals_db: str = ".eden/proposals.db",
    results_db: str = ".eden/results.db",
    proposals_dir: str = ".eden/proposals",
    artifacts_dir: str = ".eden/artifacts",
) -> None:
    """Run the standard planner main loop.

    1. Resolve HEAD SHA from the workspace git repo.
    2. Call *make_initial_proposals* to get the initial batch.
    3. Submit all initial proposals.
    4. Enter the notification loop: for each completed trial, call
       *make_reactive_proposal* and submit the result if not None.

    The *proposal_index* passed to *make_reactive_proposal* starts at
    ``len(initial_proposals)`` and increments by 1 for each subsequent
    reactive call, preserving global sequential ordering.
    """
    logger = configure_logging()
    head_sha = get_head_sha(workspace)

    ctx = PlannerContext(
        head_sha=head_sha,
        parallel_trials=parallel_trials,
        results_db=results_db,
        proposals_db=proposals_db,
        proposals_dir=proposals_dir,
        artifacts_dir=artifacts_dir,
        workspace=workspace,
        logger=logger,
    )

    log_event(logger, event="startup", parallel_trials=parallel_trials, head=head_sha)

    # --- Initial proposals ---
    proposals = make_initial_proposals(ctx)
    for proposal in proposals:
        _check_reserved_keys(proposal.log_fields, _PROPOSE_RESERVED, "propose")
        create_proposal(
            proposals_db=proposals_db,
            proposals_dir=proposals_dir,
            priority=proposal.priority,
            slug=proposal.slug,
            parent_commits=proposal.parent_commits,
            plan_text=proposal.plan_text,
        )
        log_event(
            logger,
            event="propose",
            slug=proposal.slug,
            priority=proposal.priority,
            parent=proposal.parent_commits[0],
            **proposal.log_fields,
        )

    # --- Reactive loop ---
    proposal_index = len(proposals)

    for trial_id in iter_trial_notifications():
        log_event(logger, event="notify", trial_id=trial_id)

        trial = get_trial(results_db, trial_id)
        if trial is None or trial["commit_sha"] is None:
            continue

        # Log result with all metric columns
        metrics = {k: v for k, v in trial.items() if k not in _TRIAL_META_COLUMNS}
        log_event(logger, event="result", trial_id=trial_id, commit=trial["commit_sha"], **metrics)

        proposal = make_reactive_proposal(ctx, proposal_index, trial)
        proposal_index += 1
        if proposal is not None:
            _check_reserved_keys(proposal.log_fields, _REACT_RESERVED, "react")
            create_proposal(
                proposals_db=proposals_db,
                proposals_dir=proposals_dir,
                priority=proposal.priority,
                slug=proposal.slug,
                parent_commits=proposal.parent_commits,
                plan_text=proposal.plan_text,
            )
            log_event(
                logger,
                event="react",
                slug=proposal.slug,
                priority=proposal.priority,
                parent=proposal.parent_commits[0],
                trial_id=trial_id,
                **proposal.log_fields,
            )
