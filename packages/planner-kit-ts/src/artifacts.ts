/**
 * Trial artifact reading.
 *
 * Mirrors the Python `read_trial_artifact` function.
 */

import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";

/**
 * Read a text artifact file from a completed trial.
 *
 * Intended for text artifacts (plan.md, notes.md, eval_report.json).
 * Returns the trimmed file contents, or null if the file does not exist
 * or cannot be read as UTF-8.
 */
export function readTrialArtifact(
  artifactsDir: string,
  trialId: number,
  filename: string,
): string | null {
  const path = join(artifactsDir, `trial-${trialId}`, filename);
  try {
    const stat = statSync(path);
    if (!stat.isFile()) return null;
    return readFileSync(path, "utf-8").trim();
  } catch {
    return null;
  }
}
