import type { Database } from "./sqlite";
import type { Trial, Proposal, MetricSeries } from "../types";

export function listTrials(db: Database): Trial[] {
  const stmt = db.prepare("SELECT * FROM trials ORDER BY trial_id ASC");
  const trials: Trial[] = [];
  while (stmt.step()) {
    const row = stmt.getAsObject() as unknown as Trial;
    trials.push(row);
  }
  stmt.free();
  return trials;
}

export function listProposals(db: Database): Proposal[] {
  const stmt = db.prepare("SELECT * FROM proposals ORDER BY id ASC");
  const proposals: Proposal[] = [];
  while (stmt.step()) {
    const row = stmt.getAsObject() as unknown as Proposal;
    proposals.push(row);
  }
  stmt.free();
  return proposals;
}

export function bestTrial(
  db: Database,
  expr: string,
  direction: "maximize" | "minimize"
): Trial | null {
  const order = direction === "maximize" ? "DESC" : "ASC";
  const sql = `
    SELECT * FROM trials
    WHERE status = 'success' AND (${expr}) IS NOT NULL
    ORDER BY (${expr}) ${order}, trial_id ASC
    LIMIT 1
  `;
  const stmt = db.prepare(sql);
  if (stmt.step()) {
    const row = stmt.getAsObject() as unknown as Trial;
    stmt.free();
    return row;
  }
  stmt.free();
  return null;
}

export function metricSeries(
  db: Database,
  metricsSchema: Record<string, string>
): MetricSeries[] {
  const series: MetricSeries[] = [];
  for (const name of Object.keys(metricsSchema)) {
    const sql = `
      SELECT trial_id, ${name} AS value FROM trials
      WHERE status = 'success' AND ${name} IS NOT NULL
      ORDER BY trial_id ASC
    `;
    const stmt = db.prepare(sql);
    const points: { trial_id: number; value: number }[] = [];
    while (stmt.step()) {
      const row = stmt.getAsObject() as { trial_id: number; value: number };
      points.push({ trial_id: row.trial_id, value: row.value });
    }
    stmt.free();
    series.push({ name, points });
  }
  return series;
}

export function runQuery(
  db: Database,
  sql: string
): { columns: string[]; rows: unknown[][] } {
  try {
    const results = db.exec(sql);
    if (results.length === 0) {
      return { columns: [], rows: [] };
    }
    return { columns: results[0].columns, rows: results[0].values };
  } catch (e) {
    throw new Error(
      `SQL error: ${e instanceof Error ? e.message : String(e)}`
    );
  }
}
