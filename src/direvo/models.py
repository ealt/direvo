"""Core runtime models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ObjectiveDirection(StrEnum):
    """Optimization direction for an objective."""

    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class TrialStatus(StrEnum):
    """Status values for rows in ``results.db``."""

    STARTING = "starting"
    SUCCESS = "success"
    ERROR = "error"
    EVAL_ERROR = "eval_error"


class ProposalStatus(StrEnum):
    """Status values for rows in ``proposals.db``."""

    DRAFTING = "drafting"
    READY = "ready"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ObjectiveSpec:
    """Validated objective configuration."""

    expr: str
    direction: ObjectiveDirection


@dataclass(frozen=True)
class SessionConfig:
    """Fully validated session config."""

    config_path: Path
    experiment_root: Path
    planner_root: Path
    workspace_root: Path
    parallel_trials: int
    evaluate_command: str
    max_trials: int
    max_wall_time_seconds: int
    metrics_schema: dict[str, str]
    objective: ObjectiveSpec
    convergence_window: int | None
    target_condition: str | None
    results_db: Path
    proposals_db: Path
    proposals_dir: Path
    artifacts_dir: Path
    implement_command: str
    plan_command: str | None
    file_permissions: tuple[FilePermissionGrant, ...]
    plan_notify_template: str
    plan_start_timeout_sec: int
    implement_timeout_sec: int
    evaluation_timeout_sec: int
    sqlite_busy_timeout_ms: int
    proposal_retry_priority_delta: float


@dataclass(frozen=True)
class FilePermissionGrant:
    """A validated cross-scope file grant."""

    path: str
    actor: str


@dataclass(frozen=True)
class ProposalClaim:
    """Claimed proposal metadata returned from the dispatch queue."""

    proposal_id: int
    priority: float
    slug: str
    parent_commits: str
    artifacts_uri: str
    status: ProposalStatus = ProposalStatus.DISPATCHED


@dataclass(frozen=True)
class TrialUpdate:
    """Update payload for a reserved trial row."""

    trial_id: int
    status: TrialStatus
    commit_sha: str | None = None
    parent_commits: str | None = None
    branch: str | None = None
    artifacts_uri: str | None = None
    description: str | None = None
    metrics: dict[str, float | int | str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class TrialPaths:
    """Filesystem paths associated with a claimed trial."""

    worktree_path: Path
    trial_docs_path: Path
    artifacts_path: Path


@dataclass(frozen=True)
class ValidatedProposal:
    """A claimed proposal that passed orchestrator-side validation."""

    proposal_id: int
    priority: float
    slug: str
    parent_commits: list[str]
    artifacts_path: Path
