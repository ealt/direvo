---
name: query-trial-results
description: Query the EDEN results database for trial metrics and history
---

# Query Trial Results

The results database is at `.eden/results.db` (SQLite, **read-only**).

## Schema

Fixed columns: `trial_id`, `commit_sha`, `parent_commits`, `branch`, `status`,
`artifacts_uri`, `description`, `timestamp`.

Metric columns are experiment-specific and appear after the fixed columns.
Use `PRAGMA table_info(trials)` to discover them:

```sql
PRAGMA table_info(trials);
```

## Useful Queries

### All successful trials

```sql
SELECT * FROM trials WHERE status = 'success' ORDER BY trial_id;
```

### Best trial by a metric

```sql
SELECT * FROM trials WHERE status = 'success'
ORDER BY r_squared DESC LIMIT 1;
```

Replace `r_squared` with the relevant metric for the experiment.

### Trial history for a specific commit lineage

```sql
SELECT trial_id, commit_sha, parent_commits, r_squared
FROM trials WHERE status = 'success'
ORDER BY trial_id;
```

### Failed trials (for debugging)

```sql
SELECT trial_id, status, description FROM trials
WHERE status IN ('error', 'eval_error');
```

### Trial count by status

```sql
SELECT status, COUNT(*) FROM trials GROUP BY status;
```

## Interactive Access

```bash
sqlite3 .eden/results.db
```

Or run a single query:

```bash
sqlite3 .eden/results.db "SELECT * FROM trials WHERE status = 'success';"
```

## Notes

- The database is **read-only** -- you cannot insert or modify trial records
- `parent_commits` is a JSON-encoded array (e.g., `["abc123"]`)
- `status` values: `starting`, `success`, `error`, `eval_error`
- Metric column names are not known in advance -- always discover them
  with `PRAGMA table_info(trials)` rather than assuming
