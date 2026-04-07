# Plan: Generalize planner kit with artifact reading and agent session support

## Context

The first round of planner kit extraction (`planner-kit-extraction.md`) moved
shared utilities and the main loop into `src/eden/planner_kit.py`, making both
planners thin wrappers. However, the data-fitting planner still contains three
pieces of generally useful functionality that should ship with the library:

1. **`read_trial_artifact`** — reads files from `.eden/artifacts/trial-{id}/`.
   Every planner that wants to inspect trial outputs (plans, notes, eval
   reports) needs this. The bootstrap already symlinks `.eden/artifacts` into
   planner_root (`bootstrap.py:55`), so the convention path works from any
   planner.

2. **Claude CLI session management** — the `_session_started` global,
   `generate_claude_proposal()`, and the first-call-vs-continuation flag logic.
   This pattern (persistent agent session across reactive calls) is useful for
   any planner that delegates proposal generation to an AI agent CLI.

3. **Embedded system prompt** — `_SYSTEM_PROMPT` is a string constant that
   could instead be a file in the planner directory, passed explicitly via
   `--append-system-prompt-file`. This makes the prompt editable without
   touching Python and preserves Claude's built-in capabilities.

## Scope and non-goals

- **In scope**: artifact reading, agent session abstraction, system prompt
  extraction, updating the data-fitting planner to use these, tests.
- **Not in scope**: changes to the seed-sum fixture planner (it doesn't use
  artifacts or agent sessions), changes to the orchestrator or evaluator,
  notification template configurability (documented limitation from round 1).

## Approach

### 1. `read_trial_artifact` — module-level function + `PlannerContext` method

Add `artifacts_dir` as a field on `PlannerContext` and a parameter on
`run_planner()` (default `".eden/artifacts"`).

Module-level function:

```python
def read_trial_artifact(artifacts_dir: str, trial_id: int, filename: str) -> str | None:
    """Read a text artifact file from a completed trial.

    Intended for text artifacts (plan.md, notes.md, eval_report.json).
    Returns the stripped file contents, or None if the file does not exist.
    """
    path = Path(artifacts_dir) / f"trial-{trial_id}" / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None
```

`PlannerContext` method delegates to the module-level function:

```python
def read_trial_artifact(self, trial_id: int, filename: str) -> str | None:
    """Read an artifact file from a completed trial."""
    return read_trial_artifact(self.artifacts_dir, trial_id, filename)
```

### 2. `AgentSession` base class + `ClaudeSession` subclass

A generic base class for persistent CLI agent sessions, with the subprocess
execution and error handling in the base, and command construction in
subclasses.

```python
@dataclass
class AgentSession(ABC):
    """Base class for persistent CLI agent sessions.

    Subclasses define how to build the CLI command for a given prompt.
    The base class handles subprocess execution, timeout, error handling,
    and session-started state tracking.
    """
    timeout: int = 120
    _started: bool = field(default=False, init=False, repr=False)

    @abstractmethod
    def _build_command(self, prompt: str) -> list[str]:
        """Build the CLI command list for the given prompt."""
        ...

    def generate(self, prompt: str) -> str | None:
        """Send a prompt to the agent and return the response.

        Returns None on timeout, missing CLI binary, non-zero exit, or
        empty output. Logs failures at DEBUG level for diagnostics.
        """
        cmd = self._build_command(prompt)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
            if result.returncode == 0 and result.stdout.strip():
                self._started = True
                return result.stdout.strip()
            logging.debug("Agent CLI returned rc=%d: %s", result.returncode, result.stderr[:200])
        except subprocess.TimeoutExpired:
            logging.debug("Agent CLI timed out after %ds", self.timeout)
        except FileNotFoundError:
            logging.debug("Agent CLI binary not found: %s", cmd[0])
        return None
```

Claude-specific subclass:

```python
@dataclass
class ClaudeSession(AgentSession):
    """Claude CLI session with automatic session continuity.

    On the first call, starts a new session with optional system prompt
    configuration. Subsequent calls append ``-c`` to continue the session.

    System prompt flags (applied only on the first successful call):

    - *append_system_prompt_file*: ``--append-system-prompt-file`` (additive,
      preserves Claude's built-in capabilities — recommended)
    - *append_system_prompt*: ``--append-system-prompt`` (additive, inline string)
    - *system_prompt*: ``--system-prompt`` (full replacement)
    - *system_prompt_file*: ``--system-prompt-file`` (full replacement from file)

    At most one should be set. If multiple are set, ``__post_init__``
    raises ``ValueError``.
    """
    append_system_prompt_file: Path | None = None
    append_system_prompt: str | None = None
    system_prompt: str | None = None
    system_prompt_file: Path | None = None

    def __post_init__(self) -> None:
        opts = [self.append_system_prompt_file, self.append_system_prompt,
                self.system_prompt, self.system_prompt_file]
        if sum(o is not None for o in opts) > 1:
            raise ValueError("ClaudeSession accepts at most one system prompt option")

    def _build_command(self, prompt: str) -> list[str]:
        cmd = ["claude", "-p", prompt]
        if self._started:
            cmd.append("-c")
        else:
            if self.append_system_prompt_file is not None:
                cmd.extend(["--append-system-prompt-file", str(self.append_system_prompt_file)])
            elif self.append_system_prompt is not None:
                cmd.extend(["--append-system-prompt", self.append_system_prompt])
            elif self.system_prompt_file is not None:
                cmd.extend(["--system-prompt-file", str(self.system_prompt_file)])
            elif self.system_prompt is not None:
                cmd.extend(["--system-prompt", self.system_prompt])
        return cmd
```

### Key design decisions

1. **ABC, not duck typing** — `AgentSession` is abstract because
   `_build_command` has no sensible default. This makes the contract explicit
   and gives Pyright something to check. `generate()` is concrete in the base.

2. **`_started` managed by the base** — only set to `True` after a successful
   response. This means a failed first call still gets the system prompt on
   retry, which is the right behavior. Subclasses for agents without session
   continuity (e.g., one-shot `codex exec`) simply ignore `_started` in their
   `_build_command`.

3. **Intentionally multi-agent scope** — the base class is designed to support
   different CLI agents (Claude, Codex, etc.), each with their own flag
   conventions. `_started` is the minimal common state; subclasses decide
   whether and how to use it.

4. **Append-by-default system prompt convention** — the Claude CLI docs
   distinguish between replace flags (`--system-prompt`, `--system-prompt-file`)
   which discard the built-in prompt, and append flags
   (`--append-system-prompt`, `--append-system-prompt-file`) which add to it.
   For planner use cases, the append mode is preferred: Claude keeps its
   built-in capabilities (tool use, reasoning) while receiving experiment-
   specific guidance. `ClaudeSession` supports all four flags but orders them
   so append variants take priority.

5. **Documented flags only** — the existing data-fitting planner uses `-s`
   (undocumented short form) and `--no-input` (undocumented). The library
   replaces these with documented equivalents: `--append-system-prompt` (or
   file variant) instead of `-s`, and drops `--no-input` since `-p` already
   implies non-interactive mode.

6. **No `cwd` parameter on `generate()`** — the planner subprocess already
   runs from `planner_root` (set by `SubprocessPlannerSession` in
   `src/eden/planner.py`), so the CLI inherits the right working directory.

### 3. System prompt → file

Create `example/data-fitting/planner/CLAUDE.md` with the current
`_SYSTEM_PROMPT` content. Remove the `_SYSTEM_PROMPT` constant from the
planner script. The `ClaudeSession` is instantiated with
`append_system_prompt_file=Path("CLAUDE.md")`, which passes
`--append-system-prompt-file CLAUDE.md` on the first call. This preserves
Claude's built-in prompt and adds the experiment-specific guidance on top.

### 4. Update data-fitting planner

After these changes, `example/data-fitting/planner/plan.py` drops:
- `read_trial_artifact()` — use `ctx.read_trial_artifact()`
- `_SYSTEM_PROMPT` — moved to `CLAUDE.md`
- `_session_started` global — managed by `ClaudeSession`
- `generate_claude_proposal()` — replaced by `session.generate()`

The planner creates a `ClaudeSession(append_system_prompt_file=Path("CLAUDE.md"))`
at module level and uses it in the reactive callback.

Approximate result (~100 lines, down from current 195):

```python
from pathlib import Path
from eden.planner_kit import ClaudeSession, PlannerContext, Proposal, run_planner

session = ClaudeSession(append_system_prompt_file=Path("CLAUDE.md"))

def format_history(ctx: PlannerContext, trials: list[dict]) -> str:
    # Uses ctx.read_trial_artifact() instead of local function
    ...

def _make_reactive_proposal(ctx, proposal_index, trial):
    all_trials = ctx.get_all_trials(order_by="r_squared DESC")
    history = format_history(ctx, all_trials)
    prompt = f"Latest trial results:\n{history}\n\nPropose the next approach."
    plan_text = session.generate(prompt) or _fallback_text(best)
    ...
```

## Files to create/modify

| File | Action | Description |
|---|---|---|
| `src/eden/planner_kit.py` | Modify | Add `read_trial_artifact`, `AgentSession`, `ClaudeSession`; add `artifacts_dir` to `PlannerContext` and `run_planner()` |
| `example/data-fitting/planner/CLAUDE.md` | Create | System prompt extracted from `_SYSTEM_PROMPT` |
| `example/data-fitting/planner/plan.py` | Modify | Use `ctx.read_trial_artifact()`, `ClaudeSession`, drop local implementations |
| `tests/test_planner_kit.py` | Modify | Add tests for new library functionality |
| `tests/test_data_fitting_demo.py` | Modify | Add `test_data_fitting_planner_reactive_proposal` |

## New tests

- `test_read_trial_artifact_returns_content` — reads an existing artifact file
- `test_read_trial_artifact_returns_none_for_missing` — file doesn't exist
- `test_read_trial_artifact_strips_whitespace` — trailing newlines stripped
- `test_planner_context_read_trial_artifact` — method delegates correctly
- `test_claude_session_append_system_prompt_file` — verifies `--append-system-prompt-file` flag
- `test_claude_session_append_system_prompt` — verifies `--append-system-prompt` flag
- `test_claude_session_replace_system_prompt` — verifies `--system-prompt` flag
- `test_claude_session_replace_system_prompt_file` — verifies `--system-prompt-file` flag
- `test_claude_session_no_system_prompt` — no system prompt flags
- `test_claude_session_continuation` — verifies `-c` flag after first success
- `test_claude_session_retry_after_failure` — still sends system prompt on retry
- `test_claude_session_rejects_multiple_prompt_options` — ValueError on ambiguous config
- `test_agent_session_returns_none_on_timeout` — timeout handling
- `test_agent_session_returns_none_on_missing_binary` — FileNotFoundError

In `tests/test_data_fitting_demo.py`:
- `test_data_fitting_planner_reactive_proposal` — patches `session.generate()`, verifies the refactored data-fitting planner builds a reactive proposal correctly from trial history

## Imports added to `planner_kit.py`

```python
from abc import ABC, abstractmethod
```

## Verification

1. `uv run -m pytest -q tests/test_planner_kit.py` — unit tests pass
2. `uv run -m pytest -q tests/test_e2e.py` — E2E unchanged (seed-sum planner unaffected)
3. `uv run -m pytest -q tests/test_data_fitting_demo.py` — data-fitting tests pass
4. `uv run ruff check .` — lint clean
5. `uv run pyright` — type checking passes
