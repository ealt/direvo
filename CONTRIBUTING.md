# Contributing to EDEN

Thank you for your interest in contributing to eden!

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- Docker (for integration tests)
- Git
- Node.js 18+ and npm (for Web UI development only)

## Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/YOUR_USERNAME/eden.git
   cd eden
   ```

2. Install dependencies:
   ```bash
   uv sync --dev
   ```

3. Verify the setup:
   ```bash
   uv run -m pytest -q
   ```

## Development Workflow

### Running the Project

EDEN runs inside a Docker container. For local development, you primarily interact through the test suite and the `doctor` command:

```bash
uv run eden doctor --config /path/to/.eden/config.yaml
```

### Running Tests

```bash
# Full test suite
uv run -m pytest -q

# Single module
uv run -m pytest -q tests/test_orchestrator.py

# Single test
uv run -m pytest -q -k test_function_name
```

### Web UI Tests

```bash
# Build the frontend
cd packages/web-ui
npm install
npm run build

# Type check
npm run lint

# Run frontend tests
npm test
```

### Docker Integration Tests

These require Docker and test the full container lifecycle:

```bash
# Smoke test
./scripts/run_docker_integration.sh

# Root-only validation (requires privileged mode)
./scripts/run_privileged_validation.sh
```

### Code Style

Follow our [Style Guide](STYLE_GUIDE.md) for formatting rules.

Run the linter before committing:

```bash
uv run ruff check .
uv run pyright
```

## Pull Request Process

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes and commit with a short imperative subject:
   ```bash
   git commit -m "Add proposal priority field"
   ```

3. Push and open a Pull Request against `main`

### PR Requirements

- [ ] Tests pass (`uv run -m pytest -q`)
- [ ] Linting passes (`uv run ruff check .`)
- [ ] Type checking passes (`uv run pyright`)
- [ ] Tests added/updated for behavioral changes
- [ ] PR description includes problem/solution summary and any Docker/permission implications

## Questions?

Open an issue for questions or suggestions.
