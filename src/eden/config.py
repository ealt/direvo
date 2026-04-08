"""Session config loading and validation."""

from __future__ import annotations

import re
import shlex
import sqlite3
from pathlib import Path
from string import Formatter

import yaml

from .models import DockerConfig, FilePermissionGrant, ObjectiveDirection, ObjectiveSpec, SessionConfig

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$")
_SQLITE_TYPES = {"integer", "real", "text"}


class ConfigError(ValueError):
    """Raised when config validation fails."""


def load_config(config_path: str | Path) -> SessionConfig:
    """Load and validate a session config file.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Parsed and validated session config.

    Raises:
        ConfigError: If the file is missing or invalid.
    """
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).absolute()
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file is not valid YAML: {path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping.")

    experiment_root = _infer_experiment_root(path)
    planner_root = _resolve_path(experiment_root, _require_str(raw, "planner_root"))
    workspace_root = _resolve_path(planner_root, _string_default(raw, "workspace", "workspace"))
    _validate_containment(
        experiment_root=experiment_root,
        planner_root=planner_root,
        workspace_root=workspace_root,
    )
    metrics_schema = _validate_metrics_schema(raw.get("metrics_schema"))
    objective = _validate_objective(raw.get("objective"))
    file_permissions = _validate_file_permissions(experiment_root, raw.get("file_permissions", []))

    convergence_window = raw.get("convergence_window")
    if convergence_window is not None and (
        not isinstance(convergence_window, int) or convergence_window <= 0
    ):
        raise ConfigError("convergence_window must be a positive integer.")

    target_condition = raw.get("target_condition")
    if target_condition is not None and not isinstance(target_condition, str):
        raise ConfigError("target_condition must be a string when provided.")
    if target_condition is not None:
        target_condition = target_condition.strip()
        if not target_condition:
            raise ConfigError("target_condition must be a non-empty string when provided.")

    plan_notify_template = _validate_plan_notify_template(
        raw.get("plan_notify_template", "Trial completed. ID: {trial_id}")
    )
    _validate_sql_expressions(
        metrics_schema=metrics_schema,
        objective_expr=objective.expr,
        target_condition=target_condition,
    )

    docker = _validate_docker_config(experiment_root, raw.get("docker"))

    return SessionConfig(
        config_path=path,
        experiment_root=experiment_root,
        planner_root=planner_root,
        workspace_root=workspace_root,
        parallel_trials=_require_positive_int(raw, "parallel_trials"),
        evaluate_command=_resolve_command(
            experiment_root, _require_str(raw, "evaluate_command")
        ),
        max_trials=_require_positive_int(raw, "max_trials"),
        max_wall_time_seconds=_parse_duration(_require_str(raw, "max_wall_time")),
        metrics_schema=metrics_schema,
        objective=objective,
        convergence_window=convergence_window,
        target_condition=target_condition,
        results_db=_resolve_path(
            experiment_root, raw.get("results_db", ".eden/results.db")
        ),
        proposals_db=_resolve_path(
            planner_root, raw.get("proposals_db", ".eden/proposals.db")
        ),
        proposals_dir=_resolve_path(
            planner_root, raw.get("proposals_dir", ".eden/proposals")
        ),
        artifacts_dir=_resolve_path(
            experiment_root, raw.get("artifacts_dir", ".eden/artifacts")
        ),
        implement_command=_resolve_command(
            experiment_root,
            _validate_implement_command(raw.get("implement_command")),
        ),
        plan_command=_resolve_command_optional(
            planner_root, _optional_str(raw, "plan_command")
        ),
        file_permissions=file_permissions,
        plan_notify_template=plan_notify_template,
        plan_start_timeout_sec=_positive_int_default(
            raw, "plan_start_timeout_sec", 60
        ),
        implement_timeout_sec=_positive_int_default(
            raw, "implement_timeout_sec", 1800
        ),
        evaluation_timeout_sec=_positive_int_default(
            raw, "evaluation_timeout_sec", 1800
        ),
        sqlite_busy_timeout_ms=_positive_int_default(
            raw, "sqlite_busy_timeout_ms", 5000
        ),
        proposal_retry_priority_delta=_positive_float_default(
            raw, "proposal_retry_priority_delta", 0.1
        ),
        docker=docker,
    )


def _infer_experiment_root(config_path: Path) -> Path:
    """Infer the experiment root from the config path."""
    if config_path.parent.name in (".eden", ".direvo"):
        return config_path.parent.parent
    return config_path.parent


def _resolve_path(base: Path, value: str) -> Path:
    """Resolve a configured path relative to a base directory."""
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _resolve_command(experiment_root: Path, command: str) -> str:
    """Resolve file paths in a command string against the experiment root.

    Each shell token is checked: if the experiment root contains a file at that
    relative path, the token is replaced with its absolute path.  Tokens that
    do not correspond to existing files are left unchanged.
    """
    tokens = shlex.split(command)
    resolved: list[str] = []
    for token in tokens:
        candidate = experiment_root / token
        if not Path(token).is_absolute() and candidate.is_file():
            resolved.append(str(candidate))
        else:
            resolved.append(token)
    return shlex.join(resolved)


def _resolve_command_optional(experiment_root: Path, command: str | None) -> str | None:
    """Resolve an optional command string."""
    if command is None:
        return None
    return _resolve_command(experiment_root, command)


def _validate_containment(
    *, experiment_root: Path, planner_root: Path, workspace_root: Path
) -> None:
    """Ensure configured roots follow the ownership hierarchy."""
    if planner_root == experiment_root or experiment_root not in planner_root.parents:
        raise ConfigError("planner_root must be under experiment_root.")
    if workspace_root == planner_root or planner_root not in workspace_root.parents:
        raise ConfigError("workspace must be under planner_root.")


def _validate_file_permissions(
    experiment_root: Path, value: object
) -> tuple[FilePermissionGrant, ...]:
    """Validate read-only cross-scope file grants."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("file_permissions must be a list when provided.")

    grants: list[FilePermissionGrant] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError("file_permissions entries must be mappings.")
        path = entry.get("path")
        grant = entry.get("grant")
        if not isinstance(path, str) or not path.strip():
            raise ConfigError("file_permissions path must be a non-empty string.")
        if not isinstance(grant, str) or grant not in {"planner", "implementer"}:
            raise ConfigError('file_permissions grant must be "planner" or "implementer".')

        normalized = Path(path.strip())
        if normalized.is_absolute():
            raise ConfigError("file_permissions path must be relative to experiment_root.")
        if ".." in normalized.parts:
            raise ConfigError("file_permissions path must not contain '..'.")
        if not normalized.parts:
            raise ConfigError("file_permissions path must not be empty.")
        if normalized.parts[0] == ".eden":
            raise ConfigError("file_permissions path must not target .eden.")

        source = (experiment_root / normalized).resolve()
        if experiment_root not in source.parents:
            raise ConfigError("file_permissions path escapes experiment_root.")
        if not source.exists() or not source.is_file():
            raise ConfigError(f"file_permissions path does not reference an existing file: {path}")

        grants.append(FilePermissionGrant(path=normalized.as_posix(), actor=grant))
    return tuple(grants)


def _require_str(data: dict[str, object], key: str) -> str:
    """Require a non-empty string config value."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string.")
    return value


def _optional_str(data: dict[str, object], key: str) -> str | None:
    """Return an optional string config value."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string when provided.")
    return value.strip()


def _string_default(data: dict[str, object], key: str, default: str) -> str:
    """Return a string config value with a default."""
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string.")
    return value.strip()


def _require_positive_int(data: dict[str, object], key: str) -> int:
    """Require a positive integer config value."""
    value = data.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer.")
    return value


def _positive_int_default(data: dict[str, object], key: str, default: int) -> int:
    """Return a positive integer config value with a default."""
    value = data.get(key, default)
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer.")
    return value


def _positive_float_default(
    data: dict[str, object], key: str, default: float
) -> float:
    """Return a positive float config value with a default."""
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{key} must be a positive number.")
    return float(value)


def _validate_metrics_schema(value: object) -> dict[str, str]:
    """Validate the dynamic metrics schema block."""
    if not isinstance(value, dict) or not value:
        raise ConfigError("metrics_schema must be a non-empty mapping.")
    schema: dict[str, str] = {}
    for key, type_name in value.items():
        if not isinstance(key, str) or not _IDENTIFIER_RE.match(key):
            raise ConfigError(f"Invalid metrics_schema key: {key!r}")
        if not isinstance(type_name, str) or type_name.lower() not in _SQLITE_TYPES:
            raise ConfigError(
                f"metrics_schema[{key!r}] must be one of: {sorted(_SQLITE_TYPES)}"
            )
        schema[key] = type_name.lower()
    return schema


def _validate_objective(value: object) -> ObjectiveSpec:
    """Validate the objective block."""
    if not isinstance(value, dict):
        raise ConfigError("objective must be a mapping.")
    expr = value.get("expr")
    direction = value.get("direction")
    if not isinstance(expr, str) or not expr.strip():
        raise ConfigError("objective.expr must be a non-empty string.")
    try:
        objective_direction = ObjectiveDirection(direction)
    except ValueError as exc:
        raise ConfigError("objective.direction must be maximize or minimize.") from exc
    return ObjectiveSpec(expr=expr.strip(), direction=objective_direction)


def _parse_duration(value: str) -> int:
    """Parse a compact duration like ``24h`` into seconds."""
    match = _DURATION_RE.match(value)
    if not match:
        raise ConfigError("max_wall_time must look like 30s, 5m, 24h, or 2d.")
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group("unit")]
    return int(match.group("value")) * scale


def _validate_implement_command(value: object) -> str:
    """Validate the execute command template."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("implement_command must be a non-empty string.")
    return value.strip()


def _validate_plan_notify_template(value: object) -> str:
    """Validate the planner notification template."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("plan_notify_template must be a non-empty string.")
    template = value.strip()
    try:
        fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name is not None}
    except ValueError as exc:
        raise ConfigError("plan_notify_template is not a valid format string.") from exc
    if "trial_id" not in fields:
        raise ConfigError("plan_notify_template must include the {trial_id} placeholder.")
    try:
        template.format(trial_id=1)
    except (IndexError, KeyError, ValueError) as exc:
        raise ConfigError("plan_notify_template is not a valid format string.") from exc
    return template


def _validate_sql_expressions(
    *,
    metrics_schema: dict[str, str],
    objective_expr: str,
    target_condition: str | None,
) -> None:
    """Validate SQL expressions against the dynamic trials schema."""
    with sqlite3.connect(":memory:") as connection:
        metric_columns = ", ".join(f"{name} {type_name.upper()}" for name, type_name in metrics_schema.items())
        connection.execute(
            f"""
            CREATE TABLE trials (
                trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_sha TEXT,
                parent_commits TEXT,
                branch TEXT,
                status TEXT,
                artifacts_uri TEXT,
                description TEXT,
                timestamp TEXT
                {", " if metric_columns else ""}{metric_columns}
            )
            """
        )
        try:
            connection.execute(f"SELECT ({objective_expr}) FROM trials LIMIT 0")
        except sqlite3.Error as exc:
            raise ConfigError(f"objective.expr is not a valid SQL expression: {objective_expr}") from exc
        if target_condition is not None:
            try:
                connection.execute(f"SELECT 1 FROM trials WHERE ({target_condition}) LIMIT 0")
            except sqlite3.Error as exc:
                raise ConfigError(f"target_condition is not a valid SQL WHERE clause: {target_condition}") from exc


_KNOWN_DOCKER_TOOLS = {"claude", "codex"}


def _validate_docker_config(
    experiment_root: Path, value: object
) -> DockerConfig | None:
    """Validate the optional docker configuration section."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("docker must be a mapping when provided.")

    tools = _validate_string_list(value.get("tools", []), "docker.tools")
    for tool in tools:
        if tool not in _KNOWN_DOCKER_TOOLS:
            raise ConfigError(
                f"Unknown docker tool: {tool!r}. Known tools: {sorted(_KNOWN_DOCKER_TOOLS)}"
            )

    dependencies = _validate_string_list(
        value.get("dependencies", []), "docker.dependencies"
    )
    pip_dependencies = _validate_string_list(
        value.get("pip_dependencies", []), "docker.pip_dependencies"
    )

    dockerfile = _validate_docker_path(
        experiment_root, value.get("dockerfile"), "docker.dockerfile"
    )
    entrypoint = _validate_docker_path(
        experiment_root, value.get("entrypoint"), "docker.entrypoint"
    )

    setup_command = value.get("setup_command")
    if setup_command is not None:
        if not isinstance(setup_command, str) or not setup_command.strip():
            raise ConfigError(
                "docker.setup_command must be a non-empty string when provided."
            )
        setup_command = setup_command.strip()

    export_disabled = False
    export_command: str | None = None
    if "export_command" in value:
        raw_export = value["export_command"]
        if raw_export is None:
            export_disabled = True
        elif isinstance(raw_export, str) and raw_export.strip():
            export_command = raw_export.strip()
        else:
            raise ConfigError(
                "docker.export_command must be a non-empty string or null."
            )

    image_name = value.get("image_name")
    if image_name is not None:
        if not isinstance(image_name, str) or not image_name.strip():
            raise ConfigError(
                "docker.image_name must be a non-empty string when provided."
            )
        image_name = image_name.strip()

    git_user_name, git_user_email = _validate_docker_git_config(
        value.get("git_config")
    )

    return DockerConfig(
        tools=tuple(tools),
        dependencies=tuple(dependencies),
        pip_dependencies=tuple(pip_dependencies),
        dockerfile=dockerfile,
        entrypoint=entrypoint,
        setup_command=setup_command,
        export_command=export_command,
        export_disabled=export_disabled,
        image_name=image_name,
        git_user_name=git_user_name,
        git_user_email=git_user_email,
    )


def _validate_string_list(value: object, key: str) -> list[str]:
    """Validate a config value as a list of non-empty strings."""
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key} entries must be non-empty strings.")
        result.append(item.strip())
    return result


def _validate_docker_path(
    experiment_root: Path, value: object, key: str
) -> Path | None:
    """Validate an optional path that must point to an existing file under experiment_root."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string when provided.")
    path = _resolve_path(experiment_root, value.strip())
    if not path.exists():
        raise ConfigError(f"{key} does not exist: {path}")
    if not path.is_file():
        raise ConfigError(f"{key} is not a file: {path}")
    if experiment_root not in path.resolve().parents and path.resolve() != experiment_root:
        raise ConfigError(f"{key} must be under experiment_root: {path}")
    return path


def _validate_docker_git_config(value: object) -> tuple[str, str]:
    """Validate the docker git_config section, returning (user_name, user_email)."""
    if value is None:
        return ("eden", "eden@experiment")
    if not isinstance(value, dict):
        raise ConfigError("docker.git_config must be a mapping when provided.")
    user_name = value.get("user_name", "eden")
    user_email = value.get("user_email", "eden@experiment")
    if not isinstance(user_name, str) or not user_name.strip():
        raise ConfigError("docker.git_config.user_name must be a non-empty string.")
    if not isinstance(user_email, str) or not user_email.strip():
        raise ConfigError("docker.git_config.user_email must be a non-empty string.")
    return (user_name.strip(), user_email.strip())
