/**
 * SQLite database connections and trial queries.
 *
 * Mirrors the Python `connect_results_db`, `connect_proposals_db`,
 * `get_trial`, and `get_all_trials` functions.
 */

import Database from "better-sqlite3";

/** A row from the trials table. Metric columns vary by experiment. */
export type Trial = Record<string, unknown> & {
  trial_id: number;
  commit_sha: string | null;
  parent_commits: string | null;
  branch: string | null;
  status: string;
  artifacts_uri: string | null;
  description: string | null;
  timestamp: string;
};

/**
 * Open the results database read-only.
 *
 * Expects the database to already use DELETE journal mode (set by the
 * orchestrator at initialization time).
 */
export function connectResultsDb(path: string): Database.Database {
  const db = new Database(path, { readonly: true });
  db.pragma("busy_timeout = 5000");
  return db;
}

/**
 * Open the proposals database for read-write access with WAL journal.
 */
export function connectProposalsDb(path: string): Database.Database {
  const db = new Database(path);
  db.pragma("journal_mode = WAL");
  db.pragma("busy_timeout = 5000");
  return db;
}

/**
 * Fetch a completed trial by ID, returning all columns.
 * Returns null if the trial does not exist or is not successful.
 */
export function getTrial(resultsDb: string, trialId: number): Trial | null {
  const db = connectResultsDb(resultsDb);
  try {
    const row = db
      .prepare("SELECT * FROM trials WHERE trial_id = ? AND status = 'success'")
      .get(trialId) as Trial | undefined;
    return row ?? null;
  } finally {
    db.close();
  }
}

/**
 * Fetch all completed trials.
 *
 * @param resultsDb - Path to the results database.
 * @param orderBy - Optional raw SQL ORDER BY clause (trusted internal input).
 */
export function getAllTrials(
  resultsDb: string,
  orderBy?: string,
): Trial[] {
  const suffix = orderBy
    ? ` ORDER BY ${orderBy}`
    : " ORDER BY trial_id ASC";
  const db = connectResultsDb(resultsDb);
  try {
    return db
      .prepare(`SELECT * FROM trials WHERE status = 'success'${suffix}`)
      .all() as Trial[];
  } finally {
    db.close();
  }
}
