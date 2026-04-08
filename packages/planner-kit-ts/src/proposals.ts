/**
 * Proposal creation.
 *
 * Mirrors the Python `create_proposal` function.
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { connectProposalsDb } from "./db.js";

export interface CreateProposalOptions {
  proposalsDb: string;
  proposalsDir: string;
  priority: number;
  slug: string;
  parentCommits: string[];
  planText: string;
}

/**
 * Create a proposal with its plan.md and database row.
 */
export function createProposal(options: CreateProposalOptions): void {
  const {
    proposalsDb,
    proposalsDir,
    priority,
    slug,
    parentCommits,
    planText,
  } = options;

  const proposalPath = join(proposalsDir, slug);
  mkdirSync(proposalPath, { recursive: true });
  writeFileSync(join(proposalPath, "plan.md"), planText + "\n", "utf-8");

  const db = connectProposalsDb(proposalsDb);
  try {
    db.prepare(
      `INSERT INTO proposals (priority, slug, parent_commits, artifacts_uri, status, created_at)
       VALUES (?, ?, ?, ?, 'ready', datetime('now'))`,
    ).run(
      priority,
      slug,
      JSON.stringify(parentCommits),
      proposalPath,
    );
  } finally {
    db.close();
  }
}
