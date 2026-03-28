# Docker Validation

This document records the completed Docker/root validation results and keeps
the exact rerun procedure in one place for future verification.

## Status

Validation is complete.

Latest recorded successful run:

- host sanity check: `uv run -m pytest -q` -> `59 passed, 2 skipped`
- Docker smoke: `./scripts/run_docker_integration.sh` -> passed
- privileged boundary test: `./scripts/run_privileged_validation.sh` -> passed
- privileged full suite: `./scripts/run_privileged_validation.sh full` ->
  `60 passed, 1 skipped`

Those results close the environment-heavy validation gap for the current
implementation.

## Current Baseline

The repo-local implementation is currently green in a non-Docker environment:

- `uv run -m pytest -q` -> `59 passed, 2 skipped`

Recent privileged-suite fixes already folded into the repo:

- root-mode execution, planner startup, and worktree ownership now degrade
  safely when `planner` / `trial-*` users have not been provisioned yet
- merge-path tests no longer assume the default branch is `master`
- the Docker smoke launcher now prefers `uv run` on the host
- the smoke test now runs runtime preparation before invoking the CLI, which
  matches the real container entrypoint path
- planner subprocess unit tests that are not about privilege-switching now run
  with `user=None`, so they do not depend on ambient root/container user state

The two skipped tests in the normal non-Docker host suite are the
environment-gated validations covered by this runbook:

- [test_docker_integration.py](/direvo/tests/test_docker_integration.py)
- [test_permission_boundary.py](/direvo/tests/test_permission_boundary.py)

## Purpose

Validate:

- the real Docker image path
- the container entrypoint behavior
- root/user permission boundaries

Use this document when you want to rerun the Docker/root checks after future
changes.

## Preconditions

The agent running these steps needs:

- Docker installed and usable
- permission to run Linux containers
- internet/package access inside the validation container for `apt` and `pip`
- a checkout of this repository with the current `scripts/` directory present

## Files To Use

Run the validations through these repo scripts:

- [run_docker_integration.sh](/direvo/scripts/run_docker_integration.sh)
- [run_privileged_validation.sh](/direvo/scripts/run_privileged_validation.sh)

Do not retype the long Docker commands unless you are debugging the scripts
themselves.

## Fast Sanity Check

From the repo root:

```sh
uv run -m pytest -q
```

Expected:

- local suite passes

If this fails, stop. That is no longer a Docker-specific problem.

## Rerun Order

Run the validations in this order:

1. local sanity check
2. Docker smoke test
3. privileged permission-boundary test
4. optional full privileged suite

This order matters because Step 1 confirms the checkout is healthy before you
start debugging container behavior.

## Step 1: Docker Smoke Test

Recommended command:

```sh
./scripts/run_docker_integration.sh
```

What this does:

- builds the repo `Dockerfile`
- creates a temp git workspace
- seeds a ready proposal
- runs the image with:
  - mounted `/workspace`
  - real entrypoint
  - config at `/workspace/.direvo/config.yaml`
- verifies:
  - successful trial row in `results.db`
  - committed artifact copy exists

Expected:

- the script exits `0`
- pytest reports the Docker integration test passed
- the test proves:
  - the image builds
  - the entrypoint runs
  - a mounted workspace can execute a real session
  - `results.db` and artifacts are produced correctly

If it fails, capture:

- full pytest output
- `docker build` stderr/stdout
- `docker run` stderr/stdout
- contents of `/workspace/.direvo/session.log` from the temp workspace

## Step 2: Privileged Permission-Boundary Test

This must run as `root` in an ephemeral Linux environment. Do not run this on a
long-lived host because it creates `planner` and `trial-*` users.

Recommended command:

```sh
./scripts/run_privileged_validation.sh
```

What this does:

- runs in a disposable container as root
- installs the minimal system dependencies needed by the runtime
- installs the repo in editable mode
- executes the real privileged permission test

What the test validates:

- `planner` can read `.git/HEAD`
- `planner` can read `results.db`
- `planner` can write `proposals.db`
- `trial-0` can access its own worktree
- `trial-0` cannot read `.git/HEAD`
- `trial-0` cannot read `results.db`
- `trial-0` cannot access `trial-1`'s worktree
- slot worktree roots are mode `0700`

Expected:

- the script exits `0`
- pytest reports the permission test passed
- the runtime proves the intended Linux user boundaries actually hold in a real
  root-owned container

If it fails, capture:

- full pytest output
- `ls -ld` for:
  - `.git`
  - `.direvo/results.db`
  - `.direvo/proposals.db`
  - `worktrees/wt-0`
  - `worktrees/wt-1`
- `id planner`
- `id trial-0`
- `id trial-1`

## Step 3: Optional Full Batch

If both targeted tests pass, run the full suite inside the root validation
container:

```sh
./scripts/run_privileged_validation.sh full
```

Expected:

- the script exits `0`
- the full pytest suite passes inside the disposable root container

This step is useful because it catches interactions between the privileged test
setup and the rest of the runtime, not just the targeted boundary assertions.

This full-suite rerun is important. Earlier privileged full-suite runs exposed
root-only test-environment interactions that have since been fixed in the repo.
Do not assume the earlier failure still reproduces.

## Pass Criteria

The handoff is successful if:

- `tests/test_docker_integration.py` passes with Docker enabled
- `tests/test_permission_boundary.py` passes as root in an ephemeral container
- ideally, `./scripts/run_privileged_validation.sh full` also passes

If all three scripted runs pass, the Docker/root environment is behaving as
expected for the current implementation.

## What To Send Back

If everything passes, send back:

- the exact commands you ran
- the exit status of each command
- the final pytest summaries

If anything fails, send back:

- the exact command that failed
- the full terminal output
- whether the failure happened in:
  - image build
  - container startup
  - runtime permissions
  - git safety checks
  - pytest assertions
- the captured artifacts listed in the failure sections above

## Minimal Success Report Template

Use this format in the return message:

```txt
Environment:
- docker version: ...
- host OS: ...

Commands run:
1. uv run -m pytest -q
   Result: ...
2. ./scripts/run_docker_integration.sh
   Result: ...
3. ./scripts/run_privileged_validation.sh
   Result: ...
4. ./scripts/run_privileged_validation.sh full
   Result: ...

Notes:
- any deviations from the documented flow
- any warnings that did not fail the run
```
