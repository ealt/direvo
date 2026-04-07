"""Tests for Docker runner: Dockerfile generation and command assembly."""

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from eden.config import load_config
from eden.docker_runner import (
    _find_eden_source_tree,
    detect_auth_mounts,
    render_dockerfile,
)


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


def _write_config(tmp_path: Path, docker_section: str) -> Path:
    eden_dir = tmp_path / ".eden"
    eden_dir.mkdir(exist_ok=True)
    eval_py = tmp_path / "eval.py"
    eval_py.write_text("print('ok')\n", encoding="utf-8")
    config_path = eden_dir / "config.yaml"
    config_path.write_text(_base_config() + docker_section, encoding="utf-8")
    return config_path


def test_render_dockerfile_minimal(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker: {}\n")
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "FROM python:3.12-slim" in dockerfile
    assert "COPY eden-src /app" in dockerfile
    assert "pip install --no-cache-dir /app" in dockerfile
    assert "eden-container-entrypoint" in dockerfile
    assert "nodejs" not in dockerfile
    assert "claude.ai/install.sh" not in dockerfile


def test_render_dockerfile_with_claude(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  tools: [claude]\n")
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "claude.ai/install.sh" in dockerfile
    assert "curl" in dockerfile
    # No codex/nodejs since only claude is specified.
    assert "nodejs" not in dockerfile
    assert "npm install" not in dockerfile


def test_render_dockerfile_with_codex(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  tools: [codex]\n")
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "nodejs" in dockerfile
    assert "npm install -g @openai/codex" in dockerfile


def test_render_dockerfile_with_both_tools(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  tools: [claude, codex]\n")
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "claude.ai/install.sh" in dockerfile
    assert "npm install -g @openai/codex" in dockerfile
    assert "nodejs" in dockerfile


def test_render_dockerfile_with_dependencies(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, "\ndocker:\n  dependencies: [ripgrep, bubblewrap]\n"
    )
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "bubblewrap" in dockerfile
    assert "ripgrep" in dockerfile


def test_render_dockerfile_with_pip_dependencies(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, "\ndocker:\n  pip_dependencies: [numpy, scipy]\n"
    )
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "pip install --no-cache-dir numpy scipy" in dockerfile


def test_render_dockerfile_with_setup_command(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, '\ndocker:\n  setup_command: "python3 generate_data.py"\n'
    )
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "RUN python3 generate_data.py" in dockerfile


def test_render_dockerfile_with_custom_git_config(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        textwrap.dedent(
            """
            docker:
              git_config:
                user_name: "myname"
                user_email: "me@test.com"
            """
        ),
    )
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert '"myname"' in dockerfile
    assert '"me@test.com"' in dockerfile


def test_render_dockerfile_export_disabled(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker:\n  export_command: ~\n")
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert 'EDEN_EXPORT_DISABLED="1"' in dockerfile


def test_render_dockerfile_custom_export_command(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, '\ndocker:\n  export_command: "my-export.sh"\n'
    )
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert 'EDEN_EXPORT_COMMAND="my-export.sh"' in dockerfile


def test_render_dockerfile_workspace_bootstrap_skips_existing_git(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "\ndocker: {}\n")
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    # The generated Dockerfile checks for .git before initializing.
    assert "[ ! -e .git ]" in dockerfile
    assert "git init" in dockerfile


def test_render_dockerfile_custom_entrypoint(tmp_path: Path) -> None:
    entrypoint = tmp_path / "my-entrypoint.sh"
    entrypoint.write_text("#!/bin/sh\nexec eden-container-entrypoint \"$@\"\n", encoding="utf-8")
    config_path = _write_config(
        tmp_path, "\ndocker:\n  entrypoint: my-entrypoint.sh\n"
    )
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "custom-entrypoint" in dockerfile
    assert 'ENTRYPOINT ["custom-entrypoint"]' in dockerfile


def test_render_dockerfile_nested_custom_entrypoint(tmp_path: Path) -> None:
    nested_dir = tmp_path / "scripts"
    nested_dir.mkdir()
    entrypoint = nested_dir / "ep.sh"
    entrypoint.write_text("#!/bin/sh\nexec eden-container-entrypoint \"$@\"\n", encoding="utf-8")
    config_path = _write_config(
        tmp_path, "\ndocker:\n  entrypoint: scripts/ep.sh\n"
    )
    config = load_config(config_path)
    dockerfile = render_dockerfile(config)

    assert "COPY experiment/scripts/ep.sh /usr/local/bin/custom-entrypoint" in dockerfile


def test_render_dockerfile_no_docker_section_raises(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "")
    config = load_config(config_path)
    with pytest.raises(RuntimeError, match="no docker section"):
        render_dockerfile(config)


def test_find_eden_source_tree_succeeds() -> None:
    source_tree = _find_eden_source_tree()
    assert (source_tree / "pyproject.toml").exists()
    assert (source_tree / "src" / "eden").is_dir()


def test_find_eden_source_tree_raises_when_not_found() -> None:
    with patch("eden.docker_runner.Path") as mock_path:
        sentinel = mock_path.return_value.resolve.return_value.parent
        sentinel.parent = sentinel  # make it look like filesystem root
        sentinel.__truediv__ = lambda self, x: sentinel
        sentinel.exists.return_value = False

        with pytest.raises(RuntimeError, match="source checkout"):
            _find_eden_source_tree()


def test_detect_auth_mounts_finds_existing(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()

    with patch("eden.docker_runner.Path") as mock_path:
        mock_path.home.return_value = tmp_path
        mounts = detect_auth_mounts()

    host_paths = [m[0] for m in mounts]
    assert str(tmp_path / ".claude") in host_paths
    assert str(tmp_path / ".codex") in host_paths


def test_detect_auth_mounts_skips_missing(tmp_path: Path) -> None:
    # No auth dirs exist.
    with patch("eden.docker_runner.Path") as mock_path:
        mock_path.home.return_value = tmp_path
        mounts = detect_auth_mounts()

    assert mounts == []


def test_build_image_uses_custom_dockerfile(tmp_path: Path) -> None:
    """When docker.dockerfile is set, build_image uses that file instead of generating one."""
    from eden.docker_runner import build_image

    custom_df = tmp_path / "custom.Dockerfile"
    custom_df.write_text("FROM python:3.12\nRUN echo custom\n", encoding="utf-8")
    config_path = _write_config(
        tmp_path, '\ndocker:\n  dockerfile: "custom.Dockerfile"\n'
    )
    config = load_config(config_path)

    # Mock subprocess.run to capture the docker build command without actually running Docker.
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append(cmd)
        return type("Result", (), {"returncode": 0})()

    with patch("eden.docker_runner.subprocess.run", side_effect=fake_run):
        tag = build_image(config, tag="test-custom")

    assert tag == "test-custom"
    assert len(calls) == 1
    assert "docker" in calls[0]
    assert "build" in calls[0]
