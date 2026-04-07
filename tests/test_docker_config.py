"""Tests for Docker config validation."""

import textwrap
from pathlib import Path

import pytest

from eden.config import ConfigError, load_config


def _base_config() -> str:
    return textwrap.dedent(
        """
        planner_root: "./planner"
        workspace: "./workspace"
        parallel_trials: 2
        evaluate_command: "python3 eval.py"
        implement_command: "claude -p"
        max_trials: 10
        max_wall_time: "24h"
        objective:
          expr: "test_pass_rate"
          direction: "maximize"
        metrics_schema:
          test_pass_rate: real
        """
    )


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    eden_dir = tmp_path / ".eden"
    eden_dir.mkdir(exist_ok=True)
    config_path = eden_dir / "config.yaml"
    eval_py = tmp_path / "eval.py"
    eval_py.write_text("print('ok')\n", encoding="utf-8")
    config_path.write_text(_base_config() + extra, encoding="utf-8")
    return config_path


def test_loads_config_without_docker_section(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    assert config.docker is None


def test_loads_valid_docker_section(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        textwrap.dedent(
            """
            docker:
              tools: [claude, codex]
              dependencies: [ripgrep]
              pip_dependencies: [numpy]
              setup_command: "echo hello"
              image_name: "my-image"
              git_config:
                user_name: "test"
                user_email: "test@example.com"
            """
        ),
    )
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.tools == ("claude", "codex")
    assert config.docker.dependencies == ("ripgrep",)
    assert config.docker.pip_dependencies == ("numpy",)
    assert config.docker.setup_command == "echo hello"
    assert config.docker.image_name == "my-image"
    assert config.docker.git_user_name == "test"
    assert config.docker.git_user_email == "test@example.com"
    assert config.docker.dockerfile is None
    assert config.docker.entrypoint is None
    assert config.docker.export_command is None
    assert config.docker.export_disabled is False


def test_docker_empty_section_uses_defaults(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker: {}\n")
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.tools == ()
    assert config.docker.dependencies == ()
    assert config.docker.pip_dependencies == ()
    assert config.docker.git_user_name == "eden"
    assert config.docker.git_user_email == "eden@experiment"


def test_docker_tools_rejects_non_list(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  tools: claude\n")
    with pytest.raises(ConfigError, match="docker.tools must be a list"):
        load_config(config_path)


def test_docker_tools_rejects_empty_string(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, '\ndocker:\n  tools: [""]\n')
    with pytest.raises(ConfigError, match="docker.tools entries"):
        load_config(config_path)


def test_docker_unknown_tool_rejects(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  tools: [unknown_tool]\n")
    with pytest.raises(ConfigError, match="Unknown docker tool"):
        load_config(config_path)


def test_docker_dependencies_rejects_non_list(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  dependencies: ripgrep\n")
    with pytest.raises(ConfigError, match="docker.dependencies must be a list"):
        load_config(config_path)


def test_docker_dockerfile_must_exist(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, "\ndocker:\n  dockerfile: nonexistent.Dockerfile\n"
    )
    with pytest.raises(ConfigError, match="docker.dockerfile does not exist"):
        load_config(config_path)


def test_docker_dockerfile_valid_path(tmp_path: Path) -> None:
    dockerfile = tmp_path / "custom.Dockerfile"
    dockerfile.write_text("FROM python:3.12\n", encoding="utf-8")
    config_path = _write_config(
        tmp_path, "\ndocker:\n  dockerfile: custom.Dockerfile\n"
    )
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.dockerfile == dockerfile.resolve()


def test_docker_entrypoint_must_exist(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, "\ndocker:\n  entrypoint: nonexistent.sh\n"
    )
    with pytest.raises(ConfigError, match="docker.entrypoint does not exist"):
        load_config(config_path)


def test_docker_git_config_defaults(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker: {}\n")
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.git_user_name == "eden"
    assert config.docker.git_user_email == "eden@experiment"


def test_docker_git_config_custom(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        textwrap.dedent(
            """
            docker:
              git_config:
                user_name: "myname"
                user_email: "me@example.com"
            """
        ),
    )
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.git_user_name == "myname"
    assert config.docker.git_user_email == "me@example.com"


def test_docker_git_config_rejects_non_mapping(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  git_config: bad\n")
    with pytest.raises(ConfigError, match="docker.git_config must be a mapping"):
        load_config(config_path)


def test_docker_setup_command_optional(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker: {}\n")
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.setup_command is None


def test_docker_setup_command_rejects_empty(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, '\ndocker:\n  setup_command: ""\n')
    with pytest.raises(ConfigError, match="docker.setup_command"):
        load_config(config_path)


def test_docker_export_command_absent_uses_default(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker: {}\n")
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.export_command is None
    assert config.docker.export_disabled is False


def test_docker_export_command_null_disables(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  export_command: ~\n")
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.export_command is None
    assert config.docker.export_disabled is True


def test_docker_export_command_custom_string(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, '\ndocker:\n  export_command: "my-export.sh"\n'
    )
    config = load_config(config_path)
    assert config.docker is not None
    assert config.docker.export_command == "my-export.sh"
    assert config.docker.export_disabled is False


def test_docker_export_command_rejects_empty_string(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, '\ndocker:\n  export_command: ""\n')
    with pytest.raises(ConfigError, match="docker.export_command"):
        load_config(config_path)


def test_docker_image_name_rejects_empty(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, '\ndocker:\n  image_name: ""\n')
    with pytest.raises(ConfigError, match="docker.image_name"):
        load_config(config_path)


def test_docker_rejects_non_mapping(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker: true\n")
    with pytest.raises(ConfigError, match="docker must be a mapping"):
        load_config(config_path)


def test_docker_entrypoint_must_be_under_experiment_root(tmp_path: Path) -> None:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as f:
        f.write(b"#!/bin/sh\n")
        external_path = f.name

    config_path = _write_config(
        tmp_path, f'\ndocker:\n  entrypoint: "{external_path}"\n'
    )
    with pytest.raises(ConfigError, match="must be under experiment_root"):
        load_config(config_path)

    import os

    os.unlink(external_path)
