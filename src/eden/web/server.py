"""Starlette application for the EDEN Web UI."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles


@dataclass(frozen=True)
class ExperimentPaths:
    """Resolved filesystem paths for an experiment."""

    results_db: Path
    proposals_db: Path
    session_log: Path
    artifacts_dir: Path
    proposals_dir: Path
    config_yaml: Path


@dataclass(frozen=True)
class ExperimentInfo:
    """Static experiment metadata loaded at server startup."""

    metrics_schema: dict[str, str]
    objective_expr: str
    objective_direction: str
    parallel_trials: int
    paths: ExperimentPaths


def _resolve_paths_from_config(config_path: Path) -> ExperimentPaths:
    """Derive experiment file paths from a config file using load_config."""
    from ..config import load_config

    config = load_config(config_path)
    session_log = config.experiment_root / ".eden" / "session.log"
    config_yaml = config.config_path
    return ExperimentPaths(
        results_db=config.results_db,
        proposals_db=config.proposals_db,
        session_log=session_log,
        artifacts_dir=config.artifacts_dir,
        proposals_dir=config.proposals_dir,
        config_yaml=config_yaml,
    )


def _resolve_paths_from_dir(experiment_dir: Path) -> ExperimentPaths:
    """Derive experiment file paths from an exported experiment directory."""
    eden_dir = experiment_dir / ".eden"
    planner_eden = experiment_dir / "planner" / ".eden"
    return ExperimentPaths(
        results_db=eden_dir / "results.db",
        proposals_db=planner_eden / "proposals.db",
        session_log=eden_dir / "session.log",
        artifacts_dir=eden_dir / "artifacts",
        proposals_dir=planner_eden / "proposals",
        config_yaml=eden_dir / "config.yaml",
    )


def _load_info_from_config(config_path: Path, paths: ExperimentPaths) -> ExperimentInfo:
    """Load experiment metadata from a config file."""
    from ..config import load_config

    config = load_config(config_path)
    return ExperimentInfo(
        metrics_schema=config.metrics_schema,
        objective_expr=config.objective.expr,
        objective_direction=config.objective.direction.value,
        parallel_trials=config.parallel_trials,
        paths=paths,
    )


def _load_info_from_yaml(config_yaml: Path, paths: ExperimentPaths) -> ExperimentInfo:
    """Load experiment metadata from a raw config.yaml in an export directory."""
    import yaml

    if not config_yaml.exists():
        return ExperimentInfo(
            metrics_schema={},
            objective_expr="",
            objective_direction="maximize",
            parallel_trials=1,
            paths=paths,
        )
    with config_yaml.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    metrics = raw.get("metrics_schema", {})
    obj = raw.get("objective", {})
    return ExperimentInfo(
        metrics_schema=metrics if isinstance(metrics, dict) else {},
        objective_expr=obj.get("expr", "") if isinstance(obj, dict) else "",
        objective_direction=obj.get("direction", "maximize") if isinstance(obj, dict) else "maximize",
        parallel_trials=raw.get("parallel_trials", 1),
        paths=paths,
    )


# ---------------------------------------------------------------------------
# Liveness heuristic (D11)
# ---------------------------------------------------------------------------

_LIVENESS_WINDOW_SECONDS = 300  # 5 minutes


def _detect_status(session_log: Path) -> str:
    """Return 'live', 'ended', or 'unknown' based on session.log state."""
    if not session_log.exists():
        return "unknown"

    try:
        stat = session_log.stat()
    except OSError:
        return "unknown"

    # Check the last few KB for session_ended event.
    try:
        size = stat.st_size
        read_size = min(size, 8192)
        with session_log.open("rb") as f:
            if read_size < size:
                f.seek(size - read_size)
            tail = f.read(read_size).decode("utf-8", errors="replace")
        for line in reversed(tail.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("event") == "session_ended":
                    return "ended"
            except (json.JSONDecodeError, TypeError):
                continue
    except OSError:
        return "unknown"

    age = time.time() - stat.st_mtime
    if age < _LIVENESS_WINDOW_SECONDS:
        return "live"
    return "unknown"


# ---------------------------------------------------------------------------
# proposals.db WAL-safe snapshot (D10)
# ---------------------------------------------------------------------------


class _ProposalsCache:
    """Cache for the proposals.db WAL-safe snapshot.

    SQLite WAL-mode databases interact with file metadata in ways that make
    mtime-based ETags unreliable (read-only connections can update mtime on
    some filesystems).  Instead, we cache the snapshot bytes and derive the
    ETag from a content hash.
    """

    def __init__(self, proposals_db: Path) -> None:
        self._db_path = proposals_db
        self._data: bytes = b""
        self._etag: str = ""
        self._version: int = 0

    def _create_snapshot(self) -> bytes:
        """Create a WAL-checkpointed snapshot of proposals.db."""
        import os
        import tempfile

        source = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            dest = sqlite3.connect(tmp_path)
            try:
                source.backup(dest)
            finally:
                dest.close()
            data = Path(tmp_path).read_bytes()
            Path(tmp_path).unlink(missing_ok=True)
            return data
        finally:
            source.close()

    def refresh(self) -> None:
        """Regenerate the cached snapshot, bumping the version if content changed."""
        data = self._create_snapshot()
        if data != self._data:
            self._data = data
            self._version += 1
            self._etag = f'"{self._version}-{len(data)}"'

    @property
    def etag(self) -> str:
        """Return the current ETag."""
        if not self._etag:
            self.refresh()
        return self._etag

    @property
    def data(self) -> bytes:
        """Return the current snapshot bytes."""
        if not self._data:
            self.refresh()
        return self._data


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _make_info_handler(info: ExperimentInfo) -> Route:
    """Create the /experiment/info route."""

    async def info_endpoint(request: Request) -> JSONResponse:
        paths = info.paths
        files = {
            "results_db": {"path": "/experiment/data/results.db", "available": paths.results_db.exists()},
            "proposals_db": {"path": "/experiment/data/proposals.db", "available": paths.proposals_db.exists()},
            "session_log": {"path": "/experiment/data/session.log", "available": paths.session_log.exists()},
            "artifacts_dir": {"path": "/experiment/data/artifacts", "available": paths.artifacts_dir.exists()},
            "proposals_dir": {"path": "/experiment/data/proposals", "available": paths.proposals_dir.exists()},
        }
        status = _detect_status(paths.session_log)
        return JSONResponse(
            {
                "metrics_schema": info.metrics_schema,
                "objective": {"expr": info.objective_expr, "direction": info.objective_direction},
                "parallel_trials": info.parallel_trials,
                "status": status,
                "files": files,
            }
        )

    return Route("/experiment/info", info_endpoint)


def _make_artifact_listing_handler(artifacts_dir: Path) -> Route:
    """Create the /experiment/data/artifacts/{trial_id}/_list endpoint."""

    async def list_artifacts(request: Request) -> JSONResponse:
        trial_id = request.path_params["trial_id"]
        trial_dir = artifacts_dir / f"trial-{trial_id}"
        if not trial_dir.is_dir():
            return JSONResponse({"files": []})
        files = sorted(f.name for f in trial_dir.iterdir() if f.is_file())
        return JSONResponse({"files": files})

    return Route(
        "/experiment/data/artifacts/{trial_id}/_list",
        list_artifacts,
    )


def _make_proposals_snapshot_handler(proposals_db: Path) -> Route:
    """Create the /experiment/data/proposals.db WAL-safe snapshot route."""
    cache = _ProposalsCache(proposals_db)

    async def proposals_snapshot(request: Request) -> Response:
        if not proposals_db.exists():
            return Response(status_code=404)

        # Always refresh so we serve current data.
        cache.refresh()
        etag = cache.etag

        # Conditional GET: return 304 if ETag matches.
        if_none_match = request.headers.get("if-none-match", "")
        if etag and if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})

        # HEAD request: return headers without body.
        if request.method == "HEAD":
            return Response(
                headers={
                    "ETag": etag,
                    "Content-Type": "application/x-sqlite3",
                    "Cache-Control": "no-cache",
                },
            )

        data = cache.data
        return Response(
            content=data,
            media_type="application/x-sqlite3",
            headers={
                "ETag": etag,
                "Cache-Control": "no-cache",
                "Content-Length": str(len(data)),
            },
        )

    return Route("/experiment/data/proposals.db", proposals_snapshot, methods=["GET", "HEAD"])


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    config_path: Path | None = None,
    experiment_dir: Path | None = None,
    dev: bool = False,
    spa_dir: Path | None = None,
) -> Starlette:
    """Create the EDEN Web UI Starlette application.

    Args:
        config_path: Path to .eden/config.yaml (live mode).
        experiment_dir: Path to an exported experiment directory (post-run mode).
        dev: Enable CORS for Vite dev server.
        spa_dir: Path to built SPA directory (packages/web-ui/dist/).
    """
    if config_path is not None:
        paths = _resolve_paths_from_config(config_path)
        info = _load_info_from_config(config_path, paths)
    elif experiment_dir is not None:
        paths = _resolve_paths_from_dir(experiment_dir)
        info = _load_info_from_yaml(paths.config_yaml, paths)
    else:
        raise ValueError("Either config_path or experiment_dir must be provided.")

    routes: list[Route | Mount] = [
        _make_info_handler(info),
        _make_proposals_snapshot_handler(paths.proposals_db),
        _make_artifact_listing_handler(paths.artifacts_dir),
    ]

    # Serve experiment data files (results.db, session.log, artifacts, proposals docs).
    # proposals.db is handled by the snapshot route above, so StaticFiles serves the rest.
    data_mounts: list[Route | Mount] = []
    if paths.artifacts_dir.exists():
        data_mounts.append(
            Mount("/experiment/data/artifacts", app=StaticFiles(directory=str(paths.artifacts_dir)))
        )
    if paths.proposals_dir.exists():
        data_mounts.append(
            Mount("/experiment/data/proposals", app=StaticFiles(directory=str(paths.proposals_dir)))
        )

    # Serve individual data files via a parent StaticFiles mount.
    # We need results.db, session.log, config.yaml from the .eden directory.
    eden_dir = paths.results_db.parent
    if eden_dir.exists():
        data_mounts.append(Mount("/experiment/data", app=StaticFiles(directory=str(eden_dir))))

    routes.extend(data_mounts)

    # Serve SPA static files.
    if spa_dir and spa_dir.exists() and not dev:
        routes.append(Mount("/", app=StaticFiles(directory=str(spa_dir), html=True)))

    middleware = []
    if dev:
        middleware.append(
            Middleware(
                CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"]
            )
        )

    return Starlette(routes=routes, middleware=middleware)
