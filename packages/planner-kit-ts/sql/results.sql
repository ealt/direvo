CREATE TABLE IF NOT EXISTS trials (
    trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha TEXT,
    parent_commits TEXT,
    branch TEXT,
    status TEXT NOT NULL,
    artifacts_uri TEXT,
    description TEXT,
    timestamp TEXT NOT NULL,
-- METRIC_COLUMNS
    CHECK (status IN ('starting', 'success', 'error', 'eval_error'))
);
