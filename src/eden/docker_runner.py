"""Docker image build and container run orchestration."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from .models import SessionConfig


def render_dockerfile(config: SessionConfig) -> str:
    """Generate a Dockerfile from the session config's docker section."""
    docker = config.docker
    if docker is None:
        raise RuntimeError("Cannot render Dockerfile: no docker section in config.")

    lines: list[str] = []
    lines.append("FROM python:3.12-slim")
    lines.append("")
    lines.append("ENV PYTHONDONTWRITEBYTECODE=1")
    lines.append("ENV PYTHONUNBUFFERED=1")
    lines.append("")

    # System dependencies.
    apt_packages = ["git", "passwd"]
    if docker.tools:
        apt_packages.extend(["curl", "ca-certificates", "gnupg"])
    apt_packages.extend(docker.dependencies)
    apt_install = " ".join(sorted(set(apt_packages)))
    lines.append("RUN apt-get update \\")
    lines.append(f"    && apt-get install -y --no-install-recommends {apt_install} \\")
    lines.append("    && rm -rf /var/lib/apt/lists/*")
    lines.append("")

    # Conditional tool installation.
    if "codex" in docker.tools:
        lines.append("# Node.js 22 LTS (for Codex CLI)")
        lines.append("RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\")
        lines.append("    && apt-get install -y --no-install-recommends nodejs \\")
        lines.append("    && rm -rf /var/lib/apt/lists/*")
        lines.append("")

    if "claude" in docker.tools:
        lines.append("# Claude CLI")
        lines.append("RUN curl -fsSL https://claude.ai/install.sh | bash \\")
        lines.append('    && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude')
        lines.append("")

    if "codex" in docker.tools:
        lines.append("# Codex CLI")
        lines.append("RUN npm install -g @openai/codex")
        lines.append("")

    # Install eden from local source tree.
    lines.append("# Install eden")
    lines.append("COPY eden-src /app")
    lines.append("RUN pip install --no-cache-dir /app")
    lines.append("")

    # Extra pip dependencies.
    if docker.pip_dependencies:
        pip_pkgs = " ".join(docker.pip_dependencies)
        lines.append(f"RUN pip install --no-cache-dir {pip_pkgs}")
        lines.append("")

    # Copy shipped scripts.
    lines.append("# Container scripts")
    lines.append("COPY scripts/entrypoint.sh /usr/local/bin/eden-container-entrypoint")
    lines.append("RUN chmod 0755 /usr/local/bin/eden-container-entrypoint")
    lines.append("COPY scripts/auth-setup.sh /usr/local/bin/eden-auth-setup")
    lines.append("RUN chmod 0755 /usr/local/bin/eden-auth-setup")
    lines.append("COPY scripts/export.sh /usr/local/bin/eden-export")
    lines.append("RUN chmod 0755 /usr/local/bin/eden-export")
    lines.append("")

    # Copy experiment.
    lines.append("# Copy experiment")
    lines.append("COPY experiment /experiment")
    lines.append("WORKDIR /experiment")
    lines.append("")

    # Setup command.
    if docker.setup_command:
        lines.append(f"RUN {docker.setup_command}")
        lines.append("")

    # Conditional workspace git init.
    workspace_rel = config.planner_root.relative_to(config.experiment_root) / config.workspace_root.relative_to(
        config.planner_root
    )
    lines.append("# Initialize workspace git repo if not already one")
    lines.append(f"RUN cd {workspace_rel} \\")
    lines.append("    && if [ ! -e .git ]; then \\")
    lines.append("         git init -q \\")
    lines.append(f'         && git config user.email "{docker.git_user_email}" \\')
    lines.append(f'         && git config user.name "{docker.git_user_name}" \\')
    lines.append("         && git add . \\")
    lines.append('         && git commit -q -m "initial baseline"; \\')
    lines.append("       fi")
    lines.append("")

    # Export environment.
    if docker.export_disabled:
        lines.append('ENV EDEN_EXPORT_DISABLED="1"')
    elif docker.export_command:
        lines.append(f'ENV EDEN_EXPORT_COMMAND="{docker.export_command}"')
    lines.append("")

    # Entrypoint.
    if docker.entrypoint:
        entrypoint_rel = docker.entrypoint.relative_to(config.experiment_root)
        lines.append(f"COPY experiment/{entrypoint_rel} /usr/local/bin/custom-entrypoint")
        lines.append("RUN chmod 0755 /usr/local/bin/custom-entrypoint")
        lines.append('ENTRYPOINT ["custom-entrypoint"]')
    else:
        lines.append('ENTRYPOINT ["eden-container-entrypoint"]')

    lines.append('CMD ["run", "--config", "/experiment/.eden/config.yaml"]')
    lines.append("")

    return "\n".join(lines)


def _find_eden_source_tree() -> Path:
    """Locate the eden source tree by walking up from this file."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            toml_text = (current / "pyproject.toml").read_text(encoding="utf-8")
            if 'name = "eden"' in toml_text:
                return current
        current = current.parent
    raise RuntimeError(
        "eden docker requires a source checkout. "
        "Could not locate pyproject.toml with the eden package. "
        "A pip-installable package is not yet available."
    )


def _extract_shipped_scripts(dest: Path) -> None:
    """Extract packaged Docker scripts to a destination directory."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("entrypoint.sh", "auth-setup.sh", "export.sh"):
        content = resources.files("eden.docker").joinpath(name).read_text(encoding="utf-8")
        (dest / name).write_text(content, encoding="utf-8")


def build_image(config: SessionConfig, *, tag: str | None = None) -> str:
    """Build a Docker image for the experiment.

    Args:
        config: Validated session config with a docker section.
        tag: Optional image tag. Auto-generated if not provided.

    Returns:
        The image tag.
    """
    docker = config.docker
    if docker is None:
        raise RuntimeError("Cannot build image: no docker section in config.")

    if tag is None:
        tag = docker.image_name or f"eden-{config.experiment_root.name}"

    with tempfile.TemporaryDirectory(prefix="eden-docker-") as tmpdir:
        context = Path(tmpdir)

        # Custom Dockerfile: use it directly.
        if docker.dockerfile:
            dockerfile_content = docker.dockerfile.read_text(encoding="utf-8")
        else:
            dockerfile_content = render_dockerfile(config)

        (context / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

        # Copy eden source tree.
        source_tree = _find_eden_source_tree()
        eden_src = context / "eden-src"
        for subdir in ("src", "docs"):
            src = source_tree / subdir
            if src.exists():
                shutil.copytree(src, eden_src / subdir)
        for filename in ("pyproject.toml",):
            src = source_tree / filename
            if src.exists():
                shutil.copy2(src, eden_src / filename)

        # Copy experiment directory.
        experiment_dest = context / "experiment"
        shutil.copytree(config.experiment_root, experiment_dest, dirs_exist_ok=True)

        # Extract shipped scripts.
        _extract_shipped_scripts(context / "scripts")

        subprocess.run(
            ["docker", "build", "-t", tag, "-f", "Dockerfile", "."],
            cwd=context,
            check=True,
        )

    return tag


def detect_auth_mounts() -> list[tuple[str, str]]:
    """Detect host CLI auth directories that should be mounted into the container."""
    home = Path.home()
    mounts: list[tuple[str, str]] = []

    dir_mappings = [
        (".claude", "/root/.claude"),
        (".config/claude", "/root/.config/claude"),
        (".local/state/claude", "/root/.local/state/claude"),
        (".local/share/claude", "/root/.local/share/claude"),
        (".cache/claude", "/root/.cache/claude"),
        (".codex", "/root/.codex"),
    ]
    for rel, container_path in dir_mappings:
        host_path = home / rel
        if host_path.is_dir():
            mounts.append((str(host_path), container_path))

    file_mappings = [
        (".claude.json", "/root/.claude.json"),
    ]
    for rel, container_path in file_mappings:
        host_path = home / rel
        if host_path.is_file():
            mounts.append((str(host_path), container_path))

    return mounts


def run_container(
    config: SessionConfig,
    *,
    tag: str,
    output_dir: Path | None = None,
) -> int:
    """Run a Docker container for the experiment.

    Args:
        config: Validated session config.
        tag: Docker image tag to run.
        output_dir: Optional host directory for result export.

    Returns:
        Container exit code.
    """
    cmd: list[str] = ["docker", "run", "--rm", "--privileged"]

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["-v", f"{output_dir.resolve()}:/output"])

    for host_path, container_path in detect_auth_mounts():
        cmd.extend(["-v", f"{host_path}:{container_path}"])

    cmd.append(tag)

    result = subprocess.run(cmd)
    return result.returncode
