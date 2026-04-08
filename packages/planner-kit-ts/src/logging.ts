/**
 * Structured JSON-line logging for planner scripts.
 *
 * Mirrors the Python `configure_logging` and `log_event` functions.
 */

import { appendFileSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";

export interface Logger {
  /** Path to the log file, or null if logging is disabled. */
  path: string | null;
}

/**
 * Configure a JSON-line logger that writes to `plan.log`.
 *
 * Reads `EDEN_LOG_DIR` from the environment. If unset, returns a
 * logger with no output path (logging calls become no-ops).
 */
export function configureLogging(): Logger {
  const logDir = process.env.EDEN_LOG_DIR;
  if (!logDir) return { path: null };

  const path = join(logDir, "plan.log");
  mkdirSync(dirname(path), { recursive: true });
  return { path };
}

/**
 * Emit a structured JSON log line with the given fields.
 */
export function logEvent(
  logger: Logger,
  fields: Record<string, unknown>,
): void {
  if (!logger.path) return;

  const sorted = Object.fromEntries(
    Object.entries(fields).sort(([a], [b]) => a.localeCompare(b)),
  );
  appendFileSync(logger.path, JSON.stringify(sorted) + "\n", "utf-8");
}
