/**
 * Git helpers for planner scripts.
 *
 * Mirrors the Python `get_head_sha` function.
 */

import { execFileSync } from "node:child_process";

/**
 * Return the current HEAD commit SHA of the workspace repo.
 */
export function getHeadSha(workspace: string = "workspace"): string {
  const result = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: workspace,
    encoding: "utf-8",
  });
  return result.trim();
}
