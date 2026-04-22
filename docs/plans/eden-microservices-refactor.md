# EDEN Microservices Refactor

## Context

EDEN today runs as a single Docker container: orchestrator, planner (subprocess),
implementer/evaluator (subprocesses), Web UI, and SQLite databases all co-located.
This works for one experiment at a time on one host, but it blocks several
capabilities the project now needs:

- **Multi-experiment concurrency.** Research teams want to run many experiments
  in parallel on shared infrastructure, not one container per experiment per host.
- **Horizontal scaling.** Planners should scale within an experiment and
  across experiments. Within-experiment scaling specifically means running
  multiple *independent* logical planners (e.g., one Claude session, one
  human, one Codex session) in parallel — not replicating one shared-context
  planner; that would require session-sharing infrastructure outside the
  scope of this refactor. Implementers should scale across GPU nodes.
- **Human-in-the-loop roles for every role.** Planner, implementer, and evaluator
  must each be playable by humans (via Web UI), LLM agents, or custom scripts —
  and mixable in any combination. A fully-automated run, a fully-manual run, and
  any mix in between should all be expressible in the same config schema and
  run on the same infrastructure.
- **Cross-experiment data accumulation.** A shared proposal/history store lets the
  team learn across experiments over time.
- **Observability and manual override.** Users need SSH/Web-UI access to observe
  experiments and manually play the orchestrator role.

This plan is a **greenfield rewrite alongside** the current monolith. The existing
`src/eden/` package stays runnable until the new system reaches parity; the new
system lives in a parallel tree so neither blocks the other. The first milestone
is a Docker Compose deployment where all services run locally end-to-end for a
single experiment. Kubernetes comes later, after the service contracts are solid.

## Target Architecture

### Services

| Service | Role | Scaling |
|---|---|---|
| **Control plane** | Owns `experiments` table + orchestrator-pod leases. Setup script registers experiments here. Orchestrator pods claim ownership leases. | Single instance (stateless in front of Postgres) |
| **Orchestrator** | Per-experiment trial dispatch: claim proposals, dispatch work, integrate results into canonical git history, emit events, manage lifecycle. Sole writer to canonical `trial/*` branches. | Horizontally scaled; sharded by experiment via control-plane leases |
| **Planner service** | Hosts planner workers (LLM or human-via-UI). Workers subscribe to events and pull planning tasks. | Horizontally scaled; per-experiment config routes data access |
| **Evaluator service** | Hosts evaluator workers (LLM or human-via-UI). Same contract as planner workers, different task kind. | Horizontally scaled |
| **Implementer service** | Hosts implementer workers (LLM in a sandbox container, human-via-UI, or custom script). Workers operate on a dedicated per-trial `work/*` branch they fully control; they do not write to canonical branches. | Parallel within experiment via `parallel_trials` |
| **Web UI** | Shared shell (auth, navigation, experiment switcher) with per-service modules (orchestrator, planner, implementer, evaluator, observability). Humans use the per-role modules to claim and complete tasks. | Single deployment |
| **Setup script** | Takes an experiment config; builds experiment-specific image; initializes bare git repo; creates per-service configs; registers experiment with control plane. | CLI, not a service |

### Shared infrastructure

- **Postgres** (one instance for MVP) — pluggable behind a repository interface.
  Hosts: control plane tables, tasks queue, events log, planner proposals,
  experiment results, worker context stores.
- **Blob storage** — pluggable. Filesystem for MVP, S3/GCS later. Per-experiment
  subdirectory convention. Stores: proposal artifacts, trial artifacts, run logs.
- **Git hub** — a git-repo host. Central source of truth for experiment code.
  Orchestrator and implementers push/pull trial branches. **Gitea from MVP
  onward** (single lightweight container in Compose, same deployment
  upgraded to a managed instance in k8s for Milestone 3). Using Gitea from
  MVP solves a concrete problem: the human-implementer path requires a clone
  URL reachable from *outside* the Compose network, which is awkward with a
  raw bare repo on a Docker volume. Gitea exposes HTTP(+SSH) natively on a
  published host port, handles auth when we need it, and gives us real PRs
  for free in Milestone 3 with no code changes. The added MVP complexity is
  one container and a port publish — cheaper than building a bespoke git
  daemon wrapper and rewriting it later.

### Communication topology

```
setup script ──► control plane (REST)
                     │
                     ▼ (lease claim)
                 orchestrator pod
                     │
                     ├──► git hub (read all, write canonical trial/* branches only)
                     │
                     ├──► Postgres (tasks, events, proposals, trials)
                     │
                     ├──► implementer workers ──► git hub (work/* branches only)
                     │         (LLM container | human UI | script)
                     │
                     └──► planner + evaluator workers
                              (via Postgres events + tasks;
                               evaluator artifacts → blob storage)
```

The orchestrator is the sole integrator: it reads implementer `work/*` branches,
incorporates evaluator artifacts, and produces the canonical `trial/*` commit.

- Service-to-service **control** calls: REST (control plane, Web UI BFF endpoints).
- **Work distribution**: Postgres `tasks` table, claim semantics.
- **Event fan-out**: Postgres `events` table + `LISTEN/NOTIFY`.
- **Bulk state**: Postgres tables and blob storage, read directly by services
  that need them (no chatty RPC layer over simple queries).

## Service Contracts

These are the interfaces every service depends on. They are the highest-priority
design artifacts — once they're stable, services can be built independently.

### 1. Experiment config schema

A single YAML describes a whole experiment. Ported and extended from today's
`src/eden/config.py`. **No compatibility shim:** the new schema is a clean
supersede. Today's monolith keeps its config schema on `src/eden/config.py`;
the new system uses a new schema loaded by `packages/eden-contracts`. Existing
experiments (including the fixture at `tests/fixtures/experiment/.eden/config.yaml`)
are migrated manually as part of the Milestone 1 port work — this is an
exit-criterion activity, not a runtime concern.

New fields:

- `experiment_id` — UUID, assigned at registration.
- `control_plane_url` — where to register and claim leases.
- `git_hub.url` — central bare-repo location.
- `postgres.dsn` and `blob_storage.uri` — or references to shared defaults.
- `planner.workers[]` — list of worker specs (kind: `claude`|`codex`|`human`|`script`;
  parallelism; routing labels; command for LLM/script workers).
- `implementer.workers[]` — same shape. LLM/script workers run `implement_command`
  in a sandbox container; human workers use the Web UI.
- `evaluator.workers[]` — same shape. LLM/script workers run `evaluate_command`;
  human workers use the Web UI.
- Existing fields preserved: `metrics_schema`, `objective`, `max_trials`,
  `max_wall_time`, `parallel_trials`.

The setup script derives per-service **sub-configs** (e.g., the planner service
gets only the fields it needs: `experiment_id`, `postgres.dsn`, its
`workers[]` entry, routing labels).

### 2. Database schemas (Postgres)

- `experiments` (control plane): `id`, `name`, `config_json`, `status`,
  `created_at`, `owner_pod`, `lease_expires_at`.
- `tasks` (work queue): `id`, `experiment_id`, `kind`
  (`plan_request`|`implement`|`evaluate`), `payload_json`, `priority`, `status`
  (`ready`|`claimed`|`submitted`|`done`|`cancelled`|`failed`), `claimed_by`,
  `claimed_at`, **`claim_token UUID`** (new on each claim transition; required
  in every worker write; admin reclaim regenerates it so a stale worker
  cannot submit against a task it no longer owns), `routing_labels_json`,
  `created_at`. `implement` and `evaluate` tasks carry the trial id.

  **State machine (authoritative):**
  - `ready → claimed`: atomic claim, sets `claimed_by`, `claimed_at`.
    Only the claimant may mutate the row thereafter until release.
  - `claimed → submitted`: worker finished its outputs; payload updated with
    submission pointer (SHA for implement, metrics for evaluate,
    proposal_id for plan_request).
  - `claimed → ready`: explicit release by the claimant (voluntary abandon).
  - `claimed → ready` by **admin reclaim**: an operator (via CLI or Web UI)
    forcibly releases a stranded claim. This is the MVP recovery path for a
    dead or stuck worker — no automatic TTL, but explicit human reclaim is
    always available. Required for Milestone 1 exit criterion #4.
  - `submitted → done`: orchestrator finished integrating the submission.
  - `submitted → failed`: integration failed. `payload_json.failure` has a
    `kind` field: `validation` (permanent — invalid SHA, malformed metrics,
    proposal failed sanity checks), `backpressure` (transient — proposal
    pool at cap; re-enqueue safe), or `integrator_error` (orchestrator-side
    bug/transient; can be retried). Re-enqueueing a task is always a new
    task row with a fresh `claim_token`.
  - `ready|claimed → cancelled`: experiment termination or orchestrator
    decision; ignored going forward.
  - Only the claimant writes submission outputs; only the orchestrator
    transitions `submitted → done/failed`; admin reclaim is the sole
    cross-worker mutation.
  - Idempotency: a worker may submit once; re-submissions on an already-
    `submitted`/`done` task are rejected. Workers are expected to check
    current task status before doing heavy work on resume.
  - Lease TTL + auto-reclaim replaces manual admin reclaim in a later
    milestone; the state machine already accommodates it (same `claimed →
    ready` transition).
- `events` (pub/sub log): `id`, `experiment_id`, `kind` (`trial_completed`,
  `proposal_accepted`, `experiment_started`, `experiment_terminated`, etc.),
  `payload_json`, `created_at`. Consumers use `LISTEN experiment_<id>_events` +
  catch-up via `SELECT … WHERE id > last_seen`.

  **Transactional rule (invariant):** any durable state change that has a
  corresponding event — task status transition, proposal write, trial write,
  experiment lifecycle change — **must INSERT the event row in the same
  Postgres transaction as the state change**. `NOTIFY` is safe (Postgres
  fires it only after commit). This prevents consumers from observing
  inconsistent state where an event references a state change that's not
  yet committed, or where a committed state change has no event trail.
- `proposals` (shared across experiments): `id`, `experiment_id`, `slug`,
  `parent_commits` (array, length 1 in MVP), `plan_md_uri`,
  `artifacts_uri`, `priority`, `routing_labels`, `author_worker_id`,
  `author_kind`, `created_at`, `validated_at`, `trial_id` (nullable, set
  when dispatched), `status`, `failure_reason` (nullable).

  **Proposal status lifecycle (supersedes today's `drafting → ready →
  dispatched → completed`):**
  - `pending_validation`: just written by the planner API; awaiting
    orchestrator validation.
  - `ready`: passed validation; eligible for implementer dispatch. This is
    the "unconsumed" state counted against the proposal-pool cap above.
  - `dispatched`: orchestrator has enqueued an `implement` task referencing
    this proposal; `trial_id` is set.
  - `completed`: the associated trial reached a terminal state (any
    outcome — success, failure, cancelled). Proposals become queryable
    history at this point.
  - `invalid`: failed validation (missing parent commit, unresolvable
    artifact URIs, slug collision after auto-suffix retries, etc.).
    Retained for debugging with `failure_reason` populated.
  - `superseded`: optional — reserved for future cross-experiment
    deduplication. Not used in MVP.

  Legal transitions: `pending_validation → ready | invalid`,
  `ready → dispatched`, `dispatched → completed`. No other transitions are
  permitted; `invalid` and `completed` are terminal.
- `trials` (shared across experiments): `id`, `experiment_id`, `status`,
  `proposal_id`, `parent_commits` (array, length 1 in MVP; length ≥1 reserved
  for future multi-parent), `trial_branch`, `commit_sha`, `artifacts_uri`,
  `description`, `timestamps`, plus a **`metrics JSONB` column validated
  against the experiment's `metrics_schema` on write**. A single shared table
  cannot keep dynamic per-experiment columns (different experiments have
  different schemas), so metrics live in JSONB. Per-experiment **typed views**
  (never generated columns on the shared table, since those imply schema
  migrations) are created on-demand by the setup script at experiment
  registration time, based on the experiment's `metrics_schema` — they
  give UI dashboards and ad-hoc SQL a typed surface without mutating the
  shared table's schema. Tradeoff accepted: slightly worse ergonomics for
  cross-experiment arbitrary SQL analysis in exchange for a stable shared
  schema.
- `worker_context` (optional per-worker persistent notes): `worker_id`,
  `experiment_id`, `kind`, `content_blob_uri`, `updated_at`. Workers may
  ignore this entirely; it's there for implementations that want durable
  context across restarts.

All access goes through a **repository interface** so SQLite can be dropped in
for local/dev later.

### 3. Worker contract (all three roles)

Implementation-agnostic. Planner, implementer, and evaluator workers — LLM,
human, or script — all follow this contract. What differs per role is *what
task kinds they accept* and *where their outputs go*, not the lifecycle.

**Guiding principle — UI orchestrates, tools execute:** the Web UI's job is
task orchestration (discovery, claiming, manifest display, structured output
submission). The actual work happens in whatever environment the worker
chooses — their local IDE, an agentic TUI, a cloud workspace, a sandbox
container, whatever fits the task. The platform provides a bare repo and
well-defined input/output contracts; workers pick up, work in their preferred
environment, and submit results.

1. **Discover**: subscribe to `experiment_<id>_events`, and/or poll for ready
   tasks filtered by the worker's routing labels and the kinds it handles.
2. **Claim**: atomically claim a task (set `status=claimed`, `claimed_by=self`).
   A claim stays with the worker until they submit or explicitly release it.
   No automatic lease expiration in MVP; if a worker dies, an operator uses
   the admin-reclaim action (CLI or Web UI) to release the stranded claim
   so another worker can pick it up. Automatic TTL / heartbeat is post-MVP.
3. **Read context**: from `trials`, `proposals`, the experiment's git repo,
   blob storage, and optionally `worker_context`. The platform exposes a
   **task manifest** endpoint that bundles everything a worker needs to start
   (parent commit SHA, branch name, clone URL, proposal link, expected output
   schema).
4. **Execute**: do the work in whatever environment fits — LLM sandbox
   container, the worker's local editor, a cloud workspace, a terminal.
5. **Submit outputs**: write role-specific outputs (see per-role sections
   below) and mark the task `status=submitted`.
6. **Optionally write context**: update `worker_context` if the worker wants
   to persist state across restarts.
7. **Release or terminate.**

Workers never write to canonical git state (`trial/*` branches, `experiments`,
`trials` rows that represent the canonical trial record). Those are written by
the orchestrator as it integrates submissions. This makes worker implementations
interchangeable and keeps blast radius limited when a worker misbehaves.

Redundant submissions across parallel workers (e.g., two LLM planners making
similar proposals) are acceptable in v1.

### 4. Per-role outputs and UI depth

The three roles differ in how much of the *work itself* reasonably happens in
the UI, versus in the worker's chosen environment. The UI is always the
orchestration surface; only some outputs are well-suited to in-UI authoring.

**Planner worker.** Output: a row in `proposals` (with `experiment_id`,
`author_worker_id`, `author_kind`, parent commit ref, artifact URIs) plus a
`plan.md`-style markdown document + any supporting artifacts uploaded to blob
storage. Proposal *metadata* (parent commit, slug, priority, routing labels)
is structured data with many auto-generatable defaults — **UI form is the
primary authoring surface** for humans. Proposal *content* (the markdown plan)
can be authored in a text area in the UI or, optionally, uploaded as a file
(upload post-MVP). No git interaction.

**Implementer worker.** Output: pointer to a final commit SHA on the
per-trial `work/trial-<id>-impl` branch in the experiment's bare repo. The
orchestrator creates the branch at the proposal's parent commit at
task-claim time. Workers have **full control of that branch only** — any
number of commits, any structure — pushed to the bare repo. **UI is purely
orchestration** for implementers: claim, show manifest (clone command,
branch name, parent SHA, link to proposal), submit SHA, release. No in-UI
editor. Humans work in Cursor/VS Code/Claude Code/terminal/wherever. LLM
workers run in a sandbox container (see Autonomous worker architecture below).

**Evaluator worker.** Output: metrics conforming to `metrics_schema` + any
artifacts (reports, logs, plots) uploaded to blob storage under
`trials/<trial_id>/eval/`. The **metrics form is UI-native** (structured data
the platform needs in-schema) and includes an artifact upload input. The
evaluation *work itself* — running the code, benchmarks, analyses,
visualizations — happens in whatever environment the evaluator chooses, just
like implementation. UI is the submission surface, not the workspace.
Evaluators do not write to git by default; the orchestrator records an eval
manifest (metrics + blob URIs) in the canonical trial commit — full artifact
payloads stay in blob storage. If an experiment ever needs evaluator-
generated code changes (rare), the same `work/trial-<id>-eval` branch
pattern is reserved; deferred until someone needs it.

### 5. Autonomous worker architecture (LLM / script workers)

A **worker host** is a long-lived process run by the implementer, planner, or
evaluator service. Each host polls the queue for tasks matching its labels,
and for each claimed task:

- **Planner / evaluator LLM hosts** (context-accumulating): the host itself
  runs the LLM session. One task's work happens inside the host's persistent
  session; the session accumulates context across tasks for the same
  experiment. This preserves today's single-persistent-planner-session
  behavior.
- **Implementer LLM hosts** (context-per-task): the host spawns a **per-task
  sandbox** to execute the actual work — in Compose, a `docker run` with
  `implement_command` as entrypoint; in k8s (Milestone 3), a Job on a
  GPU-labeled node. The sandbox clones the bare repo, checks out the
  `work/*` branch at the parent commit, runs `implement_command`, commits,
  pushes, exits. The host waits for sandbox completion, reads exit status,
  and submits the final SHA. Sandbox-per-task matches today's fresh-subprocess
  model, keeps blast radius contained, and maps cleanly to k8s Jobs.

**Context model for planner/evaluator hosts (important):** when multiple
planner hosts run for the same experiment, each host is a **separate logical
planner with its own independent context**. Hosts do not share session
state. This is by design — two Claude hosts, a Codex host, and a human tab
all coexist as independent planners generating (possibly redundant)
proposals. The platform coordinates via shared `proposals` / `events` / `trials`
tables; session state is host-local. **Replica-scaling of a single logical
planner's throughput** (same context across N replicas) is not supported and
not planned; it would require shared session persistence and reconciliation
logic that's out of scope for this refactor. If you need more parallelism
from a single logical planner, the recommended path is to run different
planner configurations (different prompts, different LLMs) rather than to
clone one.

Scaling autonomous workers is deployment config: run more host replicas for
more parallelism — understanding that each replica is logically independent.
Each host serves one task at a time.

Cloud-workspace-style workers (Codespaces, Cursor cloud, etc.) are a future
host variant: they provision a workspace on claim, hand the URL to a
pre-registered human, and wait for submission. Same contract; not in MVP.

### 5a. Control loop and experiment lifecycle

The orchestrator owns the control loop for each experiment it leases. Its
responsibilities, concretely:

**Experiment start.** On claiming a fresh experiment lease, the orchestrator:
1. Emits `experiment_started` event.
2. Seeds the initial planner queue by enqueuing `parallel_trials` count of
   `plan_request` tasks (so there's at least one proposal opportunity per
   implementer slot from the start).

**Planning-task replenishment.** Every time the orchestrator emits a
`trial_completed` event, it evaluates the planning buffer and enqueues more
`plan_request` tasks if needed. The target is: keep `ready` + `claimed` +
`submitted` `plan_request` tasks at roughly `parallel_trials` count, capped
at `3 * parallel_trials` to prevent runaway.

**Proposal-pool admission (hard cap, enforced at the API seam).** All
proposal writes go through the **planner service's submit API** — both
submissions tied to a `plan_request` task and proactive submissions that
don't involve a task. There is no direct-DB write path for proposals in
MVP (consistent with the "no direct DB submissions" MVP boundary). The API
enforces a **hard cap on unconsumed proposals** per experiment, where
"unconsumed" means `status IN ('pending_validation', 'ready')`. If the cap
is already at or above `3 * parallel_trials`:

- **Proactive submission** (no `plan_request`): the API rejects with HTTP 429
  and a `Retry-After` hint. Planners are expected to back off and retry.
  The Web UI surfaces the cap and current count so humans see when to stop
  submitting.
- **Plan-request-tied submission**: the proposal row is *not* created; the
  task transitions `submitted → failed` with `failure.kind=backpressure`.
  Backpressure-failed tasks are distinct from `failure.kind=validation`
  (permanent rejection) — they're safe to re-enqueue, and the orchestrator
  automatically re-enqueues an equivalent `plan_request` once the pool
  drops below the cap. The planner can therefore retry the same work
  without duplicating the proposal row.

This closes the proactive-path bypass: bounding the plan-request queue was
insufficient, but gating all writes at one API seam makes the cap
enforceable and observable.

**Planner submission validation.** When a `plan_request` reaches `submitted`
with a `proposal_id`, the orchestrator validates before transitioning to
`done`:
- Proposal exists with the given id and `experiment_id`.
- Each `parent_commits` entry exists in the experiment's bare repo.
- `slug` is unique within the experiment (auto-suffix if not).
- Any artifact URIs the proposal references resolve in blob storage.
- `metrics_schema` compatibility is checked only at evaluation time, not
  planning time.
Any failure transitions the task to `failed` with a reason in
`payload_json.failure`; the proposal row is marked invalid but retained for
debugging. A successful validation marks the proposal ready-to-implement and
the orchestrator enqueues the corresponding `implement` task.

**Termination.** The orchestrator owns stop-condition evaluation. After every
`trial_completed` (and on a periodic tick for wall-time), it checks:
- `max_trials` reached?
- `max_wall_time` exceeded?
- `objective` satisfied (if the config defines a satisfied-predicate)?
- Operator-triggered termination via the Web UI / CLI?

On termination the orchestrator transitions the experiment to `terminating`,
cancels all `ready` and `claimed` tasks for the experiment (setting
`status=cancelled`), waits for `submitted` tasks to finish integrating, emits
`experiment_terminated`, and transitions the experiment to `done`. Work
branches are retained; the lease is released.

**End-of-life (MVP).** Terminated experiments remain fully queryable in
Postgres, blob storage, and git. No archival, export, or delete tooling in
MVP — those land post-MVP. State-in-place is acceptable for the research
workflow because the cost is low (metrics rows are small; git and blob are
compressible/expire-able at the storage layer). Post-MVP: scripted archive
(freeze experiment data + optional compaction), export (portable tarball
with Postgres dump + blob + git bundle), and delete (full cleanup across
all three stores).

**Experiment config immutability.** Once registered, an experiment's config
is immutable — particularly `metrics_schema`, because typed views are
created at registration and evaluator validation depends on the schema.
Config edits require creating a new experiment. Post-MVP: a migration path
that drops+rebuilds typed views and re-validates historical metrics.

### 6. Git topology and the integrator role

- One **bare repo** per experiment, hosted by the git hub. Setup script
  initializes it and seeds the starting commit.
- **Branch namespaces:**
  - `main` / seeded refs — the starting state. Immutable after setup.
  - `work/*` — worker-controlled scratch branches. Written by implementers
    (and optionally evaluators). Never referenced as canonical.
  - `trial/<id>-<slug>` — canonical trial branches. **Written only by the
    orchestrator.** One commit per completed trial.
- **Parentage (MVP: single-parent only):** a proposal has exactly one parent
  commit in MVP. Multi-parent (merge-style) proposals — where a planner
  proposes combining two or more prior trials — are deferred. When they
  return, the explicit integration algorithm will be: orchestrator pre-merges
  the parents into a synthetic base commit on a `merge-base/*` branch, the
  implementer branches from that synthetic base, and the canonical `trial/*`
  commit records all original parents via `git commit-tree -p` with multiple
  `-p` flags. Until then, the `parent_commits` array in `trials` is always
  length 1.
- **Trial integration flow (orchestrator is the integrator):**
  1. Orchestrator creates `work/trial-<id>-impl` at the proposal's parent
     commit and enqueues an `implement` task pointing at that branch.
  2. An implementer worker claims the task, does its work on that branch,
     pushes, and submits.
  3. Orchestrator enqueues an `evaluate` task; an evaluator worker reads the
     implementation's tip commit, runs evaluation, uploads artifacts to blob
     storage, writes metrics.
  4. Orchestrator creates the canonical `trial/<id>-<slug>` branch from the
     proposal's parent commit. It squashes the implementer's `work/*` branch
     into a single commit, then adds an **eval manifest** (a small JSON/YAML
     file) under `.eden/trials/<id>/eval.json` containing metrics, blob URIs
     for each artifact, sizes, hashes, and evaluator identity. Full artifact
     payloads (logs, reports, plots, large data) stay in blob storage —
     **never wholesale-copied into git**. Small metadata or text excerpts
     below a configurable threshold (default 100KB total) may be inlined
     alongside the manifest. This keeps the trial commit a complete, git-
     native record of *what happened* while preventing repo size from
     blowing up with every trial. Sets a descriptive commit message from
     proposal + metrics.
  5. Work branches are retained for debugging but are not canonical; they can
     be garbage-collected on experiment archival.
- **Access control:** in MVP (Gitea in Compose, trust-the-network), no
  enforcement — all services and human users authenticate as the same shared
  operator. In Milestone 3 (Gitea on k8s with auth), per-branch ACLs limit
  workers to `work/*` and give the orchestrator write on `trial/*`. At that
  point, submissions can natively use **real pull requests** from `work/*` to
  `trial/*`, inheriting Gitea's review UI and approval state machine — no
  changes to our services, just enabling auth and ACLs on the already-deployed
  Gitea.
- **PR-style UX in MVP:** the Web UI renders submissions as a PR-like review
  page (diff, commit list, proposal metadata, metrics after evaluation) even
  even though MVP's Gitea runs without the PR review flow enabled (we gate
  integration on our own validation in MVP). Familiar affordance, same
  mental model across milestones; Milestone 3 enables native Gitea PRs in
  place of our UI rendering.
- **`main` stays pristine:** EDEN trials form a DAG of data points, not a
  linear trunk of accepted changes — so submissions never merge into `main`.
  `main` remains the experiment's starting point. Promoting a successful
  trial into `main` as a new baseline is a separate, explicit, out-of-MVP
  workflow.
- **Failure modes this defends against:**
  - Worker force-pushing over another trial: workers only touch their own
    `work/<trial-id>-*` branch.
  - Worker failing to commit, committing weirdly, or making multi-commit
    messes: orchestrator squashes; worker's branch shape doesn't matter.
  - Worker checking out the wrong parent: parent is set by orchestrator at
    branch creation; worker can't change it.
  - Worker pushing junk to `main` or `trial/*`: they don't have access to
    push there (in Milestone 3+) and the orchestrator's integration step
    ignores anything outside the designated `work/*` branch.
- **Integrator as the orchestrator (not a separate service):** trial
  integration is tightly coupled to trial lifecycle, so keeping it in the
  orchestrator avoids an extra service. Can be extracted later if
  git-integration concerns justify it.

## Repository Layout

The current monolith stays at `src/eden/`. New code lives in a parallel tree so
both run side-by-side during the transition.

```
services/
  control-plane/          # FastAPI, ~300 LOC
  orchestrator/           # FastAPI + async dispatch loop + git integrator
  planner/                # worker host: LLM sessions + HTTP-facade for humans
  implementer/            # worker host: LLM sandbox containers, script
                          #   runners, HTTP-facade for humans (patch submission)
  evaluator/              # worker host: LLM sessions + HTTP-facade for humans
  web-ui/                 # shell + per-role modules (planner, implementer,
                          #   evaluator, observability, manual-orchestrator)
packages/
  eden-contracts/         # Pydantic schemas for configs, tasks, events
  eden-storage/           # Repository interfaces + Postgres and SQLite impls
  eden-git/               # Git operations (port of src/eden/git_manager.py)
  eden-blob/              # Blob storage interface + filesystem and S3 impls
scripts/
  setup-experiment/       # CLI: register experiment, init git, build image
compose/
  docker-compose.yml
  postgres/, git-hub/, blob/   # persistent-volume stubs
tests/
  integration/            # end-to-end tests using Compose
```

## Components to Reuse from the Current Codebase

Not a rewrite-from-scratch — these modules have proven logic we port rather than
rebuild:

- `src/eden/git_manager.py` → `packages/eden-git`. Worktree and branch
  management logic is largely independent of the monolith. The orchestrator's
  integrator uses this package to squash `work/*` branches and compose
  canonical `trial/*` commits.
- `src/eden/execution.py` → `services/implementer/` (LLM/script worker
  entrypoint: subprocess runner) and `services/evaluator/` (metrics parsing).
  The `ImplementationManager` logic splits along its two roles.
- `src/eden/db.py` → `packages/eden-storage` (SQLite impl). The
  `DatabaseManager` patterns port to the Postgres impl as well.
- `src/eden/config.py` + `src/eden/models.py` → `packages/eden-contracts`.
  Schema validation patterns move wholesale; add the new fields.
- `src/eden/orchestrator.py` dispatch-loop logic → `services/orchestrator/`.
  The async slot-worker pattern becomes the per-experiment dispatch loop;
  proposal-claiming logic becomes Postgres-backed atomic updates.
- `src/eden/web/server.py` + `packages/web-ui/` → `services/web-ui/`. The
  Starlette server becomes the shell; existing React components become the
  orchestrator module.
- `src/eden/docker_runner.py` `render_dockerfile()` → `scripts/setup-experiment/`.
  The image-building path is reused, now targeting "experiment workspace image"
  rather than "all-in-one container."

## MVP — Milestone 1 (Compose, Single Experiment, End-to-End)

Goal: prove the service contracts by running one experiment from setup through
multiple trial cycles, all on one machine via `docker compose up`.

**In scope:**

- All services in single-replica form.
- One Postgres instance; one filesystem blob store; one Gitea instance as
  the git host.
- One experiment, registered via the setup script.
- Orchestrator performs git integration (squash `work/*` → `trial/*`, add
  eval manifest with blob URIs to artifacts).
- **One autonomous worker host per role, plus human-UI for all three:**
  - **Planner**: LLM worker host (long-lived Claude Code session
    accumulating context) + UI claim/form/markdown flow for humans.
  - **Implementer**: LLM worker host that spawns per-task sandbox containers
    running `implement_command` + UI claim/manifest/submit-SHA flow for
    humans (who work in their own editor/IDE/TUI and push to the `work/*`
    branch).
  - **Evaluator**: LLM/script worker host + UI claim/metrics-form/artifact-upload
    flow for humans (who run evaluation in their own environment).
- Web UI shows: experiment list, trial timeline, proposals table, task queue
  (filterable by kind + claim status), PR-style submission review page,
  manual dispatch controls, **admin-reclaim action on stranded claims**.
- Claim persistence without automatic lease TTL (claimed = taken until
  submit, release, or admin reclaim).
- Single-parent proposals only; multi-parent / merge proposals deferred.

**Out of scope for Milestone 1:**

- Multi-experiment correctness (leases, sharding).
- Kubernetes deployment.
- Auth and multi-tenancy (trust-the-network for MVP).
- S3 blob backend.
- Webhooks and external notification channels (Slack, email) for task
  availability — discovery is UI-polled in MVP.
- Native Gitea PR review flow for submissions (MVP uses Gitea as a plain
  bare-repo host with our own PR-style UI; native Gitea PRs land with
  Milestone 3 once auth and ACLs are configured).
- Cloud-workspace worker hosts (Codespaces, Cursor cloud).
- Lease TTL / auto-reclaim of abandoned claims.
- File upload for proposal plan.md (UI text area only in MVP).
- Durable worker-context persistence (stub the table; workers may ignore it).
- "Promote trial to main" workflow (out of scope; trials stay as `trial/*`
  branches).
- Direct-DB submission of proposals or metrics (out of MVP; all writes go
  through the planner-service / evaluator-service APIs, which enforce
  validation, caps, and backpressure).

**Milestone 1 exit criteria:**

1. `./scripts/setup-experiment <config.yaml>` provisions an experiment end-to-end.
2. `docker compose up` brings up all services.
3. The experiment runs continuously: orchestrator seeds `plan_request`
   tasks, planners submit proposals, orchestrator validates and enqueues
   `implement` tasks, implementers work on `work/*` branches, orchestrator
   squashes into `trial/*` commits with eval manifests, metrics land in
   Postgres, the Web UI reflects all of it. Termination via `max_trials`
   or `max_wall_time` cleanly cancels pending work and marks the
   experiment `done`.
4. A human can fully play any one role (planner, implementer, or evaluator) for
   at least one trial via the Web UI — proving the "any mix" requirement end-to-end.
5. **Worker-death recovery:** killing a worker mid-task, then invoking
   admin-reclaim via the Web UI, makes the task re-claimable and the
   experiment continues. Verified in the integration tests below.
6. The existing fixture experiment at
   `tests/fixtures/experiment/.eden/config.yaml` (manually migrated to the
   new config schema as part of Milestone 1 work) runs under the new system
   with comparable results to the current monolith.

## Milestone 2 — Multi-Experiment on Compose

Adds what the MVP deferred:

- Control-plane leases and sharding — spin up N orchestrator replicas in
  Compose; verify two experiments run concurrently with clean ownership.
- Experiment-switcher in the Web UI.
- Cross-experiment views in the shared planner (e.g., "proposals from all
  experiments" filtered by labels).
- Task-queue cancellation on experiment termination.
- Lease-reclaim path tested by killing an orchestrator replica mid-experiment.

## Milestone 3 — Kubernetes Port

- Helm chart for the services.
- Implementer becomes a k8s Job (GPU node selection).
- Postgres as a managed instance.
- Blob storage switches to S3/GCS.
- Gitea moved to a managed/k8s deployment with auth and per-branch ACLs
  enabled; native PRs replace our in-UI PR rendering.

## Verification Plan

**Unit/contract tests:**

- Repository interface contract tests run against both the SQLite and Postgres
  implementations.
- Event LISTEN/NOTIFY fan-out test: multiple subscribers, one publisher, all
  receive and catch-up correctly after disconnect.
- Task-claim concurrency test: N workers racing for M tasks yields exact
  claim semantics.
- **Transactional-event invariant test:** simulate failure *inside* the
  transaction before commit — state change and event insert both happen
  in-memory, then the tx is aborted. Assert nothing is persisted (no
  half-state). Then run a successful commit and assert both rows land
  atomically. Equivalent test for proposal writes and trial writes.
- **Claim-token stale-submit test:** worker A claims a task; admin reclaim
  regenerates the token; worker A attempts to submit with its old token;
  assert rejection. Worker B then claims with the new token and submits
  successfully.
- **Proposal-pool cap test:** insert proposals (mixed via `plan_request` and
  proactive inserts) past `3 * parallel_trials`; assert later inserts return
  the backpressure signal and do not exceed the cap.

**Integration tests (per milestone):**

For Milestone 1 — in `tests/integration/compose_e2e/`:

1. Spin up Compose stack in CI.
2. Run the setup script against `tests/fixtures/experiment/`.
3. Drive the experiment for N trials (with a deterministic LLM stub planner so
   the test is exact per `feedback_exact_assertions`).
4. Assert: trials count, metrics rows in Postgres, commits on trial branches,
   artifacts present in blob store, events emitted in order.
5. Kill a planner worker mid-task; assert the task stays `claimed` with no
   progress. Invoke admin-reclaim (via API in tests, Web UI manually).
   Verify status returns to `ready`, a fresh worker can claim it, and the
   experiment continues to completion.
6. Contract test: task state-machine transitions — enumerate legal and
   illegal transitions; assert illegal ones are rejected.
7. Contract test: metrics JSONB validation — write metrics that don't conform
   to the experiment's `metrics_schema` and assert rejection; valid metrics
   write through.
8. **Termination test:** configure `max_trials` low; run until reached;
   assert orchestrator transitions experiment to `terminating`, cancels all
   `ready`/`claimed` tasks (status set to `cancelled`), waits for `submitted`
   tasks to integrate, emits `experiment_terminated`, and marks experiment
   `done`. Repeat with `max_wall_time`.
9. **Proposal-cap backpressure test (integration):** drive a run where the
   planner stub attempts to exceed the proposal cap; assert the orchestrator
   rejects overflow and the experiment continues without unbounded growth
   in the `proposals` table.
10. **Eval-manifest size test:** evaluate a trial with a large artifact;
    assert the blob is in storage and the git commit contains only the
    manifest (not the blob payload); repo size bounded.
11. Teardown; confirm state is recoverable from Postgres + blob + Gitea alone.

For Milestone 2:

- Same as above but two concurrent experiments; verify no cross-contamination.
- Kill the orchestrator replica owning experiment A; verify the other replica
  claims the lease and continues.

**Manual smoke test (per milestone):**

- Open Web UI; observe experiment dashboard updating live.
- Claim a planning task as a human; submit a proposal; verify it reaches the
  orchestrator and results in an `implement` task.
- Claim an implementation task as a human; follow the manifest (clone the
  repo locally, work in the editor/IDE of choice, push to the `work/*`
  branch); submit the final SHA; verify the orchestrator integrates it into
  a canonical `trial/*` commit.
- Claim an evaluation task as a human; run evaluation in the chosen
  environment; submit metrics + upload artifacts via the UI form; verify
  the trial row updates and eval artifacts appear in the trial commit.

## Critical Files to Read Before Starting

The new code will be informed by how today's monolith does each thing:

- `src/eden/orchestrator.py` — trial dispatch, proposal claiming, slot workers.
- `src/eden/config.py` + `src/eden/models.py` — current config + data classes.
- `src/eden/db.py` — SQLite schema and manager patterns.
- `src/eden/planner.py` — subprocess planner session (to understand what
  long-lived workers do today).
- `src/eden/execution.py` — implement/evaluate subprocess pattern.
- `src/eden/git_manager.py` — worktree and branch logic (direct port).
- `src/eden/bootstrap.py` — workspace bootstrap (informs setup script).
- `src/eden/docker_runner.py` — Dockerfile rendering (informs setup script).
- `src/eden/web/server.py` — Web UI backend (becomes the shell).
- `tests/fixtures/experiment/.eden/config.yaml` — the reference experiment the
  MVP must match.

## Open Items Deferred to Implementation

- Exact authentication story (defer to Milestone 3 at earliest).
- Lease TTL and retry policy for claimed tasks — post-MVP.
- Whether the evaluator ever needs a `work/*` branch of its own (for
  evaluators that modify code). Not built in MVP; the branch-naming
  convention reserves room for it.
- Whether the control plane also emits events (probably yes: experiment
  registered, experiment terminated).
- Whether Web UI is React SPA + BFF or a monolithic Next.js-style app.
- Observability layer (structured logs to Postgres for now, metrics/traces
  later).
- **Simplification opportunity — single generic worker-host service.** The
  planner and evaluator services share the worker contract and lifecycle;
  only task kinds and outputs differ. They could collapse into one
  `services/worker/` deployment with role-selectable adapters. Keeping them
  separate initially for clarity; revisit during Milestone 1 implementation
  if the duplication is painful. (Implementer stays distinct because of
  sandbox-spawning.)
- Experiment config migration path once config is immutable (post-MVP):
  drop+rebuild typed views, re-validate historical metrics against new
  `metrics_schema`. Only needed when an experiment outlives its schema.
- Configurable inline-threshold for the eval manifest (default 100KB) —
  tuned with experience.
