import textwrap
from pathlib import Path

import pytest

from direvo.config import ConfigError, load_config


def test_loads_valid_config(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    eval_script = tmp_path / "evaluate.sh"
    eval_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            max_trials: 10
            max_wall_time: "24h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.parallel_trials == 2
    assert config.workspace_root == tmp_path
    assert config.eval_script == eval_script.resolve()
    assert config.execution_command == "claude -p {direction}"
    assert config.max_wall_time_seconds == 86400


def test_rejects_invalid_metric_identifier(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            max_trials: 10
            max_wall_time: "24h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              bad-key: real
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_rejects_invalid_yaml(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text("not: [valid\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_rejects_execution_command_without_direction_placeholder(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            execution_command: "claude -p"
            max_trials: 10
            max_wall_time: "24h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="execution_command"):
        load_config(config_path)


def test_rejects_invalid_objective_sql_expression(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            max_trials: 10
            max_wall_time: "24h"
            objective:
              expr: "missing_metric + 1"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="objective\\.expr"):
        load_config(config_path)


def test_rejects_invalid_target_condition_sql(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            max_trials: 10
            max_wall_time: "24h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            target_condition: "missing_metric >="
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="target_condition"):
        load_config(config_path)


def test_rejects_planner_notify_template_without_trial_id(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            planner_notify_template: "Trial completed."
            max_trials: 10
            max_wall_time: "24h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="planner_notify_template"):
        load_config(config_path)


def test_rejects_invalid_planner_notify_template_format_string(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            eval_script: "./evaluate.sh"
            planner_notify_template: "Trial {trial_id"
            max_trials: 10
            max_wall_time: "24h"
            objective:
              expr: "test_pass_rate"
              direction: "maximize"
            metrics_schema:
              test_pass_rate: real
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="planner_notify_template"):
        load_config(config_path)
