# Plan: Three-Level Permission Model + Rename

## Context

The current direvo architecture has two directory levels: experiment_root (everything) and workspace_root (git repo). All scripts, databases, proposals, and artifacts live at experiment_root, meaning the planner subprocess has access to evaluation scripts and test data — enabling reward hacking by frontier models. We need a three-level ownership hierarchy with filesystem-enforced isolation, plus a rename of `execute_command` → `implement_command` to better describe the actor's role.

## Three-Level Ownership Model

```
experiment_root/                    # Level 1: orchestrator owns
├── .direvo/
│   ├── config.yaml                 # experiment definition
│   ├── results.db                  # orchestrator writes, planner reads (automatic)
│   ├── artifacts/                  # orchestrator writes, planner reads (automatic)
│   └── session.log
├── [experiment-specific files: eval scripts, held-out data, etc.]
│
└── planner_root/                   # Level 2: planner owns
    ├── [planner scripts]
    ├── .direvo/
    │   ├── proposals.db            # planner writes proposals
    │   ├── proposals/              # planner writes plan docs
    │   ├── results.db → ../../.direvo/results.db      # auto-symlink
    │   └── artifacts/ → ../../.direvo/artifacts/      # auto-symlink
    │
    └── workspace/                  # Level 3: implementer owns
        └── .git/
```

**Ownership principle**: files live at the level of the actor that creates/manages them.

**Containment invariants** (validated at config load):
- `planner_root` must be under `experiment_root`
- `workspace` must be under `planner_root`

**Default access** (downward reads implicit):
- Orchestrator (root): sees everything. Runs evaluate_command as root (NOT as trial user).
- Planner: sees planner_root + workspace. No access to experiment_root.
- Implementer (trial-{slot}): sees only their worktree. No access above.

**Automatic infrastructure access**: The system automatically symlinks core direvo infrastructure (results.db, artifacts/) from experiment_root into planner_root so the planner can read them without explicit config. These are not experiment-specific — every experiment needs them.

**Command execution model**:
- `implement_command`: runs as **trial-{slot}** (CWD=worktree). Grant symlinks are present in the worktree during this phase. If the command needs a script file, the user places it on PATH, in the workspace, or explicitly grants it via `file_permissions`.
- After implement, before commit: **remove all grant symlinks** from the worktree. They've served their purpose and must not be committed.
- Commit: `git add -A && git commit` (current behavior, captures new files the implementer created).
- `evaluate_command`: runs as **root** (CWD=worktree). Eval accesses experiment_root files directly (resolved to absolute paths), no grant symlinks needed. **Eval is read-only**: the orchestrator resets any eval artifacts after (`git checkout . && git clean -fd`). This is a structural guarantee.
- `plan_command`: runs as **planner** (CWD=planner_root).

**Cross-level grants** (`file_permissions`) are for **experiment-specific files** only. Example for a data-fitting experiment:
```yaml
file_permissions:
  - path: train.csv                 # relative to experiment_root
    grant: implementer              # read-only symlink into worktree
```

**All grants are read-only.** There is no `rw` mode — writable cross-level grants would let agents mutate shared state outside their ownership scope. If an agent needs writable access to external data, the experiment should copy it into the agent's scope instead.

**Grant validation**: paths must be relative (no `..` or absolute), must not collide with `.direvo/` or other system-managed paths, and must reference existing files.

**Grant mechanism**: orchestrator creates symlinks in the target's scope during bootstrap (planner grants) or trial preparation (implementer grants). Directory traversal (mode 711) on experiment_root allows symlink resolution without directory listing.

**SQLite journal modes**: results.db uses DELETE mode (not WAL) so the planner can read it with only read permissions. proposals.db keeps WAL (both actors have full access).

---

## Phase 1: Rename `execute` → `implement`

Pure mechanical rename, no behavior change. Establishes the new naming before structural changes.

### `src/direvo/models.py`
- `execute_command: str` → `implement_command: str`
- `execution_timeout_sec` → `implement_timeout_sec`

### `src/direvo/config.py`
- `_validate_execute_command()` → `_validate_implement_command()`
- YAML field: `execute_command` → `implement_command`
- YAML field: `execution_timeout_sec` → `implement_timeout_sec`
- All references in `load_config()` return value

### `src/direvo/execution.py`
- `ExecutionManager` → `ImplementationManager`
- `run_execution()` → `run_implementation()`
- `ExecutionResult` → `ImplementationResult`
- `self.execute_command` → `self.implement_command`
- `_render_execute_command()` → `_render_implement_command()`
- Constructor kwarg: `execute_command=` → `implement_command=`

### `src/direvo/orchestrator.py`
- `execution_manager` → `implementation_manager`
- `ExecutionManager` import → `ImplementationManager`
- `ExecutionResult` → `ImplementationResult` (if referenced)
- `config.execute_command` → `config.implement_command`
- `config.execution_timeout_sec` → `config.implement_timeout_sec`
- `execution_result` variable → `implementation_result`
- Log event `"execution_complete"` → `"implementation_complete"`

### `src/direvo/cli.py`
- Doctor checks: `execute_command` → `implement_command`

### All test files
- YAML configs: `execute_command:` → `implement_command:`
- YAML configs: `execution_timeout_sec:` → `implement_timeout_sec:` (if present)
- Python references to renamed classes/methods
- Files: test_config.py, test_execution.py, test_orchestrator.py, test_cli.py, test_smoke.py, test_entrypoint.py, test_runtime.py, test_docker_integration.py, test_permission_boundary.py, test_e2e.py

### Fixture files
- `tests/fixtures/experiment/.direvo/config.yaml`: `execute_command` → `implement_command`
- `tests/fixtures/experiment/execute.py` → rename to `implement.py`

### Documentation
- `AGENTS.md`: all references

---

## Phase 2: Add `planner_root` to config and model

### `src/direvo/models.py`
- Add `planner_root: Path` field to `SessionConfig` (after `experiment_root`)

### `src/direvo/config.py`
- Parse `planner_root` field (required string, resolve relative to experiment_root)
- Change resolution bases:
  - `proposals_db`: resolve against `planner_root` (was experiment_root)
  - `proposals_dir`: resolve against `planner_root` (was experiment_root)
  - `workspace`: resolve against `planner_root` (was experiment_root)
  - `results_db`: stays experiment_root (orchestrator owns)
  - `artifacts_dir`: stays experiment_root (orchestrator owns, planner reads via symlink)
- `plan_command`: resolve against `planner_root` (was experiment_root)
- `implement_command`: stays experiment_root
- `evaluate_command`: stays experiment_root

### `src/direvo/orchestrator.py`
- `bootstrap()`:
  - Create `experiment_root / ".direvo"` (config, results.db, artifacts, session.log)
  - Create `planner_root / ".direvo"` (proposals.db, proposals/)
  - Create auto-symlinks in `planner_root/.direvo/`:
    - `results.db → experiment_root/.direvo/results.db`
    - `artifacts → experiment_root/.direvo/artifacts`
  - Validate containment: planner_root under experiment_root, workspace under planner_root
- `ensure_trial_directories()`: proposals_dir under planner_root, artifacts_dir under experiment_root
- `_run_claimed_trial()` — restructure trial flow:
  1. Create grant symlinks for implementer grants in worktree
  2. Run implement_command (as trial-{slot})
  3. Remove all grant symlinks from worktree
  4. Commit all changes (`git add -A && git commit`) — captures implementer's work, grants are gone
  5. Run evaluate_command (as root, `user=None`) — reads committed state, accesses eval files directly
  6. Reset eval artifacts (`git checkout . && git clean -fd`)
  7. Record results using commit SHA from step 4
  - Grant symlinks are transient: present only during implement, removed before commit
  - Eval runs as root with access to experiment_root, no grants needed

---

## Phase 3: Update planner CWD

### `src/direvo/planner.py`
- `SubprocessPlannerSession.__init__`: rename `experiment_root` param → `planner_root`
- `self.experiment_root` → `self.planner_root` throughout
- CWD in `start()`: `cwd=self.planner_root`
- CWD in `_planner_command()`: `cd {self.planner_root}`

### `src/direvo/orchestrator.py`
- `create_planner_session()` call: pass `planner_root=config.planner_root` instead of `experiment_root=config.experiment_root`

### `src/direvo/planner.py` — `create_planner_session()`
- Rename `experiment_root` param → `planner_root`

---

## Phase 4: Split DB journal modes

### `src/direvo/db.py`
- Add `results_journal_mode: str = "DELETE"` and `proposals_journal_mode: str = "WAL"` to `DatabaseManager`
- Or simpler: add a `journal_mode` parameter to `_connect()` and pass the appropriate mode per DB
- `_connect()` currently does `conn.execute("PRAGMA journal_mode=WAL")` unconditionally
- Change to: accept `journal_mode` parameter, default `"WAL"`
- In `initialize()`: use DELETE for results_db, WAL for proposals_db
- All methods that open results_db use DELETE mode; all that open proposals_db use WAL

Implementation approach — add per-DB journal mode config to DatabaseManager:
```python
@dataclass(slots=True)
class DatabaseManager:
    results_db: Path
    proposals_db: Path
    metrics_schema: dict[str, str]
    busy_timeout_ms: int
    results_journal_mode: str = "DELETE"
    proposals_journal_mode: str = "WAL"
```

Then `_connect(self, path)` checks which DB it's connecting to and uses the appropriate mode. Or cleaner: `_connect(self, path, journal_mode)` and callers pass the right mode. Since `_connection()` wraps `_connect()`, update `_connection()` to accept journal_mode too.

Actually simplest: store journal modes in a dict keyed by path, set during init.

---

## Phase 5: File permissions grants

### `src/direvo/models.py`
- Add `FilePermissionGrant` dataclass:
  ```python
  @dataclass(frozen=True)
  class FilePermissionGrant:
      path: str            # relative to experiment_root
      actor: str           # "planner" or "implementer"
  ```
- Add `file_permissions: tuple[FilePermissionGrant, ...]` to `SessionConfig` (default empty tuple)
- All grants are read-only by design (no mode field needed)

### `src/direvo/config.py`
- Parse `file_permissions` list from YAML
- Validate each entry: path is a string, grant is `"planner"` or `"implementer"`
- Actor must be "planner" or "implementer" (no mode field — all grants are read-only)

### `src/direvo/orchestrator.py` — bootstrap
- After directory creation, process planner grants:
  - For each grant where `actor == "planner"`:
    - Source: `experiment_root / grant.path`
    - Target: `planner_root / grant.path` (mirror the relative path)
    - Create parent dirs at target, create symlink
- Store implementer grants for use during trial preparation

### `src/direvo/orchestrator.py` — grant lifecycle
- **Create** (in `_run_claimed_trial`, before implement):
  - For each grant where `actor == "implementer"`:
    - Source: `experiment_root / grant.path`
    - Target: `worktree_path / grant.path` (mirror relative path to preserve structure)
    - Create parent dirs at target, create symlink
    - Skip if target already exists (don't overwrite git-tracked files)
- **Remove** (in `_run_claimed_trial`, after implement, before commit):
  - Delete all grant symlinks created in the create step
  - Grants are transient — they must not be committed to the trial branch

### New helper: `src/direvo/grants.py` (or add to `worktree.py`)
- `create_grant_symlinks(grants, source_root, target_root)` — creates symlinks for a list of grants
- `create_worktree_grant_symlinks(grants, source_root, worktree_path)` — per-trial symlinks

---

## Phase 6: Runtime permission setup

### `src/direvo/runtime.py` — `prepare()`
Major refactor of the permission setup:

**Directory ownership (three levels):**
```python
# Level 1: experiment_root — root-only, traverse-only for others
self._apply_directory_permissions(config.experiment_root, user="root", group="root", mode=0o711)
self._apply_directory_permissions(config.experiment_root / ".direvo", user="root", group="root", mode=0o711)

# Level 1 infrastructure readable by planner (for auto-symlinks)
self._apply_tree_permissions(config.artifacts_dir, user="root", group="planner",
    directory_mode=0o750, file_mode=0o640)  # orchestrator writes, planner reads via symlink
self._apply_file_permissions(config.results_db, user="root", group="planner", mode=0o640)

# Level 2: planner_root — planner owns
self._apply_directory_permissions(config.planner_root, user="planner", group="planner", mode=0o750)
self._apply_tree_permissions(config.proposals_dir, user="planner", group="root", ...)
self._apply_file_permissions(config.proposals_db, user="planner", group="root", mode=0o660)

# Level 3: workspace — managed by orchestrator, planner reads, worktrees owned per-slot
self._apply_directory_permissions(config.workspace_root, user="root", group="planner", mode=0o751)
```

**Grant permissions:**
- For each planner grant: set source file readable by planner via group or mode
- For each implementer grant: set source file readable by trial users
- Ensure experiment_root and intermediate dirs have 711 (traverse) for grant targets

**Ancestor traversal:**
- `_ensure_ancestor_traversal()` for planner_root and workspace_root

---

## Phase 7: Tests

### Update existing tests
- All YAML configs: `execute_command` → `implement_command`, `execution_timeout_sec` → `implement_timeout_sec`
- All Python references to renamed classes/methods/variables
- Config tests: add `planner_root` field coverage

### New tests

**test_config.py:**
- `planner_root` resolves relative to experiment_root
- Containment validation: planner_root must be under experiment_root, workspace under planner_root
- proposals_db/proposals_dir resolve relative to planner_root
- results_db/artifacts_dir resolve relative to experiment_root
- `plan_command` resolves against planner_root
- `file_permissions` parsing and validation (reject `..`, absolute paths, `.direvo/` collisions)
- Invalid file_permissions entries raise ConfigError

**test_planner.py:**
- Planner CWD is planner_root (not experiment_root)

**test_e2e.py:**
- Update fixture config for renamed fields
- Update fixture directory structure for three-level model
- Update `tests/fixtures/experiment/plan.py` to open results.db read-only (not set WAL mode)

**test_runtime.py:**
- Three-level permission setup
- Grant symlink creation
- Auto-symlinks for results.db and artifacts

**test_grants.py** (new):
- Symlink creation for planner grants during bootstrap
- Symlink creation for implementer grants during trial preparation
- Symlinks resolve correctly
- No overwrite of existing files in worktree
- All grants are read-only (no rw mode exists)
- Invalid grant paths raise appropriate errors

**test_db.py** (new or extend existing):
- results.db uses DELETE journal mode
- proposals.db uses WAL journal mode
- Read-only access to results.db works (simulating planner)

---

## Phase 8: Documentation

### `AGENTS.md`
- Update directory layout diagram to three-level model
- Document `planner_root` config field
- Document `file_permissions` config section
- Update actor names: executor → implementer
- Document the ownership principle
- Document eval-is-read-only invariant (commit-before-eval)

---

## Execution Order

1. **Phase 1** (rename): mechanical, low risk, do first to establish naming
2. **Phase 2** (planner_root config): add the field, change path resolution
3. **Phase 3** (planner CWD): small, depends on Phase 2
4. **Phase 4** (DB journal modes): independent, can parallel with Phase 2-3
5. **Phase 5** (file permissions): depends on Phase 2
6. **Phase 6** (runtime permissions): depends on Phases 2+5
7. **Phase 7** (tests): incremental with each phase
8. **Phase 8** (docs): after implementation stabilizes

## Verification

Per-phase (unit tests only, fast):
1. After Phase 1: `grep -r "execute_command\|ExecutionManager\|run_execution\|execution_timeout" src/ tests/` → zero hits. Run unit tests.
2. After Phase 2-3: config and planner unit tests pass with planner_root. Containment validation rejects invalid paths.
3. After Phase 4: new DB test verifies `PRAGMA journal_mode` returns `delete` for results.db, `wal` for proposals.db.
4. After Phase 5: grant unit tests verify symlink creation, validation rejects bad paths.

Milestone (full suite + e2e):
5. After Phases 1-5: run full test suite + e2e test.
6. After Phase 6: runtime permission tests (may require root/Docker). Verify planner user cannot `ls` experiment_root, trial user cannot `ls` planner_root.
7. Final: full suite + e2e + `ruff check` + `pyright`.
