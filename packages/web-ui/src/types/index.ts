export interface ExperimentInfo {
  metrics_schema: Record<string, "real" | "integer" | "text">;
  objective: { expr: string; direction: "maximize" | "minimize" };
  parallel_trials: number;
  status: "live" | "ended" | "unknown";
  files: Record<string, { path: string; available: boolean }>;
}

export interface Trial {
  trial_id: number;
  commit_sha: string | null;
  parent_commits: string | null;
  branch: string | null;
  status: "starting" | "success" | "error" | "eval_error";
  artifacts_uri: string | null;
  description: string | null;
  timestamp: string;
  [metric: string]: unknown;
}

export interface Proposal {
  id: number;
  priority: number;
  slug: string;
  parent_commits: string;
  artifacts_uri: string;
  status: "drafting" | "ready" | "dispatched" | "completed";
  created_at: string;
}

export interface LogEvent {
  timestamp: string;
  level: string;
  event?: string;
  [key: string]: unknown;
}

export interface MetricSeries {
  name: string;
  points: { trial_id: number; value: number }[];
}
