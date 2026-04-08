import { useState, useRef, useCallback } from "react";
import { loadDatabase, type Database } from "../../db/sqlite";
import { runQuery } from "../../db/queries";
import type { Trial, Proposal, LogEvent, ExperimentInfo } from "../../types";
import styles from "./Explorer.module.css";

interface Props {
  trials: Trial[];
  proposals: Proposal[];
  logEvents: LogEvent[];
  info: ExperimentInfo;
}

type Tab = "sql" | "trials" | "proposals" | "log" | "git";

const tabLabels: Record<Tab, string> = {
  sql: "SQL Console",
  trials: "Trials DB",
  proposals: "Proposals DB",
  log: "Session Log",
  git: "Git Tree",
};

export function Explorer({ trials, proposals, logEvents, info }: Props) {
  const [activeTab, setActiveTab] = useState<Tab>("sql");

  return (
    <div className={styles.explorer}>
      <div className={styles.tabs}>
        {(["sql", "trials", "proposals", "log", "git"] as Tab[]).map((tab) => (
          <button
            key={tab}
            className={`${styles.tab} ${tab === activeTab ? styles.active : ""}`}
            onClick={() => setActiveTab(tab)}
          >
            {tabLabels[tab]}
          </button>
        ))}
      </div>

      <div className={styles.content}>
        {activeTab === "sql" && <SqlConsole info={info} />}
        {activeTab === "trials" && <DataTable data={trials as unknown as Record<string, unknown>[]} />}
        {activeTab === "proposals" && <DataTable data={proposals as unknown as Record<string, unknown>[]} />}
        {activeTab === "log" && <LogViewer events={logEvents} />}
        {activeTab === "git" && <GitTree trials={trials} />}
      </div>
    </div>
  );
}

function SqlConsole({ info }: { info: ExperimentInfo }) {
  const [sql, setSql] = useState("SELECT * FROM trials ORDER BY trial_id ASC");
  const [dbTarget, setDbTarget] = useState<"results" | "proposals">("results");
  const [result, setResult] = useState<{ columns: string[]; rows: unknown[][] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dbRef = useRef<Database | null>(null);

  const execute = useCallback(async () => {
    setError(null);
    try {
      const files = info.files;
      const url = dbTarget === "results"
        ? files.results_db?.path
        : files.proposals_db?.path;
      if (!url) {
        setError("Database not available");
        return;
      }
      dbRef.current?.close();
      dbRef.current = await loadDatabase(url);
      const res = runQuery(dbRef.current, sql);
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    }
  }, [sql, dbTarget, info]);

  return (
    <div className={styles.sqlConsole}>
      <div className={styles.sqlControls}>
        <select value={dbTarget} onChange={(e) => setDbTarget(e.target.value as "results" | "proposals")}>
          <option value="results">results.db</option>
          <option value="proposals">proposals.db</option>
        </select>
        <button onClick={execute}>Run</button>
      </div>
      <textarea
        className={styles.sqlInput}
        value={sql}
        onChange={(e) => setSql(e.target.value)}
        rows={4}
        spellCheck={false}
      />
      {error && <div className={styles.error}>{error}</div>}
      {result && (
        <div className={styles.resultTable}>
          <table>
            <thead>
              <tr>
                {result.columns.map((col) => (
                  <th key={col}>{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j} className={styles.mono}>
                      {cell == null ? <span className={styles.null}>NULL</span> : String(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className={styles.rowCount}>{result.rows.length} rows</div>
        </div>
      )}
    </div>
  );
}

function DataTable({ data }: { data: Record<string, unknown>[] }) {
  if (data.length === 0) {
    return <p className={styles.empty}>No data.</p>;
  }
  const columns = Object.keys(data[0]);
  return (
    <div className={styles.resultTable}>
      <table>
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col}>{col}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => (
            <tr key={i}>
              {columns.map((col) => (
                <td key={col} className={styles.mono}>
                  {row[col] == null ? (
                    <span className={styles.null}>NULL</span>
                  ) : (
                    <>{String(row[col])}</>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className={styles.rowCount}>{data.length} rows</div>
    </div>
  );
}

function LogViewer({ events }: { events: LogEvent[] }) {
  const [filter, setFilter] = useState("");
  const filtered = filter
    ? events.filter((e) => e.event?.includes(filter))
    : events;

  return (
    <div className={styles.logViewer}>
      <input
        placeholder="Filter by event name..."
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        className={styles.logFilter}
      />
      <div className={styles.logEntries}>
        {filtered.map((event, i) => (
          <div key={i} className={styles.logEntry}>
            <span className={styles.logTime}>
              {event.timestamp?.slice(11, 19) ?? ""}
            </span>
            <span className={styles.logEvent}>{String(event.event ?? event.message ?? "")}</span>
            <span className={styles.logDetail}>
              {JSON.stringify(
                Object.fromEntries(
                  Object.entries(event).filter(
                    ([k]) => !["timestamp", "level", "logger", "message", "event", "session_id"].includes(k)
                  )
                )
              )}
            </span>
          </div>
        ))}
        {filtered.length === 0 && (
          <p className={styles.empty}>No log events.</p>
        )}
      </div>
    </div>
  );
}

const statusNodeColors: Record<string, string> = {
  success: "#22c55e",
  error: "#ef4444",
  eval_error: "#f97316",
  starting: "#3b82f6",
};

function GitTree({ trials }: { trials: Trial[] }) {
  // Build a simple DAG from parent_commits and commit_sha.
  const nodes = trials
    .filter((t) => t.commit_sha)
    .map((t) => ({
      id: t.trial_id,
      sha: t.commit_sha!,
      parents: (() => {
        try {
          return JSON.parse(t.parent_commits ?? "[]") as string[];
        } catch {
          return [];
        }
      })(),
      branch: t.branch ?? "",
      status: t.status,
    }));

  if (nodes.length === 0) {
    return <p className={styles.empty}>No committed trials to display.</p>;
  }

  const nodeHeight = 40;
  const nodeWidth = 200;
  const svgHeight = nodes.length * nodeHeight + 40;
  const svgWidth = nodeWidth + 100;

  // Map sha -> y position for edge drawing.
  const shaToY = new Map<string, number>();
  nodes.forEach((n, i) => shaToY.set(n.sha, i * nodeHeight + 30));

  return (
    <div className={styles.resultTable} style={{ overflow: "auto" }}>
      <svg width={svgWidth} height={svgHeight} style={{ display: "block" }}>
        {/* Edges */}
        {nodes.map((node) =>
          node.parents.map((parentSha) => {
            const parentY = shaToY.get(parentSha);
            const childY = shaToY.get(node.sha);
            if (parentY == null || childY == null) return null;
            return (
              <line
                key={`${parentSha}-${node.sha}`}
                x1={30}
                y1={parentY}
                x2={30}
                y2={childY}
                stroke="var(--border)"
                strokeWidth={2}
              />
            );
          })
        )}
        {/* Nodes */}
        {nodes.map((node, i) => {
          const y = i * nodeHeight + 30;
          const slug = node.branch.includes("-")
            ? node.branch.split("-").slice(1).join("-")
            : "";
          return (
            <g key={node.id}>
              <circle
                cx={30}
                cy={y}
                r={6}
                fill={statusNodeColors[node.status] ?? "#8b8fa3"}
              />
              <text x={50} y={y + 4} fill="var(--text)" fontSize={13} fontFamily="var(--font-mono)">
                #{node.id} {slug}
              </text>
              <text x={50} y={y + 18} fill="var(--text-muted)" fontSize={10} fontFamily="var(--font-mono)">
                {node.sha.slice(0, 8)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
