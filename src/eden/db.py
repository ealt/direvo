"""SQLite helpers for results and proposal coordination."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .models import ObjectiveDirection, ProposalClaim, ProposalStatus, TrialStatus, TrialUpdate


class DatabaseError(RuntimeError):
    """Raised for database contract violations."""


@dataclass(slots=True)
class DatabaseManager:
    """Manage EDEN SQLite databases."""

    results_db: Path
    proposals_db: Path
    metrics_schema: dict[str, str]
    busy_timeout_ms: int
    results_journal_mode: str = "DELETE"
    proposals_journal_mode: str = "WAL"

    def initialize(self) -> None:
        """Initialize both SQLite databases."""
        self.results_db.parent.mkdir(parents=True, exist_ok=True)
        self.proposals_db.parent.mkdir(parents=True, exist_ok=True)

        with self._connection(self.results_db, self.results_journal_mode) as conn:
            conn.executescript(self._render_results_schema())
        with self._connection(self.proposals_db, self.proposals_journal_mode) as conn:
            conn.executescript(self._read_sql("proposals.sql"))

    def reserve_trial_id(self) -> int:
        """Reserve and return a new trial id."""
        with self._connection(self.results_db, self.results_journal_mode) as conn:
            cursor = conn.execute(
                "INSERT INTO trials (status, timestamp) VALUES (?, datetime('now'))",
                (TrialStatus.STARTING.value,),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("Failed to reserve a trial id.")
            return int(cursor.lastrowid)

    def update_trial(self, update: TrialUpdate) -> None:
        """Update a reserved trial row."""
        fields: dict[str, object] = {
            "status": update.status.value,
            "commit_sha": update.commit_sha,
            "parent_commits": update.parent_commits,
            "branch": update.branch,
            "artifacts_uri": update.artifacts_uri,
            "description": update.description,
        }
        for key in self.metrics_schema:
            fields[key] = update.metrics.get(key)

        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values()) + [update.trial_id]
        with self._connection(self.results_db, self.results_journal_mode) as conn:
            cursor = conn.execute(f"UPDATE trials SET {assignments} WHERE trial_id = ?", values)
            if cursor.rowcount != 1:
                raise DatabaseError(f"Unknown trial_id: {update.trial_id}")

    def claim_ready_proposal(self) -> ProposalClaim | None:
        """Atomically claim the highest-priority ready proposal."""
        with self._connection(self.proposals_db, self.proposals_journal_mode) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, priority, slug, parent_commits, artifacts_uri
                FROM proposals
                WHERE status = ?
                ORDER BY priority DESC, id ASC
                LIMIT 1
                """,
                (ProposalStatus.READY.value,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE proposals SET status = ? WHERE id = ?",
                (ProposalStatus.DISPATCHED.value, row["id"]),
            )
            conn.commit()
            return ProposalClaim(
                proposal_id=int(row["id"]),
                priority=float(row["priority"]),
                slug=str(row["slug"]),
                parent_commits=str(row["parent_commits"]),
                artifacts_uri=str(row["artifacts_uri"]),
            )

    def create_proposal(
        self,
        *,
        priority: float,
        slug: str,
        parent_commits: list[str],
        artifacts_uri: str,
        status: ProposalStatus = ProposalStatus.DRAFTING,
    ) -> int:
        """Insert a proposal row for tests or planner bootstrap."""
        with self._connection(self.proposals_db, self.proposals_journal_mode) as conn:
            cursor = conn.execute(
                """
                INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (priority, slug, json.dumps(parent_commits), artifacts_uri, status.value),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("Failed to create proposal.")
            return int(cursor.lastrowid)

    def list_trials(self, *, trial_ids: Iterable[int] | None = None) -> list[sqlite3.Row]:
        """Return trials ordered by id, optionally restricted to specific IDs."""
        ids = self._normalized_trial_ids(trial_ids)
        if ids == []:
            return []
        query = "SELECT * FROM trials"
        params: list[object] = []
        if ids is not None:
            placeholders = ", ".join("?" for _ in ids)
            query += f" WHERE trial_id IN ({placeholders})"
            params.extend(ids)
        query += " ORDER BY trial_id ASC"
        with self._connection(self.results_db, self.results_journal_mode) as conn:
            return list(conn.execute(query, params))

    def best_trial(
        self, expression: str, direction: ObjectiveDirection, *, trial_ids: Iterable[int] | None = None
    ) -> sqlite3.Row | None:
        """Return the best successful trial for an objective expression."""
        order = "DESC" if direction == ObjectiveDirection.MAXIMIZE else "ASC"
        ids = self._normalized_trial_ids(trial_ids)
        if ids == []:
            return None
        trial_filter = ""
        params: list[object] = [TrialStatus.SUCCESS.value]
        if ids is not None:
            placeholders = ", ".join("?" for _ in ids)
            trial_filter = f" AND trial_id IN ({placeholders})"
            params.extend(ids)
        query = f"""
            SELECT *
            FROM trials
            WHERE status = ?
              AND ({expression}) IS NOT NULL
              {trial_filter}
            ORDER BY ({expression}) {order}, trial_id ASC
            LIMIT 1
        """
        with self._connection(self.results_db, self.results_journal_mode) as conn:
            return conn.execute(query, params).fetchone()

    def update_proposal_status(
        self, proposal_id: int, status: ProposalStatus, *, priority: float | None = None
    ) -> None:
        """Update proposal status, optionally overriding priority."""
        with self._connection(self.proposals_db, self.proposals_journal_mode) as conn:
            if priority is None:
                cursor = conn.execute(
                    "UPDATE proposals SET status = ? WHERE id = ?",
                    (status.value, proposal_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE proposals SET status = ?, priority = ? WHERE id = ?",
                    (status.value, priority, proposal_id),
                )
            if cursor.rowcount != 1:
                raise DatabaseError(f"Unknown proposal id: {proposal_id}")

    def get_trial_row(self, trial_id: int) -> sqlite3.Row | None:
        """Fetch a trial row by id."""
        with self._connection(self.results_db, self.results_journal_mode) as conn:
            return conn.execute("SELECT * FROM trials WHERE trial_id = ?", (trial_id,)).fetchone()

    def get_proposal_row(self, proposal_id: int) -> sqlite3.Row | None:
        """Fetch a proposal row by id."""
        with self._connection(self.proposals_db, self.proposals_journal_mode) as conn:
            return conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()

    def objective_values(self, expression: str) -> list[float]:
        """Return objective values from completed trials in trial order."""
        query = f"""
            SELECT ({expression}) AS value
            FROM trials
            WHERE status IN (?, ?)
              AND ({expression}) IS NOT NULL
            ORDER BY trial_id ASC
        """
        with self._connection(self.results_db, self.results_journal_mode) as conn:
            rows = conn.execute(
                query,
                (TrialStatus.SUCCESS.value, TrialStatus.EVAL_ERROR.value),
            ).fetchall()
        values: list[float] = []
        for row in rows:
            value = row["value"]
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    def target_condition_met(self, condition: str) -> bool:
        """Return whether any completed trial satisfies a target condition."""
        query = f"""
            SELECT 1
            FROM trials
            WHERE status IN (?, ?)
              AND ({condition})
            LIMIT 1
        """
        with self._connection(self.results_db, self.results_journal_mode) as conn:
            row = conn.execute(
                query,
                (TrialStatus.SUCCESS.value, TrialStatus.EVAL_ERROR.value),
            ).fetchone()
        return row is not None

    def _connect(self, path: Path, journal_mode: str) -> sqlite3.Connection:
        """Create a configured SQLite connection."""
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return conn

    @contextmanager
    def _connection(self, path: Path, journal_mode: str) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite connection and always close it."""
        conn = self._connect(path, journal_mode)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _render_results_schema(self) -> str:
        """Render the results schema with dynamic metric columns."""
        metric_columns = "\n".join(
            f"    {name} {type_name.upper()}," for name, type_name in self.metrics_schema.items()
        )
        template = self._read_sql("results.sql")
        return template.replace("-- METRIC_COLUMNS", metric_columns)

    def _read_sql(self, name: str) -> str:
        """Read a packaged SQL file."""
        return resources.files("eden.sql").joinpath(name).read_text(encoding="utf-8")

    @staticmethod
    def _normalized_trial_ids(trial_ids: Iterable[int] | None) -> list[int] | None:
        """Normalize an optional trial-id filter to a concrete list."""
        if trial_ids is None:
            return None
        return [int(trial_id) for trial_id in trial_ids]
