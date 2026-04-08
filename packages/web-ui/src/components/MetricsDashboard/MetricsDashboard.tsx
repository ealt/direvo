import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import type { Trial, MetricSeries } from "../../types";
import styles from "./MetricsDashboard.module.css";

interface Props {
  metrics: MetricSeries[];
  best: Trial | null;
  metricsSchema: Record<string, string>;
  objective: { expr: string; direction: string };
  onTrialSelect?: (trialId: number) => void;
}

function formatMetricValue(value: unknown, type: string): string {
  if (value == null) return "-";
  if (type === "real" && typeof value === "number") return value.toFixed(4);
  if (type === "integer") return String(Math.round(value as number));
  return String(value);
}

export function MetricsDashboard({ metrics, best, metricsSchema, objective, onTrialSelect }: Props) {
  // Build convergence data: the objective metric with a running-best line.
  const objectiveSeries = metrics.find((m) => m.name === objective.expr);
  const convergenceData = (objectiveSeries?.points ?? []).map((p, i, arr) => {
    const prev = arr.slice(0, i + 1).map((pt) => pt.value);
    const runningBest =
      objective.direction === "maximize" ? Math.max(...prev) : Math.min(...prev);
    return { trial_id: p.trial_id, value: p.value, runningBest };
  });

  return (
    <div className={styles.dashboard}>
      {best && (
        <div className={styles.bestCard}>
          <h3>Best Trial: #{best.trial_id}</h3>
          <span className={styles.branch}>
            {best.branch?.split("-").slice(1).join("-") ?? ""}
          </span>
          <div className={styles.bestMetrics}>
            {Object.entries(metricsSchema).map(([name, type]) => (
              <div key={name} className={styles.metric}>
                <span className={styles.metricName}>{name}</span>
                <span className={styles.metricValue}>
                  {formatMetricValue(
                    (best as Record<string, unknown>)[name],
                    type
                  )}
                </span>
              </div>
            ))}
          </div>
          <div className={styles.objective}>
            {objective.direction} {objective.expr}
          </div>
        </div>
      )}

      {convergenceData.length > 0 && (
        <div className={styles.chart}>
          <h4>Convergence — {objective.direction} {objective.expr}</h4>
          <ResponsiveContainer width="100%" height={250}>
            <LineChart data={convergenceData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis
                dataKey="trial_id"
                label={{ value: "Trial", position: "bottom", fill: "var(--text-muted)" }}
                tick={{ fill: "var(--text-muted)", fontSize: 12 }}
              />
              <YAxis tick={{ fill: "var(--text-muted)", fontSize: 12 }} />
              <Tooltip
                contentStyle={{
                  background: "var(--bg-surface)",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius)",
                  color: "var(--text)",
                }}
              />
              <Line type="monotone" dataKey="value" stroke="var(--text-muted)" strokeWidth={1} dot={{ r: 2 }} name="value" />
              <Line type="stepAfter" dataKey="runningBest" stroke="var(--success)" strokeWidth={2} dot={false} name="best" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className={styles.charts}>
        {metrics.map((series) => (
          <div key={series.name} className={styles.chart}>
            <h4>{series.name}</h4>
            <ResponsiveContainer width="100%" height={250}>
              <LineChart
                data={series.points}
                onClick={onTrialSelect ? (e) => {
                  const trialId = (e?.activePayload?.[0]?.payload as { trial_id?: number })?.trial_id;
                  if (trialId != null) onTrialSelect(trialId);
                } : undefined}
                style={onTrialSelect ? { cursor: "pointer" } : undefined}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis
                  dataKey="trial_id"
                  label={{ value: "Trial", position: "bottom", fill: "var(--text-muted)" }}
                  tick={{ fill: "var(--text-muted)", fontSize: 12 }}
                />
                <YAxis tick={{ fill: "var(--text-muted)", fontSize: 12 }} />
                <Tooltip
                  contentStyle={{
                    background: "var(--bg-surface)",
                    border: "1px solid var(--border)",
                    borderRadius: "var(--radius)",
                    color: "var(--text)",
                  }}
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke="var(--accent)"
                  strokeWidth={2}
                  dot={{ r: 3, fill: "var(--accent)" }}
                  activeDot={{ r: 5, cursor: onTrialSelect ? "pointer" : "default" }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ))}
      </div>

      {metrics.length === 0 && (
        <p className={styles.empty}>No metric data available yet.</p>
      )}
    </div>
  );
}
