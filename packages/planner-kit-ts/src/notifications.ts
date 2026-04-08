/**
 * Trial completion notification parsing from stdin.
 *
 * Mirrors the Python `iter_trial_notifications` generator.
 */

import { createInterface } from "node:readline";

/**
 * Yield deduplicated trial IDs from stdin completion notifications.
 *
 * Parses the default notification template
 * `"Trial completed. ID: {trial_id}"`. Deduplication is process-local
 * and resets on planner restart.
 *
 * Planners that use a custom `plan_notify_template` in config should
 * parse stdin directly instead.
 */
export async function* iterTrialNotifications(): AsyncGenerator<number> {
  const seen = new Set<number>();

  const rl = createInterface({ input: process.stdin });

  for await (const rawLine of rl) {
    const line = rawLine.trim();
    if (!line || !line.includes("Trial completed")) continue;

    const parts = line.split(":");
    const idStr = parts[parts.length - 1]?.trim();
    if (!idStr) continue;

    const trialId = parseInt(idStr, 10);
    if (isNaN(trialId)) continue;
    if (seen.has(trialId)) continue;

    seen.add(trialId);
    yield trialId;
  }
}
