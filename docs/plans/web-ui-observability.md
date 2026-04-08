# Plan: EDEN Web UI for Experiment Observability

## Context

EDEN is a CLI-only orchestration engine. Experiment state lives in SQLite databases (`results.db` ~200KB, `proposals.db` ~100KB), a JSONL session log, per-trial artifact directories, and git history. The user wants a browser-based UI for full observability — both during live runs and post-hoc — with the ability to explore everything the planner can see.

---

## Design Decisions

### D1: Architecture — Thin File Server vs REST API vs Hybrid

The core question: the experiment data lives on the local filesystem. Why should a backend process query and dispatch it?

**Option A: Full REST API backend** (original plan)
- Backend queries SQLite, transforms data, serves JSON endpoints
- Frontend consumes pre-processed JSON
- Pro: Backend can do server-side aggregations; familiar pattern
- Con: Must define and maintain an API surface that mirrors the DB schema. Dynamic metric columns (user-defined per experiment) make this awkward — the backend needs to know the schema to serialize properly. Every new query the frontend wants requires a new endpoint. The "explore everything" requirement means the API surface is effectively unbounded.

**Option B: Thin file server + browser-side SQLite**
- Backend serves raw files over HTTP (DB files, artifacts, log)
- Frontend downloads the SQLite files and queries them in-browser using sql.js (SQLite compiled to WebAssembly)
- Pro: Zero API surface to define or maintain. The frontend has full SQL power — dynamic metric columns just work because the frontend queries the actual database. Perfect for the "explore everything the planner can" requirement. Dramatically less backend code. Works identically for live and post-run mode.
- Con: Frontend must re-download entire DB files to see updates (but at <300KB this is trivial — less than a typical image). sql.js WASM binary is ~1MB (cached after first load).

**Option C: Hybrid — file server + convenience endpoints**
- Serve raw files AND add a few server-side endpoints (e.g., SSE for change notifications)
- Pro: Raw access for exploration + server can signal when files change
- Con: Two paradigms to maintain

**Decision: Option B with a WAL caveat (see D10).** The databases are tiny (<300KB combined). The "explore everything" requirement makes browser-side SQLite compelling — the frontend can run arbitrary SQL, which no REST API can match without essentially reimplementing a SQL proxy. The backend is primarily a file server, not a query engine.

However, `proposals.db` uses WAL journal mode (`db.py:29`), which means uncommitted data may live in the `-wal` sidecar file rather than the main `.db` file. Serving the raw `.db` file to the browser would produce stale or incomplete snapshots. This is addressed in D10 below — the backend serves a checkpointed snapshot of `proposals.db` rather than the raw file, while `results.db` (DELETE journal mode) is served as-is.

For live updates, the frontend polls with HEAD requests to detect file changes (checking `Content-Length` / `Last-Modified`), then re-downloads only when files have changed. At <300KB per download, this is cheaper than most API responses.

### D2: Backend Framework — What to Use for a File Server

Given the decision to serve files rather than build a REST API, the backend's job is simple: serve static files (SPA + experiment data) with proper HTTP caching.

**Option A: `python -m http.server`** (stdlib)
- Pro: Zero dependencies
- Con: No CORS support, no custom routing, synchronous (blocks on large files), no Range request support, no configuration

**Option B: Starlette + uvicorn**
- Pro: Lightweight (~100KB), ASGI (async), built-in `StaticFiles` with `ETag`/`Last-Modified`/Range support, trivial to add one or two custom routes if needed, matches the async style of the orchestrator
- Con: Two new dependencies (starlette, uvicorn)

**Option C: FastAPI + uvicorn**
- Pro: Auto-generated OpenAPI docs, request validation decorators
- Con: Pulls in Pydantic v2 + annotated-types + typing-extensions. The project currently has exactly one runtime dependency (PyYAML). FastAPI's validation/serialization machinery is designed for REST APIs — overkill when the backend is serving files. The auto-docs are useful for APIs with many endpoints; we have essentially zero custom endpoints.

**Option D: Flask + gunicorn**
- Pro: Familiar, large ecosystem
- Con: WSGI (synchronous), needs gunicorn for production, Flask-CORS for CORS. Heavier than Starlette for what's needed.

**Option E: No Python server — use Vite's built-in server**
- Pro: Zero Python deps, Vite already has a dev server with proxy support
- Con: Vite is a build tool, not a production server. `vite preview` serves static files but doesn't support custom CORS, doesn't serve non-SPA files from arbitrary paths. Would need a separate production server anyway.

**Decision: Starlette + uvicorn.** Minimal dependency footprint, async, built-in StaticFiles with proper HTTP caching (ETag, Last-Modified, Range requests). Two new deps vs. zero custom endpoints — the deps earn their keep by providing correct HTTP semantics that `http.server` lacks.

### D3: Browser-Side SQLite — sql.js vs Alternatives

**Option A: sql.js** (Emscripten SQLite → WASM)
- Maturity: 10+ years, widely used, actively maintained
- Size: ~1MB WASM binary (cached by browser after first load)
- API: Synchronous, mirrors SQLite C API. `new SQL.Database(arrayBuffer)` → `.exec(sql)`
- Read-only workflow: fetch .db file as ArrayBuffer → instantiate → query → discard on re-fetch
- Pro: Battle-tested, extensive docs, TypeScript types available
- Con: Must load entire DB into memory (irrelevant at <300KB)

**Option B: Official SQLite WASM (sqlite3.wasm)**
- Released 2023 by the SQLite team
- Pro: From the source, potentially better performance/compatibility
- Con: Newer, less ecosystem, documentation is sparse, primary use case is Origin Private File System (persistent browser storage) rather than loading external files

**Option C: wa-sqlite**
- Alternative WASM build with virtual filesystem support
- Pro: Can do incremental reads via virtual FS
- Con: Less mature than sql.js, smaller community, incremental reads are unnecessary when the whole DB fits in memory

**Option D: Don't use browser SQLite — backend queries via REST**
- Pro: No WASM dependency in browser
- Con: Loses the "explore everything" capability, need to define API endpoints for every query

**Decision: sql.js.** Most mature, best documented, TypeScript support. The DBs are small enough that loading them entirely into memory is trivial. The alternatives either solve problems we don't have (persistence, large DBs) or aren't as battle-tested.

### D4: Live Update Strategy — How the Frontend Detects Changes

During a live experiment, files change as trials complete. The frontend needs to know when to re-fetch.

**Option A: Polling with HEAD requests**
- Frontend sends HEAD requests to DB/log file URLs every N seconds
- Compares `Content-Length` or `Last-Modified` header to last known value
- Re-fetches file only when changed
- Pro: Pure HTTP, no backend logic needed, works with any file server
- Con: Polling interval is a latency floor (2s poll = up to 2s stale). HEAD requests are cheap but still N requests per interval.

**Option B: Server-Sent Events (SSE) for change notification**
- Backend watches file mtimes, sends "file_changed" events
- Frontend re-fetches the specific changed file
- Pro: Instant notification, no wasted requests
- Con: Needs a backend endpoint (~20 lines), needs file watching logic

**Option C: Full SSE with data payloads**
- Backend reads DB changes and streams them as events (original plan)
- Pro: Minimal bandwidth, real-time
- Con: Backend must understand the data model, reintroduces the REST API problem

**Option D: Simple periodic re-fetch**
- Frontend re-downloads DB files every N seconds unconditionally
- Pro: Simplest possible implementation
- Con: Wasteful when nothing changed (though at <300KB, "wasteful" is relative — this is less data than a single webpage)

**Decision: Option A (HEAD polling) to start.** It requires zero backend logic — Starlette's StaticFiles already supports HEAD with correct Last-Modified headers. The poll interval (2-3 seconds) is fast enough for a dashboard monitoring trials that take minutes each. Can upgrade to SSE later if the latency floor matters, but it's unlikely to for this use case.

For the session log specifically: use HTTP Range requests (`Range: bytes={lastOffset}-`) to fetch only new bytes since the last read. The log is JSONL, so partial fetches align cleanly to line boundaries. This is more efficient than re-downloading the full log, which grows unboundedly.

### D5: Charting Library

The metrics dashboard needs line charts (metric values over trials), possibly with a running-best overlay.

**Option A: recharts** (~100KB gzip)
- React-native component API (`<LineChart>`, `<XAxis>`, etc.)
- Pro: Declarative, good React integration, handles common chart types well, active maintenance, good docs
- Con: Limited customization for unusual chart types, SVG-based so can slow with thousands of points (not an issue at ~200 trials)

**Option B: Chart.js + react-chartjs-2** (~60KB gzip)
- Canvas-based, React wrapper is thin
- Pro: Smaller bundle, good canvas performance, wide adoption
- Con: Imperative API under the wrapper (configure via options objects, not components). Less React-idiomatic. Wrapper can lag behind Chart.js releases.

**Option C: Plotly.js + react-plotly.js** (~1MB gzip)
- Full scientific plotting library
- Pro: Most powerful, built-in zoom/pan/export, 3D charts, great for data science
- Con: Enormous bundle (3-4x everything else combined). Loading time visibly impacts UX. Overkill for line charts over ~200 points.

**Option D: Observable Plot** (~50KB gzip)
- From the D3 team, designed for exploratory data analysis
- Pro: Concise API, small bundle, designed for exactly this kind of data exploration
- Con: Not React components — returns DOM elements, needs a ref-based wrapper. Relatively new (2021), smaller ecosystem. Less example code.

**Option E: uPlot** (~30KB gzip)
- Canvas-based, extremely fast and small
- Pro: Smallest bundle by far, best performance
- Con: Minimal styling options, imperative API, sparse documentation. Designed for time-series with thousands of points — overkill for ~200.

**Option F: D3 directly** (~80KB selective import)
- Maximum control, use only the modules you need
- Pro: Complete control over every pixel, no abstractions
- Con: Extremely verbose for basic charts (50+ lines for a line chart vs. 10 with recharts). Not React-native — manual DOM management in useEffect.

**Decision: recharts.** The data volume is small (~200 points), the chart types are standard (line charts, bar charts), and recharts' component API is the most natural fit for a React app. It's the pragmatic middle ground — not the smallest (that's uPlot) or most powerful (that's Plotly), but the one that requires the least code for standard dashboard charts.

### D6: Markdown Rendering

Artifacts include `.md` files (plan.md, notes.md) that need rendering.

**Option A: react-markdown + remark-gfm**
- Pro: React-native (renders to React elements, not innerHTML). Extensible via remark/rehype plugins. GFM support (tables, strikethrough, task lists) via plugin. Active ecosystem.
- Con: Can be slow for very large documents (>100KB). Pulls in unified/remark/rehype ecosystem (~15 transitive packages, but small).

**Option B: marked + DOMPurify + dangerouslySetInnerHTML**
- Pro: Very fast, tiny (~8KB for marked)
- Con: XSS risk requires DOMPurify. Not React-idiomatic (raw HTML injection). Can't use React components inside rendered markdown.

**Option C: markdown-it + React wrapper**
- Pro: Fast, pluggable, good CommonMark compliance
- Con: Same innerHTML pattern as Option B. No React-native rendering.

**Decision: react-markdown + remark-gfm.** The artifacts are small documents (<10KB typically). react-markdown is the standard React solution — renders to React elements (no XSS concerns), extensible, and GFM tables are likely present in trial artifacts.

### D7: CSS Strategy

**Option A: CSS Modules**
- Built into Vite, scoped class names, zero runtime cost
- Pro: Simple, no dependencies, good Vite integration
- Con: No utility classes, no design system, naming still manual

**Option B: Tailwind CSS**
- Utility-first, great for rapid prototyping
- Pro: Fast development velocity, consistent spacing/colors, tiny production CSS (tree-shaken)
- Con: Adds PostCSS + Tailwind deps, verbose class names, visual noise in JSX

**Option C: Vanilla CSS with custom properties**
- Pro: Zero dependencies, full control
- Con: No scoping (class name conflicts), harder to maintain

**Option D: CSS-in-JS (styled-components, emotion)**
- Pro: Dynamic styles, co-located with components
- Con: Runtime cost, going out of fashion, adds dependencies

**Decision: CSS Modules.** Zero dependencies, built into Vite, scoped styles. For a developer tool dashboard, we don't need a design system — clean defaults with CSS custom properties for theming (light/dark) are sufficient. Tailwind would also work but adds a build-time dependency for marginal benefit in a tool like this.

### D8: Project Layout

**Option A: Frontend in `packages/web-ui/`, backend in `src/eden/web/`**
- Follows the existing monorepo pattern (`packages/planner-kit-ts/` already exists)
- Frontend is a standalone npm package; backend is part of the eden Python package
- Pro: Consistent with existing layout, clean separation
- Con: Two package managers (uv for Python, npm for frontend)

**Option B: Everything in `src/eden/web/`**
- Frontend code lives inside the Python package tree
- Con: Mixing npm and Python packaging in one directory is messy. `node_modules` inside `src/` is wrong.

**Option C: Standalone repo / separate package**
- Con: Overkill, complicates development, no precedent in this project

**Decision: Option A.** `packages/web-ui/` for the React frontend (following the planner-kit-ts precedent), `src/eden/web/` for the Python file server. The two-package-manager reality already exists in this repo.

### D9: Git Tree Visualization

The user wants to explore the git DAG. Options for rendering it:

**Option A: Custom SVG from trial data**
- Parse `parent_commits` and `commit_sha` from `results.db` to reconstruct the DAG
- Use topological sort for layout, render as SVG with React
- Pro: No backend git commands, works in post-run mode, data already available
- Con: Only shows trial commits (not intermediate git state), layout algorithm is custom code

**Option B: Run git commands via backend API**
- Backend executes `git log --graph`, `git branch -a`, etc.
- Pro: Full git history, not just trials
- Con: Requires git binary and workspace access. Doesn't work in post-run mode (workspace may not be present). Adds backend endpoints.

**Option C: Use gitgraph.js library**
- Dedicated git graph visualization library
- Pro: Purpose-built, handles branch layout
- Con: Designed for authored/scripted graphs (documentation, tutorials), not for loading from data. Imperative API.

**Decision: Option A (custom SVG from trial data) for the initial implementation.** The trial table already contains all the DAG information (parent_commits, commit_sha, branch). A simple topological layout renders this as an SVG. This works in both live and post-run mode since it only needs the database. Option B can be added later for users who want full git history exploration.

### D10: WAL-Mode proposals.db — How the Browser Gets Consistent Data

`results.db` uses DELETE journal mode (`db.py:28`) — the single `.db` file always contains the full state. The browser can fetch and query it with sql.js directly.

`proposals.db` uses WAL journal mode (`db.py:29`). In WAL mode, recent writes may live in `proposals.db-wal` rather than the main file. A browser fetching only `proposals.db` would see stale or incomplete data. The export script (`src/eden/docker/export.sh:27-29`) already copies all three files (`.db`, `-wal`, `-shm`), but sql.js does not support WAL mode — it can only load a single monolithic database file.

**Option A: Backend serves a checkpointed snapshot**
- The `/experiment/data/proposals.db` route opens the database server-side with Python's `sqlite3` (which reads through WAL correctly), then uses the `.backup()` API to create an in-memory copy and serves that as bytes.
- Pro: Browser gets a complete, consistent snapshot as a single file. sql.js works normally. Adds ~15 lines of backend code.
- Con: Backend is no longer purely static file serving for this one file. The backup operation is fast (<1ms for <100KB) but happens on every fetch.

**Option B: Backend runs `PRAGMA wal_checkpoint(TRUNCATE)` before serving**
- Forces WAL content back into the main file, then serves the raw file.
- Pro: Serves the actual file.
- Con: Requires write access to the database (checkpoint modifies the WAL). The web server should be strictly read-only to avoid interfering with the orchestrator/planner.

**Option C: Serve all three files (`.db`, `-wal`, `-shm`) and reconstruct in browser**
- Pro: Pure file serving.
- Con: sql.js has no WAL support. Would need a custom WASM build or manual WAL replay — impractical.

**Option D: Backend queries proposals server-side, returns JSON**
- A single REST endpoint (`/api/proposals`) that returns all proposal rows as JSON.
- Pro: Simple, no WASM complications.
- Con: Loses the sql.js/SQL Console consistency — proposals can't be queried in the browser SQL console alongside trials. Creates an asymmetry between the two databases.

**Decision: Option A (backup snapshot).** The backend opens `proposals.db` read-only, uses `conn.backup(mem_conn)` to create an in-memory checkpoint, and serves the bytes. This is fast, read-only, and produces a standard SQLite file that sql.js can load. The SQL Console works identically for both databases. This adds ~15 lines to the backend — modest complexity for full consistency.

For post-run mode: if the experiment was exported with all three WAL files present (`export.sh:27-29`), the same backup approach works. If only the main `.db` file exists (WAL was checkpointed before export or the experiment used the default export), it's served directly.

### D11: Liveness Detection — How the UI Knows If an Experiment Is Running

The `/experiment/info` endpoint reports a `status` field so the frontend can display a liveness indicator. There is no existing PID file or liveness marker in the orchestrator.

**Option A: Check for `session_ended` event in session.log**
- Parse the last few lines of `session.log`. If a `session_ended` event exists, the session is complete. Otherwise, check if the file was modified recently — if so, likely live. If the log is stale and has no `session_ended`, the session crashed or was killed.
- Pro: No new runtime contract. Uses existing data.
- Con: Heuristic — a recently-modified log without `session_ended` could be a crashed session, not a live one. The recency threshold requires tuning.

**Option B: PID file written by the orchestrator**
- The orchestrator writes its PID to `.eden/orchestrator.pid` at startup, removes it on clean shutdown.
- Pro: Definitive liveness signal (check if PID is still running).
- Con: Requires modifying the orchestrator. Stale PID files after crashes.

**Option C: Default to static, let the user override**
- The UI starts in static/archive mode. If data changes while viewing, it automatically switches to live polling.
- Pro: No liveness detection needed. Purely reactive.
- Con: On first load of a live experiment, the UI appears static until the first change occurs (could be minutes between trials).

**Decision: Option A (log-based heuristic) for the initial implementation.** Parse the tail of `session.log` for a `session_ended` event. If present, report `"status": "ended"`. If absent and the file was modified within the last 5 minutes, report `"status": "live"`. If absent and stale beyond 5 minutes, report `"status": "unknown"` (may be crashed or may be in a long trial).

The 5-minute window (rather than 30 seconds) accounts for trials that take several minutes. This is conservative — `unknown` means "we don't know" rather than "it's dead."

**Critical: the `status` field affects only the UI indicator (badge/label), not the polling rate.** The frontend always polls at the same interval (3 seconds) regardless of status. This avoids the failure mode where a `live: false` flag reduces polling and the UI goes stale during a long trial. The status is purely informational — a visual hint for the user, not a control flow decision.

Can upgrade to a PID file later if the heuristic proves insufficient.

### D12: Scope Boundary — What "Explore Everything" Means

The EDEN architecture distinguishes experiment root, planner root, and workspace root as separate trust/ownership scopes (`AGENTS.md:25-39`). The UI needs a concrete scope definition.

**What the planner can access:**
- `results.db` (read-only)
- `proposals.db` (read-write, but UI is read-only)
- Artifact files under `.eden/artifacts/`
- Proposal docs under planner's `.eden/proposals/`
- Git history (read-only: `git log`, `git show`, `git diff`)
- Config file (`.eden/config.yaml`)
- Session log (`.eden/session.log`)

**What the UI exposes:**
The UI exposes the same read surface as the planner, minus write access and minus the workspace working tree. Specifically:

| Data | Exposed | Method |
|------|---------|--------|
| `results.db` | Yes | sql.js (raw file) |
| `proposals.db` | Yes | sql.js (backup snapshot, see D10) |
| Trial artifacts (`.eden/artifacts/trial-{id}/*`) | Yes | HTTP file serving |
| Proposal docs (`.eden/proposals/{slug}/*`) | Yes | HTTP file serving |
| Config (`.eden/config.yaml`) | Yes, parsed subset | `/experiment/info` endpoint |
| Session log (`.eden/session.log`) | Yes | HTTP Range requests |
| Git history | Derived from `results.db` trial data (D9) | sql.js queries on parent_commits/commit_sha |
| Workspace working tree | No | Not served — contains code, potentially large |
| Planner working directory | No | Not served |

This matches the planner's read surface for experiment observability without exposing the full filesystem.

### D13: Export Contract — What Files Exist in Post-Run Mode

The plan supports two modes via CLI:
- `eden ui --config .eden/config.yaml` (live: reads from experiment root)
- `eden ui --experiment-dir /path/to/exported/` (post-run: reads from export directory)

The current export script (`src/eden/docker/export.sh`) produces:

```
exported-dir/
├── .eden/
│   ├── results.db           # Trial results (always present)
│   ├── session.log          # Event log (always present)
│   ├── config.yaml          # Config (always present)
│   └── artifacts/           # Per-trial docs (always present if trials ran)
│       └── trial-{id}/
│           ├── plan.md
│           ├── notes.md
│           └── eval_report.json
├── planner/
│   └── .eden/
│       ├── proposals.db     # Proposals (present if planner ran)
│       ├── proposals.db-wal # WAL sidecar (may be present)
│       ├── proposals.db-shm # SHM sidecar (may be present)
│       └── proposals/       # Proposal docs
│           └── {slug}/
│               └── plan.md
└── workspace.bundle         # Git bundle (present if git repo existed)
```

**Path resolution for `--experiment-dir`:**
- `results.db`: `{dir}/.eden/results.db`
- `proposals.db`: `{dir}/planner/.eden/proposals.db` (with WAL sidecars if present)
- `session.log`: `{dir}/.eden/session.log`
- `artifacts`: `{dir}/.eden/artifacts/`
- `proposals docs`: `{dir}/planner/.eden/proposals/`
- `config.yaml`: `{dir}/.eden/config.yaml`

**Graceful degradation:** If `proposals.db` is missing (experiment had no planner), the Proposals tab shows "No proposal data available." If `session.log` is missing, the log viewer is unavailable. The UI requires only `results.db` and `config.yaml` as mandatory.

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────┐
│  Browser                                            │
│                                                     │
│  React SPA (packages/web-ui/)                       │
│    ├── sql.js (WASM) ── queries results.db          │
│    │                     queries proposals.db        │
│    ├── fetch ─── reads artifacts (md, json)          │
│    ├── fetch + Range ─── tails session.log           │
│    └── HEAD poll ─── detects file changes            │
│                                                     │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP
┌──────────────────────┴──────────────────────────────┐
│  Python File Server (src/eden/web/)                 │
│  Starlette + uvicorn                                │
│                                                     │
│  Routes:                                            │
│    /                  → SPA (index.html)             │
│    /assets/*          → SPA static assets            │
│    /experiment/data/* → experiment files             │
│    │  results.db       (static file, DELETE mode)    │
│    │  proposals.db     (backup snapshot, WAL-safe)   │
│    │  session.log, config.yaml                       │
│    │  artifacts/trial-{id}/*                         │
│    │  proposals/{slug}/*                             │
│    /experiment/info   → JSON metadata                │
│    │  { files, metrics_schema, objective, status, ..}│
│                                                     │
│  Standard HTTP: ETag, Last-Modified, Range, CORS    │
└─────────────────────────────────────────────────────┘
```

The backend has two custom endpoints:
- `/experiment/info` — returns config metadata, file paths, and liveness status
- `/experiment/data/proposals.db` — serves a WAL-checkpointed snapshot (see D10)

Everything else (`results.db`, `session.log`, artifacts, proposal docs) is standard static file serving via Starlette's `StaticFiles`.

## Backend — `src/eden/web/`

### Files

| File | Purpose | Approximate size |
|------|---------|-----------------|
| `__init__.py` | Package init | ~5 lines |
| `server.py` | Starlette app, `/experiment/info`, `/experiment/data/proposals.db` snapshot route | ~120 lines |

Starlette's `StaticFiles` handles serving `results.db`, `session.log`, artifacts, and proposal docs. The two custom routes handle the info endpoint and the WAL-safe proposals.db snapshot. The `eden ui` CLI integration adds ~20 lines to `src/eden/cli.py`.

### `/experiment/info` endpoint

Returns the metadata the frontend needs to bootstrap. The endpoint probes the filesystem and reports what's available:

```json
{
  "metrics_schema": {"r_squared": "real", "rmse": "real"},
  "objective": {"expr": "r_squared", "direction": "maximize"},
  "parallel_trials": 4,
  "status": "live",
  "files": {
    "results_db": {"path": "/experiment/data/results.db", "available": true},
    "proposals_db": {"path": "/experiment/data/proposals.db", "available": true},
    "session_log": {"path": "/experiment/data/session.log", "available": true},
    "artifacts_dir": {"path": "/experiment/data/artifacts", "available": true},
    "proposals_dir": {"path": "/experiment/data/proposals", "available": true}
  }
}
```

- `status`: `"live"` / `"ended"` / `"unknown"` per D11 heuristic. Affects UI indicator only, not polling rate.
- `available`: `true`/`false` — the backend checks file/directory existence. The frontend uses this to enable/disable UI sections rather than relying on 404 probing.

### CLI integration

Add `ui` subcommand to `src/eden/cli.py:build_parser()`:

```
eden ui --config .eden/config.yaml [--port 8741] [--no-open] [--dev]
eden ui --experiment-dir /path/to/exported/ [--port 8741]
```

### SPA build and packaging

The built frontend must be locatable at runtime by `eden ui`. Three modes:

1. **Development** (`--dev` flag): Vite dev server handles SPA. Python backend skips static file serving, enables CORS for `localhost:5173`. Vite proxy config forwards `/experiment/*` to the backend.

2. **From source (no `--dev`)**: The backend looks for `packages/web-ui/dist/` relative to the project root. The user runs `cd packages/web-ui && npm run build` before `eden ui`. If `dist/` is missing, `eden ui` prints an error with the build command.

3. **Installed package**: For pip-installed distributions, a build step copies `packages/web-ui/dist/` to `src/eden/web/static/`. The `pyproject.toml` package-data entry is extended:
   ```toml
   eden = ["sql/*.sql", "docker/*.sh", "templates/**/*", ..., "web/static/**/*"]
   ```
   At runtime, `server.py` checks `importlib.resources` for `eden.web.static` first, falls back to the source-tree `packages/web-ui/dist/` path.

This is the same pattern used by many Python projects with bundled frontends (e.g., Jupyter, Gradio).

### proposals.db snapshot cache semantics

The `/experiment/data/proposals.db` route is dynamic (produces a backup snapshot), not a static file. Standard `StaticFiles` caching doesn't apply. The route must provide its own cache headers:

- **`ETag`**: Compute from `max(db_mtime_ns, wal_mtime_ns)` where `wal_mtime_ns` is from `proposals.db-wal` if it exists, else 0. Under WAL mode, writes update the `-wal` file's mtime, not the main `.db` file's mtime, so the WAL stat is the primary change signal. Using `max()` of both covers all cases (WAL present, WAL checkpointed, non-WAL mode).
- **`Last-Modified`**: Derived from the same `max()` mtime.
- **`Cache-Control: no-cache`**: Forces revalidation on every request, but allows conditional GET (client sends `If-None-Match` with the ETag).

The frontend's HEAD polling sends `If-None-Match`. If the ETag matches, the backend returns 304 (no backup needed). If it doesn't match, the backend runs `conn.backup()` and returns the fresh snapshot. This avoids unnecessary backup operations while ensuring the frontend sees changes promptly.

## Frontend — `packages/web-ui/`

### Structure

```
packages/web-ui/
  package.json
  tsconfig.json
  vite.config.ts
  index.html
  src/
    main.tsx
    App.tsx
    db/
      sqlite.ts           # sql.js wrapper: load DB, run queries, re-fetch
      queries.ts          # Typed query functions (listTrials, bestTrial, etc.)
    api/
      files.ts            # Fetch artifacts, tail log via Range requests
      poll.ts             # HEAD-based change detection
    types/
      index.ts            # TypeScript types for trials, proposals, config
    hooks/
      useExperimentData.ts  # Orchestrates DB loading, polling, state
      useSqlite.ts          # sql.js lifecycle (load WASM, open DB)
    components/
      Layout.tsx
      MetricsDashboard/
        MetricsDashboard.tsx
        MetricChart.tsx
        BestTrialCard.tsx
        ConvergenceChart.tsx
      TrialTimeline/
        TrialTimeline.tsx
        SlotLane.tsx
        TrialCard.tsx
      ArtifactViewer/
        ArtifactViewer.tsx
        MarkdownView.tsx
        JsonView.tsx
      ProposalQueue/
        ProposalQueue.tsx
        ProposalRow.tsx
      Explorer/
        Explorer.tsx
        SqlConsole.tsx       # Run arbitrary SQL against loaded DBs
        DatabaseTable.tsx
        GitTree.tsx
        LogViewer.tsx
    styles/
      global.css
      variables.css
```

### Data flow

```
useSqlite()                    useExperimentData()
  │ load sql.js WASM              │ fetch /experiment/info
  │ fetch results.db (static)     │ poll HEAD for changes
  │ fetch proposals.db (snapshot) │ fetch log via Range
  │ → SQL.Database instances      │ → re-fetch DBs when changed
  │                               │
  └───────────┬───────────────────┘
              │
     db/queries.ts
       listTrials(db) → Trial[]
       listProposals(db) → Proposal[]
       bestTrial(db, objective) → Trial | null
       metricSeries(db, schema) → MetricSeries[]
              │
              ▼
         Component tree
```

Key: the queries in `db/queries.ts` are plain SQL executed against the in-memory sql.js databases. They can use the exact same queries the planner uses — no translation layer.

### SQL Console (Explorer view)

Because the frontend has full sql.js databases loaded, the Explorer can include a SQL console where the user types arbitrary queries against either database. This is the "explore everything the planner can" feature — same databases, same SQL.

### Session log tailing

```typescript
// files.ts — log tailing with carryover buffer for partial lines
let carryover = '';

async function fetchLogSince(offset: number): Promise<{lines: LogEvent[], newOffset: number}> {
  const response = await fetch('/experiment/data/session.log', {
    headers: { 'Range': `bytes=${offset}-` }
  });

  // 416 = range not satisfiable (offset past end of file), 404 = no log yet
  if (response.status === 416 || response.status === 404) {
    return { lines: [], newOffset: offset };
  }

  const text = await response.text();
  const totalBytes = new TextEncoder().encode(text).length;

  // 200 = server ignored Range and sent the whole file.
  // Reset offset to the full body length, not offset + consumed.
  const isFullFile = response.status === 200;

  const combined = carryover + text;
  const rawLines = combined.split('\n');

  // Last element may be a partial line (no trailing newline) — carry it over
  carryover = rawLines.pop() ?? '';

  const lines: LogEvent[] = [];
  for (const line of rawLines) {
    if (!line.trim()) continue;
    try { lines.push(JSON.parse(line)); }
    catch { /* skip malformed lines */ }
  }

  const carryoverBytes = new TextEncoder().encode(carryover).length;
  const newOffset = isFullFile
    ? totalBytes - carryoverBytes           // absolute position in file
    : offset + totalBytes - carryoverBytes; // relative to prior offset
  return { lines, newOffset };
}
```

This handles: partial trailing lines (carried over to next fetch), empty responses, non-206 status codes, and malformed JSON lines.

### Key component behaviors

**MetricsDashboard**: One recharts `<LineChart>` per metric from `metrics_schema`. X-axis = trial_id, Y-axis = metric value. Convergence chart shows running-best of the objective. BestTrialCard highlights the current best. Click a point to navigate to that trial's artifacts.

**TrialTimeline**: Horizontal lane per slot. Trial cards colored by status. Slot assignment parsed from session log events (`slot` field in `trial_started`). Live trials animate. **Fallback when session.log is unavailable** (D13 allows missing logs): the timeline degrades to a flat chronological list of trials (no slot lanes) since slot assignments are only available in the log. A banner indicates "Slot assignments unavailable — session log not found."

**ArtifactViewer**: Left panel lists trials (filterable). Right panel renders artifacts in tabs — `.md` via react-markdown, `.json` pretty-printed, others as preformatted text.

**ProposalQueue**: Sortable/filterable table of proposals with status badges.

**Explorer**: Tabbed — SQL Console (arbitrary queries), raw Trials/Proposals tables, Session Log viewer (filterable by event type), Git Tree SVG.

## Dependencies

### Python (optional `web` group in pyproject.toml)

```toml
[project.optional-dependencies]
web = [
  "starlette>=0.40",
  "uvicorn[standard]>=0.30",
]
```

### Frontend (packages/web-ui/package.json)

```json
{
  "dependencies": {
    "react": "^18",
    "react-dom": "^18",
    "react-router-dom": "^6",
    "sql.js": "^1.10",
    "recharts": "^2",
    "react-markdown": "^9",
    "remark-gfm": "^4"
  },
  "devDependencies": {
    "vite": "^5",
    "typescript": "^5.5",
    "@types/react": "^18",
    "@types/react-dom": "^18",
    "@vitejs/plugin-react": "^4",
    "vitest": "^2",
    "@testing-library/react": "^16"
  }
}
```

## Implementation Phases

### Phase 1: Backend file server
- `src/eden/web/__init__.py`, `server.py` (~80 lines total)
- `eden ui` subcommand in `src/eden/cli.py` (~20 lines)
- `pyproject.toml` optional deps
- `tests/test_web_server.py`

### Phase 2: Frontend scaffold + data layer
- `packages/web-ui/` project setup (Vite + React + TypeScript)
- `db/sqlite.ts`, `db/queries.ts` — sql.js integration
- `api/files.ts`, `api/poll.ts` — file fetching, change detection
- `hooks/useExperimentData.ts` — orchestrates everything
- `Layout.tsx` with tab navigation
- Vite proxy config for dev mode

### Phase 3: Core views
- MetricsDashboard (recharts)
- TrialTimeline (slot lanes)
- ProposalQueue (table)
- ArtifactViewer (react-markdown)

### Phase 4: Explorer
- SQL Console (textarea → sql.js → results table)
- Raw database table views
- Session log viewer
- Git tree SVG

## Files to modify

| File | Change |
|------|--------|
| `src/eden/cli.py` | Add `ui` subcommand (~20 lines) |
| `pyproject.toml` | Add `web` optional dependency group |

## Files to create

| File | Purpose |
|------|---------|
| `src/eden/web/__init__.py` | Package init |
| `src/eden/web/server.py` | Starlette app + `/experiment/info` endpoint |
| `packages/web-ui/` | Entire React frontend |
| `tests/test_web_server.py` | Backend tests |

## Files to reference

| File | What to reuse |
|------|---------------|
| `src/eden/config.py` | `load_config()` for `--config` flag |
| `src/eden/models.py` | Enum values and field names for TypeScript types |
| `src/eden/sql/results.sql`, `proposals.sql` | Schema that sql.js will query |
| `src/eden/logging.py:18-35` | JSON log format the frontend must parse |
| `src/eden/summary.py` | Metric formatting conventions to match |

## Verification

### Automated tests

1. `uv run -m pytest tests/ -q` — existing tests pass
2. `uv run ruff check . && uv run pyright` — lint and type check clean
3. `cd packages/web-ui && npm test` — Vitest for frontend

### Backend tests (`tests/test_web_server.py`)

Using Starlette `TestClient`:

- **Static file serving**: GET `results.db`, `session.log`, artifact files → 200 with correct content
- **WAL snapshot route**: Create a WAL-mode proposals.db, write rows, GET `/experiment/data/proposals.db` → response is a valid monolithic SQLite file containing all rows (including those only in WAL)
- **Snapshot conditional GET**: Two sequential GETs with same ETag → second returns 304. Write a new row → GET with old ETag → 200 with updated data
- **Info endpoint**: GET `/experiment/info` → correct metrics_schema, objective, file availability flags
- **Export mode path resolution**: Point server at a directory matching the export layout (D13) → all paths resolve correctly
- **Graceful degradation**: Remove proposals.db from fixture → `/experiment/info` reports `available: false`, GET proposals.db → 404
- **Liveness heuristic**: Session log with `session_ended` → `status: "ended"`. Recent log without `session_ended` → `status: "live"`. Stale log without `session_ended` → `status: "unknown"`. Missing log → `status: "unknown"`
- **Range requests on session.log**: GET with `Range: bytes=100-` → 206 with partial content

### Frontend tests (Vitest)

- sql.js integration: load a fixture DB, run typed queries, verify results
- Log tailing: partial line carryover, empty response, malformed JSON
- Poll hook: mock HEAD responses, verify re-fetch triggers on changed ETag

### Manual verification

- `uv run eden ui --config tests/fixtures/experiment/.eden/config.yaml` — browser opens, can query trial data via SQL console
- Point at an exported experiment directory → verify all views work with available data, disabled views show appropriate messages for missing data
