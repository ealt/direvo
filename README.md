# DirEvo

An orchestration system that runs concurrent research trials inside Docker containers.

## Quick Start

1. Install the package and dev dependencies:
   ```bash
   uv sync --extra dev
   ```
   This installs the `direvo` CLI into the virtualenv.

2. Run the test suite:
   ```bash
   uv run -m pytest -q
   ```

3. Validate a workspace config:
   ```bash
   uv run direvo doctor --config /path/to/.direvo/config.yaml
   ```

4. Try the [data-fitting demo](example/data-fitting/) for a realistic example
   using Claude and Codex as AI agents.

## How It Works

A **planner** proposes experiments via a shared SQLite database, and an **orchestrator** dispatches them as parallel git worktrees inside a Docker container. Each trial runs under an isolated Linux user for permission enforcement.

See [docs/plans/v0.md](docs/plans/v0.md) for the full design document.

## Documentation

- [Contributing](CONTRIBUTING.md) — Development setup and workflow
- [Style Guide](STYLE_GUIDE.md) — Code formatting rules

## License

[MIT](LICENSE)
