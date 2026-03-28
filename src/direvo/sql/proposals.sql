CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    priority REAL NOT NULL,
    slug TEXT NOT NULL,
    parent_commits TEXT NOT NULL,
    artifacts_uri TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (status IN ('drafting', 'ready', 'dispatched', 'completed'))
);
