---
name: query-proposals
description: Query the EDEN proposals database for proposal status and history
---

# Query Proposals

The proposals database is at `.eden/proposals.db` (SQLite, read/write).

## Schema

```sql
CREATE TABLE proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    priority REAL NOT NULL,
    slug TEXT NOT NULL,
    parent_commits TEXT NOT NULL,  -- JSON array of commit SHAs
    artifacts_uri TEXT NOT NULL,   -- path to proposal directory
    status TEXT NOT NULL,
    created_at TEXT NOT NULL       -- ISO 8601 UTC
);
```

## Status Lifecycle

```
drafting --> ready --> dispatched --> completed
                ^                       |
                |    (on impl failure)  |
                +-----------------------+
```

- **drafting** -- proposal docs being written (not yet visible to orchestrator)
- **ready** -- available for the orchestrator to claim
- **dispatched** -- orchestrator has claimed it and is running a trial
- **completed** -- trial finished (success or evaluation failure)

On implementation failure, the orchestrator returns the proposal to `ready`
with lowered priority for retry.

## Useful Queries

### All proposals with status

```sql
SELECT id, slug, priority, status, created_at
FROM proposals ORDER BY created_at;
```

### Pending proposals (not yet dispatched)

```sql
SELECT * FROM proposals WHERE status = 'ready'
ORDER BY priority DESC;
```

### Currently running

```sql
SELECT * FROM proposals WHERE status = 'dispatched';
```

### Completed proposals

```sql
SELECT * FROM proposals WHERE status = 'completed'
ORDER BY created_at;
```

### Proposal by slug

```sql
SELECT * FROM proposals WHERE slug = 'your-slug-here';
```

## Reading Proposal Documents

Each proposal's plan file is stored under the path in its `artifacts_uri`
column. Use that path directly rather than reconstructing from the slug,
since `artifacts_uri` may be an absolute path:

```bash
# Read artifacts_uri from the database, then:
cat "${artifacts_uri}/plan.md"
```

## Interactive Access

```bash
sqlite3 .eden/proposals.db
```

## Notes

- The orchestrator claims proposals using `BEGIN IMMEDIATE` transactions --
  concurrent writes are safe but may block briefly
- `parent_commits` is a JSON array (e.g., `["abc123"]`)
- Priority is a float -- higher values are dispatched first
