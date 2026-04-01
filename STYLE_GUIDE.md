# Style Guide

This document defines the code formatting and style conventions for eden.

## Code Formatting

### General

- Indentation: 4 spaces
- Maximum line length: 120 characters
- Use trailing commas in multiline structures
- End files with a single newline

### Python

- Target version: Python 3.12+
- Formatter/linter: [Ruff](https://docs.astral.sh/ruff/)
- Type checker: [Pyright](https://github.com/microsoft/pyright) in standard mode

## Naming Conventions

### Files

| Type | Convention | Example |
|------|------------|---------|
| Modules | `snake_case.py` | `git_manager.py` |
| Test files | `test_<area>.py` | `test_orchestrator.py` |
| SQL templates | `snake_case.sql` | `create_results.sql` |

### Variables and Functions

- Variables: `snake_case`
- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Classes: `PascalCase`
- Boolean variables: prefix with `is`, `has`, `should`, `can`
- Related config/API fields that serve parallel roles should use consistent grammatical form (e.g., all imperative verbs: `evaluate_command`, `execute_command`, `plan_command` — not a mix of verb/noun/subject)

### Type Annotations

- Explicit types on all public API signatures and dataclass fields
- Use `from __future__ import annotations` when needed for forward references
- Prefer built-in generics (`list[str]`, `dict[str, int]`) over `typing` imports

## Documentation

### Docstrings

Use [Google-style docstrings](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings):

```python
def claim_proposal(db: Database, slot: int) -> Proposal | None:
    """Atomically claim the next ready proposal for a trial slot.

    Args:
        db: The proposals database connection.
        slot: The trial slot number to assign.

    Returns:
        The claimed proposal, or None if no proposals are ready.
    """
```

### Comments

- Use comments to explain "why", not "what"
- Keep comments close to the code they describe
- Update comments when code changes

## Patterns

### Preferred: Frozen Dataclasses

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class TrialResult:
    trial_id: int
    branch: str
    metrics: dict[str, float]
```

### Preferred: Explicit Error Handling at Boundaries

```python
# At system boundaries (subprocess calls, file I/O), handle errors explicitly
try:
    result = subprocess.run(cmd, capture_output=True, check=True)
except subprocess.CalledProcessError as e:
    raise ExecutionError(f"Command failed: {e.stderr}") from e
```

### Avoid: Mutable Default Arguments

```python
# Avoid this:
def process(items: list[str] = []) -> None: ...

# Prefer this:
def process(items: list[str] | None = None) -> None:
    items = items or []
```

## Linting Configuration

Configuration is in `pyproject.toml`:

- **Ruff**: line-length 120, target Python 3.12
  - Enabled rule sets: `A`, `B`, `D`, `E`, `F`, `I`, `PT`, `SIM`, `UP`
  - Ignored: `D100` (module docstrings), `D105` (magic method docstrings), `D107` (init docstrings), `SIM108` (ternary)
  - Docstring rules disabled for test files
- **Pyright**: standard mode, warns on unnecessary type-ignore comments

### Key Rules

- **D (pydocstyle)**: Google-style docstrings required on public functions (disabled in tests)
- **I (isort)**: Import sorting enforced by Ruff
- **UP (pyupgrade)**: Enforces modern Python syntax (3.12+ features)
- **PT (pytest)**: Pytest-specific best practices

## Running Linters

For commands to run linting and formatting, see [AGENTS.md](AGENTS.md#commands).
