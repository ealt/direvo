# Plan: Distribute EDEN as `direvo`

## Context

EDEN is an orchestration engine for directed code evolution, currently at v0.1.0.
It's usable only via git clone. The goal is to make it distributable so users can
install a package, scaffold an experiment, and run it — without cloning this repo.

### Two distribution surfaces

The framework has two distinct things users consume:

1. **An executable engine** — users install it, write a config file, and call a
   CLI command (`eden run`, `eden docker run`) to launch experiments. This is a
   tool you install and invoke.

2. **Library/authoring code** — things like `planner_kit.py` and the example
   skills that users `import` or copy into their own planner/evaluator/implementer
   scripts. This is code that lives *inside* the user's experiment.

These are different distribution concerns. The engine is a CLI tool. The library
code is an SDK that experiment scripts depend on at runtime.

### Why SDKs (not just a Python library)

Planners communicate with the orchestrator via a language-agnostic protocol:

| Channel | Direction | What |
|---------|-----------|------|
| SQLite (results.db) | orchestrator → planner | Read completed trial results + metrics |
| SQLite (proposals.db) | planner → orchestrator | Write new experiment proposals |
| Filesystem | both directions | Proposal plan.md files, trial artifacts |
| Stdin | orchestrator → planner | Trial completion notifications (text lines) |
| Logging | planner → orchestrator | JSON lines to plan.log |

None of this is Python-specific. Any language with a SQLite driver can participate.
The SDK's job is to encapsulate the schema knowledge and provide ergonomic helpers
so planner authors don't need to know DB internals. Users should be able to write
planners in Python, TypeScript, or anything else.

The current `planner_kit.py` (~457 lines) is small and well-factored:
- **Low-level primitives**: `connect_results_db`, `connect_proposals_db`,
  `create_proposal`, `get_trial`, `get_all_trials`, `read_trial_artifact`,
  `iter_trial_notifications`, `configure_logging`, `log_event`
- **High-level runner**: `run_planner()` — an opinionated main loop
  (initial proposals → stdin notification loop → reactive proposals)
- **Agent helpers**: `AgentSession`, `ClaudeSession` — subprocess wrappers

The design principle: **primitives are the foundation, `run_planner` is a
convenience built on top.** Both are public API. Users who want custom control
flow (batch analysis, polling, async, external integrations) use the primitives
directly. The runner covers the common case without tying hands.

### Schema versioning is a non-issue

The SDK does `SELECT *` and returns dicts/objects. It doesn't need to know metric
column names at compile time. The only baked-in schema knowledge is:
- The proposals table structure (fixed, 7 columns)
- The trials base columns (`_TRIAL_META_COLUMNS`) — everything beyond is
  user-defined metrics returned as generic key-value pairs
- Status enum values

As long as the SDK targets the same protocol version as the engine, it works.

### Starter assets are copyable, not importable

AGENTS.md and the planner skills (`.agents/skills/*.md`) are documentation and
guidance for AI agents acting as planners. They should be copied into each
experiment and edited by the user — they're templates, not library code. The
`eden init` command handles this scaffolding.

## Three-layer distribution model

1. **Engine** (`direvo` on PyPI) — `pip install direvo` gives users the `eden`
   CLI and `eden` Python module
2. **Planner SDKs** (per language) — Python SDK ships with the engine package;
   TypeScript as `@direvo/planner-kit` on npm
3. **Starter assets** (`eden init`) — scaffolds a new experiment with config,
   planner template, AGENTS.md, and skills

## Decisions

- **Package name**: `direvo` on PyPI and npm (see `docs/naming.md` — "eden" is
  too common across registries). Import stays `import eden`. Users think of the
  tool as EDEN; the package name is just how you install it.
- **CLI commands**: Both `eden` and `direvo` work (dual entry points in
  pyproject.toml). Brand name at the command line.
- **Config directory**: `.eden/` is the default everywhere — `eden init`,
  Docker CMD, documentation, all examples. `.direvo/` is a compatibility alias:
  the engine recognizes it for experiment root inference (`config.py` and
  `entrypoint.sh`) so users who prefer the package name can use it, but we
  don't generate it, default to it, or document it as primary.
- **Module name**: Stays `eden` (same pattern as Pillow→PIL, beautifulsoup4→bs4).
  Renaming imports would touch every file for no user benefit.
- **Monorepo**: TypeScript SDK lives in `packages/planner-kit-ts/` in this repo.
  Both SDKs must stay in sync with the SQL schemas (`src/eden/sql/`), and a
  monorepo makes protocol changes atomic.
- **Templates**: Bundled in `src/eden/templates/` via `importlib.resources`
  (same pattern already used by `db.py` and `docker_runner.py`).
- **TypeScript SDK scope**: Port the low-level primitives only. Defer
  `AgentSession`/`ClaudeSession` (TS planners use the Claude Agent SDK directly)
  and `runPlanner()` (ship primitives first, add a runner if demand emerges —
  the JS ecosystem prefers explicit async/await over callback frameworks).

### Docker install story

Currently `eden docker build` requires a source checkout: `_find_eden_source_tree()`
walks up directories to find `pyproject.toml`, then `build_image()` copies
`src/` and `pyproject.toml` into the Docker build context. The generated
Dockerfile does `COPY eden-src /app && RUN pip install /app`.

After publishing to PyPI, the generated Dockerfile should instead do
`RUN pip install direvo==X.Y.Z` (pinned to the invoking package's version).
This eliminates the source checkout requirement, makes `eden docker build` work
from a pip-installed package, and prevents protocol skew between the host CLI
and the container runtime.

**Phase 1** keeps the source-tree path working (it's needed for development).
**Phase 1b** (post-publish) changes `render_dockerfile()` to `pip install direvo`
by default, falling back to source-copy only when a source tree is detected (dev
mode). Specifically:
- `render_dockerfile()` line 57-60: change `COPY eden-src` →
  `RUN pip install direvo=={version}` (version read from `importlib.metadata`)
- `build_image()` line 179-189: skip `_find_eden_source_tree()` and source copy
  unless in dev mode
- Dev mode detection: check if running from a source checkout (pyproject.toml
  exists above the installed package) vs. a wheel install
- Docker CMD (line 121): already hardcodes `/experiment/.eden/config.yaml` —
  this stays as-is since `.eden/` is the default convention

### `eden init` success criteria

The scaffold is an **authoring skeleton**, not a runnable experiment out of the
box. The implement/evaluate commands are too experiment-specific for meaningful
defaults. After `eden init`, the user must:
1. Write their implement and evaluate scripts (stubs are provided as starting
   points showing the expected interface)
2. Configure `implement_command` and `evaluate_command` in the config
3. Set up their workspace repo content

The testable acceptance criterion is: `eden init` produces a directory whose
config passes `load_config()` validation (the template config uses placeholder
values that parse correctly). After the user completes steps 1-3, `eden doctor`
should pass. `eden docker run` success depends on user-written scripts and is
not testable from a blank scaffold.

---

## Phase 1: Package rename + dual CLI

Minimal changes to make `pip install direvo` work.

### Files to modify

**`pyproject.toml`**
- `name = "eden"` → `name = "direvo"`
- Add `direvo = "eden.cli:main"` to `[project.scripts]` (alongside existing `eden`)
- Expand `package_data` to explicitly include dotfiles in templates:
  `"templates/**/*", "templates/**/.*", "templates/**/.agents/**/*"`
  (setuptools globs don't match dotfiles by default, so `.agents/`, `.gitkeep`
  need explicit patterns)

**`src/eden/docker_runner.py:127-140`** — `_find_eden_source_tree()`
- Change `'name = "eden"'` check to accept both `"eden"` and `"direvo"`
- Update error message to mention the `direvo` package name

**`src/eden/config.py:143-147`** — `_infer_experiment_root()`
- `config_path.parent.name == ".eden"` → `config_path.parent.name in (".eden", ".direvo")`

**`src/eden/docker/entrypoint.sh:62`**
- `if [ "$config_dirname" = ".eden" ]` → add `|| [ "$config_dirname" = ".direvo" ]`

### Tests to add
- `test_config.py`: verify experiment root inferred from `.direvo/config.yaml`
- `test_docker_runner.py`: verify `_find_eden_source_tree` works with `name = "direvo"`
- `test_docker_runner.py` (Phase 1b): test both render paths — installed-package
  mode emits `pip install direvo==<current version>`, source-checkout mode still
  uses `COPY eden-src`
- `test_entrypoint.py`: add test for `.direvo/config.yaml` path (existing test
  surface at `tests/test_entrypoint.py` covers `.eden/`)
- Wheel resource test: build a wheel, inspect it, assert all template files
  including dotfiles (`.agents/skills/*.md`, `.gitkeep`) are present

---

## Phase 2: Extract and generalize starter templates

Create `src/eden/templates/` with experiment-agnostic scaffold files.

### Files to create

```
src/eden/templates/
  __init__.py
  config.yaml              — commented template with placeholder metrics and
                             TODO markers for implement/evaluate commands
  eval.py                  — stub evaluator: reads workspace, prints {"score": 0.0}
  implement.py             — stub implementer: reads plan.md, prints a message
  planner/
    plan.py                — minimal planner using run_planner() with stubs
    AGENTS.md              — generalized from example/data-fitting/planner/AGENTS.md
                             (remove r_squared/rmse references, use generic metrics)
    .agents/skills/
      navigate-workspace.md
      query-proposals.md
      query-trial-results.md   — generalized (no data-fitting metric names)
      read-trial-artifacts.md
      write-proposal.md
    workspace/.gitkeep
```

### Source material
- `example/data-fitting/planner/AGENTS.md` — mostly experiment-agnostic already,
  just remove metric-specific SQL examples
- `example/data-fitting/planner/.agents/skills/*.md` — `write-proposal.md` already
  references `eden.planner_kit` generically; `query-trial-results.md` has
  data-fitting-specific metric names to generalize
- `example/data-fitting/.eden/config.yaml` — strip to commented skeleton

---

## Phase 3: `eden init` CLI command

### Files to modify/create

**`src/eden/cli.py`**
- Add `init` subcommand: `eden init [directory]`
- Delegates to new `src/eden/init.py`

**`src/eden/init.py`** (new)
- `scaffold_experiment(target: Path) -> None`
- Uses `importlib.resources.files("eden.templates")` to traverse and copy
  (same pattern as existing `db.py:239` and `docker_runner.py:147`)
- Creates `.eden/` directory, copies config.yaml into it
- Runs `git init` + initial commit in `planner/workspace/` with explicit
  identity flags (`-c user.name=eden -c user.email=eden@experiment`) so it
  works on machines without global git config (mirrors the Docker path's
  `git_config` defaults)
- Prints summary of created files and next steps
- Aborts if target directory is non-empty (unless `--force`)

**`tests/test_init.py`** (new)
- Verify all expected files created
- Verify generated config loads with `load_config()`
- Verify `planner/workspace/.git` exists
- Verify abort on non-empty directory

---

## Phase 4: TypeScript SDK (`@direvo/planner-kit`)

Port the low-level protocol primitives from `planner_kit.py`. Defer
`AgentSession`/`ClaudeSession` (TS planners use Agent SDK directly) and
`runPlanner()` (ship primitives first, add runner if demand emerges).

### Files to create under `packages/planner-kit-ts/`

```
package.json           — @direvo/planner-kit, deps: better-sqlite3
tsconfig.json
src/
  index.ts             — re-exports
  db.ts                — connectResultsDb, connectProposalsDb, getTrial, getAllTrials
  proposals.ts         — createProposal (writes plan.md + DB row)
  artifacts.ts         — readTrialArtifact
  notifications.ts     — iterTrialNotifications (async generator, stdin)
  logging.ts           — configureLogging, logEvent (JSON lines to plan.log)
  git.ts               — getHeadSha
tests/                 — vitest tests mirroring test_planner_kit.py
```

### Protocol contract
The SQL schemas in `src/eden/sql/{proposals,results}.sql` are the source of
truth. Copy them into the TS package for reference. Both SDKs must produce and
consume identical SQLite row formats.

### CI
Add TypeScript build+test job to `.github/workflows/ci.yml`.

---

## Phase 5: Documentation + publish preparation

- Rewrite `README.md` for published package (install, quick start, architecture)
- Add PyPI metadata to `pyproject.toml` (urls, classifiers, license field)
- Update `docs/plans/v0.md` to reflect the new `init` CLI command and the
  `direvo` package name — v0.md is the implementation contract per AGENTS.md
  and must stay in sync with user-facing changes
- Create `packages/planner-kit-ts/README.md`
- Verify `direvo` availability on PyPI and npm (last checked 2025-03-27)

---

## Sequencing

**Before publishing** (blockers):
- Phase 1 — cannot publish as `direvo` without the rename
- Phase 2 + 3 — first-run experience (`eden init`) is critical for adoption
- Phase 5 — README and metadata

**After publishing** (independent):
- Phase 1b — Docker `pip install direvo` path (replaces source-copy in
  generated Dockerfile; dev-mode fallback preserved)
- Phase 4 — TypeScript SDK, separate npm publish cadence
- Future: `eden init --template <name>`, `--language typescript` flag

## Verification

After each phase, run:
```bash
uv run -m pytest -q           # unit + integration tests
uv run ruff check .           # lint
uv run pyright                # type check
```

After Phase 3, end-to-end:
```bash
uv run eden init /tmp/test-experiment
uv run eden doctor --config /tmp/test-experiment/.eden/config.yaml
```
