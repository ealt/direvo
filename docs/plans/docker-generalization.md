# Plan: Generalize Docker/Run Infrastructure

## Context

The Docker build/run infrastructure is currently hardcoded in `example/data-fitting/` — a custom Dockerfile, run script, and auth-setup script. Every new experiment would need to copy and modify these files. This plan extracts that infrastructure into eden itself so any experiment can be containerized and launched via `eden docker run --config .eden/config.yaml` with no custom Docker scaffolding.

Additionally: `bootstrap()` lives in `orchestrator.py` despite being a session-initialization concern, and there are unresolved merge conflicts (`direvo` → `eden` rename) in two files.

## Config Schema

New optional `docker:` section in `.eden/config.yaml`:

```yaml
docker:
  tools: [claude, codex]              # list[str], optional, default: []
  dependencies: [ripgrep, bubblewrap] # apt packages, optional, default: []
  pip_dependencies: [numpy]           # pip packages, optional, default: []
  dockerfile: null                    # custom Dockerfile path, optional
  entrypoint: null                    # custom entrypoint script, optional
  setup_command: "python3 generate_data.py"  # build-time command, optional
  export_command: ~                   # absent=default, ~=disabled, str=custom
  image_name: null                    # custom image tag, optional
  git_config:                         # optional, defaults shown
    user_name: "eden"
    user_email: "eden@experiment"
```

New `DockerConfig` frozen dataclass in `models.py`. New `docker: DockerConfig | None` field on `SessionConfig` (None when section absent). Validated whenever present, regardless of which CLI command runs.

**export_command semantics**: key absent → use default export; key present with value `null`/`~` → disabled; key present with string value → custom command. Distinguish via `"export_command" in raw_docker` before checking value.

**Escape-hatch semantics**: When `dockerfile` is provided, it is used as-is — eden does NOT inject tool installation, dependency steps, setup_command, git init, or entrypoint configuration into a custom Dockerfile. The user owns the entire build. The other docker config keys (`tools`, `dependencies`, etc.) are still validated but are ignored during image build. When only `entrypoint` is provided (no custom `dockerfile`), the template-generated Dockerfile uses the custom entrypoint instead of the shipped one, but all other template features (tool installation, deps, setup, git init) still apply. Auth propagation is handled by the generic entrypoint; a custom entrypoint should call `eden-auth-setup` for auth propagation if needed, then delegate to the generic entrypoint via `exec eden-container-entrypoint "$@"` to get the standard runtime setup, export sync, and CLI run behavior.

## New Files

### `src/eden/docker/__init__.py`
Docstring-only package marker (same pattern as `src/eden/sql/__init__.py`).

### `src/eden/docker/entrypoint.sh`
Generic container entrypoint combining: auth propagation → background export sync → eden runtime+CLI → final export on exit. Replaces both the example's `auth-setup.sh` and the root `docker/entrypoint.sh`.

**Auth propagation**: Makes mounted host auth dirs (`/root/.claude`, `/root/.codex`, etc.) traversable by trial users. Sets `EDEN_AUTH_HOME=/root` and `EDEN_RUNTIME_DIR=/tmp/eden-runtime` (the env vars already consumed by `runtime.py`, `execution.py`, and `planner.py`). Auth dirs to propagate are determined by which dirs actually exist under `/root`, not by `EDEN_TOOLS` — this keeps auth handling independent of tool configuration and compatible with the existing runtime code paths.

**Core run logic**: Inlines the current `docker/entrypoint.sh` behavior — runs `python3 -m eden.runtime --config PATH` then `exec python3 -m eden.cli run`.

**Export**: If `/output` is mounted and export is not disabled (`EDEN_EXPORT_DISABLED` unset), starts a background sync loop and runs final export on exit. Export command comes from `EDEN_EXPORT_COMMAND` env var (set during `docker build` from config); when unset, uses the default `export.sh`.

### `src/eden/docker/auth-setup.sh`
Standalone auth propagation script, installed as `/usr/local/bin/eden-auth-setup`. Makes mounted host auth dirs traversable by trial users. Custom entrypoints call this explicitly if they need auth propagation. Extracted from the auth portion of `example/data-fitting/auth-setup.sh`.

### `src/eden/docker/export.sh`
Default export script extracted from `auth-setup.sh`'s sync/export logic. Takes mode arg (`sync`|`final`) and experiment root path. Copies `.eden/` artifacts to `/output`, creates `git bundle` on final.

### `src/eden/docker_runner.py`
Python module for Docker build/run orchestration:
- `render_dockerfile(config: SessionConfig) -> str` — assembles Dockerfile from conditional blocks based on `DockerConfig` fields. No template engine — just Python string building (same approach as `db.py:_render_results_schema()`).
- `build_image(config: SessionConfig, *, tag: str | None = None) -> str` — creates temp build context containing: experiment dir, shipped scripts (extracted via `importlib.resources`), and the eden source tree. Runs `docker build`, returns tag.
- `run_container(config: SessionConfig, *, tag: str, output_dir: Path | None = None) -> int` — detects auth mounts, runs `docker run --rm --privileged`, returns exit code.
- `detect_auth_mounts() -> list[tuple[str, str]]` — checks for host Claude/Codex auth dirs.

**Image build contract — how the image gets eden**:

The package is not yet published to PyPI, so `eden docker` is source-checkout-only for now — it must be run from within (or against) a source checkout of the eden repo. The generated Dockerfile always installs from the local source tree. `build_image()` locates the eden source tree by resolving `Path(__file__).parent` (which is `src/eden/`) and walking up to find the directory containing `pyproject.toml`. It copies the source tree (`pyproject.toml`, `src/`, `docs/`) into the build context as `eden-src/`. The Dockerfile then installs it:

```dockerfile
COPY eden-src /app
RUN pip install --no-cache-dir /app
```

When `direvo` is published to PyPI in the future, `render_dockerfile()` can be updated to use `pip install direvo` instead. This is a single-line change gated by the package being available, not by detecting the user's checkout location.

If `build_image()` cannot locate the eden source tree (no `pyproject.toml` with the eden package found by walking up from `__file__`), it raises a clear `RuntimeError` with a message like "eden docker requires a source checkout; pip-installable package not yet available" rather than letting Docker fail with a confusing build error.

The shipped scripts (`entrypoint.sh`, `export.sh`, `eden-auth-setup`) are extracted from `importlib.resources` into the build context and `COPY`'d into `/usr/local/bin/` in the Dockerfile.

**Workspace git bootstrap**:

The generated Dockerfile conditionally initializes the workspace as a git repo at build time. The template emits a script that checks whether `.git` already exists:
```dockerfile
RUN cd <planner_root>/<workspace> \
    && if [ ! -e .git ]; then \
         git init -q \
         && git config user.email "<git_email>" \
         && git config user.name "<git_name>" \
         && git add . \
         && git commit -q -m "initial baseline"; \
       fi
```
This handles both cases: experiments with a plain workspace directory (like the demo) get an initialized repo, while experiments that ship an existing git repo are left untouched. The paths come from `planner_root` and `workspace` in the existing config; `git_email`/`git_name` come from `docker.git_config` (defaults: `"eden"` / `"eden@experiment"`). When a custom `dockerfile` is provided, the user is responsible for workspace initialization.

### `src/eden/bootstrap.py`
Extract from `orchestrator.py`: `BootstrapResult`, `_ensure_symlink()`, `bootstrap()`. Re-export from `orchestrator.py` for backward compatibility.

### `tests/test_docker_config.py`
Validation tests for docker config section: valid parsing, tool validation, path validation, export_command null vs absent, backward compat (no docker section → `docker is None`).

### `tests/test_docker_runner.py`
Unit tests for Dockerfile generation and command assembly. No Docker required — tests string output and subprocess args. Must include:
- Workspace bootstrap: generated Dockerfile skips `git init` when `.git` exists (test both file and directory forms)
- Custom entrypoint: generated Dockerfile uses the custom script path instead of `eden-container-entrypoint`
- Custom dockerfile: `build_image()` uses provided Dockerfile, does not generate one
- Source tree not found: `build_image()` raises clear error when eden source tree is missing

### `tests/test_bootstrap.py`
Tests for extracted bootstrap module + verify re-export from `orchestrator.py`.

## Modified Files

### `src/eden/models.py`
- Add `DockerConfig` dataclass
- Add `docker: DockerConfig | None` to `SessionConfig` (last field, no default — always passed explicitly by `load_config()`)
- Remove the two TODO comments (lines 43-44)

### `src/eden/config.py`
- Add `_validate_docker_config(experiment_root, raw) -> DockerConfig | None`
- Add helpers: `_validate_string_list()`, `_validate_docker_tools()`, `_validate_docker_git_config()`
- Wire into `load_config()`: pass `docker=_validate_docker_config(...)` to `SessionConfig`

### `src/eden/cli.py`
- Add `docker` subcommand with `build` and `run` sub-subcommands
- `docker build --config PATH [--tag TAG]`
- `docker run --config PATH [--tag TAG] [--output DIR]`
- Import from `docker_runner.py`

### `src/eden/orchestrator.py`
- Resolve merge conflict (lines 67-72): keep `progress` parameter, keep `eden` naming
- Replace `BootstrapResult`, `_ensure_symlink`, `bootstrap` with import from `bootstrap.py`
- Re-export for backward compat: `from .bootstrap import BootstrapResult, bootstrap`

### `pyproject.toml`
- Update package-data: `eden = ["sql/*.sql", "docker/*"]`
- Package name stays `eden` for now. When publishing to PyPI as `direvo`, update the name field and switch `render_dockerfile()` to `pip install direvo`.

### `docker/entrypoint.sh` (root-level)
Replace with a thin wrapper that calls the canonical shipped version:
```sh
#!/bin/sh
# Backward-compat wrapper. The canonical entrypoint ships inside the package
# at src/eden/docker/entrypoint.sh and is installed during image build.
exec eden-container-entrypoint "$@"
```
This keeps the root `Dockerfile` working (it copies `docker/entrypoint.sh`), and existing tests that reference this path continue to pass. The real logic lives in `src/eden/docker/entrypoint.sh`.

### `Dockerfile` (root-level)
Update to also copy and install the canonical entrypoint from `src/eden/docker/entrypoint.sh` as `eden-container-entrypoint`, so the wrapper can delegate to it.

### `tests/test_entrypoint.py`
Update to test both the wrapper delegation and the canonical entrypoint behavior.

### `tests/test_docker_integration.py`
Update to test the new `eden docker build`/`run` path in addition to the existing root Dockerfile path.

### `example/data-fitting/.eden/config.yaml`
- Add `docker:` section with tools, dependencies, pip_dependencies, setup_command, git_config

### `example/data-fitting/README.md`
- Update to use `eden docker run` instead of `./run.sh`

### `AGENTS.md`
- Add `eden docker build/run` to commands table
- Update directory structure to show `src/eden/docker/`

## Files to Delete

- `example/data-fitting/Dockerfile` — replaced by generic template
- `example/data-fitting/run.sh` — replaced by `eden docker run`
- `example/data-fitting/auth-setup.sh` — auth logic extracted to `src/eden/docker/auth-setup.sh`, export/sync logic extracted to `src/eden/docker/export.sh`, orchestration logic absorbed into `src/eden/docker/entrypoint.sh`

## Implementation Order

### Phase 1: Foundation (restructuring, no new features)
1. Resolve merge conflicts in `orchestrator.py` and `example/data-fitting/Dockerfile`
2. Extract `bootstrap()` to `src/eden/bootstrap.py`, add re-export, run tests

### Phase 2: Config Schema
3. Add `DockerConfig` to `models.py`, add field to `SessionConfig`
4. Add validation in `config.py`
5. Write `tests/test_docker_config.py`, verify existing tests still pass

### Phase 3: Docker Infrastructure
6. Create `src/eden/docker/` package with entrypoint.sh, auth-setup.sh, and export.sh
7. Create `src/eden/docker_runner.py`
8. Write `tests/test_docker_runner.py`
9. Update `pyproject.toml` package-data

### Phase 4: CLI Integration
10. Add `eden docker` commands to `cli.py`
11. Update CLI tests

### Phase 5: Example Migration
12. Update `example/data-fitting/.eden/config.yaml` with docker section
13. Delete `example/data-fitting/{Dockerfile,run.sh,auth-setup.sh}`
14. Update `example/data-fitting/README.md`
15. Update demo tests

### Phase 6: Documentation
16. Update `AGENTS.md`

## Verification

### Unit tests (no Docker required)
1. `uv run -m pytest -q` — all existing tests pass (backward compat)
2. `uv run -m pytest -q tests/test_docker_config.py` — docker config validation
3. `uv run -m pytest -q tests/test_docker_runner.py` — Dockerfile generation and command assembly
4. `uv run -m pytest -q tests/test_bootstrap.py` — bootstrap extraction
5. `uv run -m pytest -q tests/test_entrypoint.py` — entrypoint wrapper + canonical entrypoint
6. `uv run ruff check .` — lint clean
7. `uv run pyright` — type check clean

### Integration tests (require Docker)
8. `./scripts/run_docker_integration.sh` — existing Docker smoke test still passes with root Dockerfile
9. `uv run -m pytest -q tests/test_docker_integration.py -k docker_build_from_config` — new: builds image from config with generated Dockerfile
10. `uv run -m pytest -q tests/test_docker_integration.py -k docker_run_from_config` — new: full build+run cycle via `eden docker run`

### Manual verification
11. `eden docker run --config example/data-fitting/.eden/config.yaml --output ./output` — builds and runs the data-fitting demo end-to-end (requires Docker + Claude/Codex auth)
