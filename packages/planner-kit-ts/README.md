# @direvo/planner-kit

TypeScript SDK for writing [EDEN](../../README.md) planner scripts.

## Install

```bash
npm install @direvo/planner-kit
```

## Usage

```typescript
import {
  getTrial,
  getAllTrials,
  createProposal,
  readTrialArtifact,
  iterTrialNotifications,
  configureLogging,
  logEvent,
  getHeadSha,
} from "@direvo/planner-kit";

// Read trial results
const trials = getAllTrials(".eden/results.db", "score DESC");
const best = trials[0];

// Read artifacts from a trial
const plan = readTrialArtifact(".eden/artifacts", best.trial_id, "plan.md");

// Create a new proposal
createProposal({
  proposalsDb: ".eden/proposals.db",
  proposalsDir: ".eden/proposals",
  priority: 2.0,
  slug: "improved-strategy",
  parentCommits: [best.commit_sha!],
  planText: "Try a different approach...",
});

// Structured logging
const logger = configureLogging();
logEvent(logger, { event: "propose", slug: "improved-strategy" });

// Listen for trial completions
for await (const trialId of iterTrialNotifications()) {
  const trial = getTrial(".eden/results.db", trialId);
  // ... react to completion
}
```

## API

### Database

- `connectResultsDb(path)` — open results DB read-only
- `connectProposalsDb(path)` — open proposals DB read-write (WAL mode)
- `getTrial(resultsDb, trialId)` — fetch a successful trial by ID
- `getAllTrials(resultsDb, orderBy?)` — fetch all successful trials

### Proposals

- `createProposal(options)` — write plan.md + insert DB row

### Artifacts

- `readTrialArtifact(artifactsDir, trialId, filename)` — read a text artifact

### Notifications

- `iterTrialNotifications()` — async generator parsing stdin for trial IDs

### Logging

- `configureLogging()` — set up JSON-line logger (reads `EDEN_LOG_DIR`)
- `logEvent(logger, fields)` — emit a structured log line

### Git

- `getHeadSha(workspace?)` — get HEAD commit SHA

## Protocol

This SDK speaks the same SQLite protocol as the Python `eden.planner_kit`.
The schemas in `sql/proposals.sql` and `sql/results.sql` are the contract.
Both SDKs produce and consume identical row formats.

## License

[MIT](../../LICENSE)
