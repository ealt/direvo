import textwrap
from pathlib import Path

import pytest

from direvo.config import ConfigError, load_config


def test_loads_valid_config(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    eval_py = tmp_path / "eval.py"
    eval_py.write_text("print('ok')\n", encoding="utf-8")
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            evaluate_command: "python3 eval.py"
            execute_command: "claude -p"
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
    assert config.experiment_root == tmp_path
    assert config.workspace_root == tmp_path
    assert "eval.py" in config.evaluate_command
    assert config.max_wall_time_seconds == 86400


def test_rejects_invalid_metric_identifier(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            evaluate_command: "python3 eval.py"
            execute_command: "claude -p"
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


def test_rejects_invalid_objective_sql_expression(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            evaluate_command: "python3 eval.py"
            execute_command: "claude -p"
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
            evaluate_command: "python3 eval.py"
            execute_command: "claude -p"
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


def test_rejects_plan_notify_template_without_trial_id(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            evaluate_command: "python3 eval.py"
            plan_notify_template: "Trial completed."
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

    with pytest.raises(ConfigError, match="plan_notify_template"):
        load_config(config_path)


def test_rejects_invalid_plan_notify_template_format_string(tmp_path: Path) -> None:
    direvo = tmp_path / ".direvo"
    direvo.mkdir()
    config_path = direvo / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            parallel_trials: 2
            evaluate_command: "python3 eval.py"
            plan_notify_template: "Trial {trial_id"
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

    with pytest.raises(ConfigError, match="plan_notify_template"):
        load_config(config_path)
