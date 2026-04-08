/**
 * @direvo/planner-kit — TypeScript SDK for writing EDEN planner scripts.
 *
 * Provides low-level protocol primitives for interacting with the EDEN
 * orchestrator via SQLite databases, filesystem artifacts, and stdin
 * notifications.
 */

export {
  connectResultsDb,
  connectProposalsDb,
  getTrial,
  getAllTrials,
  type Trial,
} from "./db.js";

export {
  createProposal,
  type CreateProposalOptions,
} from "./proposals.js";

export { readTrialArtifact } from "./artifacts.js";

export { iterTrialNotifications } from "./notifications.js";

export {
  configureLogging,
  logEvent,
  type Logger,
} from "./logging.js";

export { getHeadSha } from "./git.js";
