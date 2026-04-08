import { useState } from "react";
import type { Proposal } from "../../types";
import styles from "./ProposalQueue.module.css";

interface Props {
  proposals: Proposal[];
}

const statusColors: Record<string, string> = {
  drafting: "var(--text-muted)",
  ready: "var(--starting)",
  dispatched: "var(--warning)",
  completed: "var(--success)",
};

type SortKey = "id" | "priority" | "status" | "created_at";

export function ProposalQueue({ proposals }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("id");
  const [filterStatus, setFilterStatus] = useState<string>("all");

  const filtered =
    filterStatus === "all"
      ? proposals
      : proposals.filter((p) => p.status === filterStatus);

  const sorted = [...filtered].sort((a, b) => {
    if (sortKey === "priority") return b.priority - a.priority;
    if (sortKey === "status") return a.status.localeCompare(b.status);
    if (sortKey === "created_at") return a.created_at.localeCompare(b.created_at);
    return a.id - b.id;
  });

  return (
    <div className={styles.queue}>
      <div className={styles.controls}>
        <select
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
        >
          <option value="all">All statuses</option>
          <option value="drafting">Drafting</option>
          <option value="ready">Ready</option>
          <option value="dispatched">Dispatched</option>
          <option value="completed">Completed</option>
        </select>
      </div>

      {sorted.length === 0 ? (
        <p className={styles.empty}>No proposals available.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th onClick={() => setSortKey("id")} className={styles.sortable}>
                ID {sortKey === "id" && "^"}
              </th>
              <th>Slug</th>
              <th
                onClick={() => setSortKey("priority")}
                className={styles.sortable}
              >
                Priority {sortKey === "priority" && "^"}
              </th>
              <th
                onClick={() => setSortKey("status")}
                className={styles.sortable}
              >
                Status {sortKey === "status" && "^"}
              </th>
              <th
                onClick={() => setSortKey("created_at")}
                className={styles.sortable}
              >
                Created {sortKey === "created_at" && "^"}
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => (
              <tr key={p.id}>
                <td className={styles.mono}>{p.id}</td>
                <td>{p.slug}</td>
                <td className={styles.mono}>{p.priority.toFixed(1)}</td>
                <td>
                  <span
                    className={styles.badge}
                    style={{
                      background: statusColors[p.status] ?? "var(--text-muted)",
                    }}
                  >
                    {p.status}
                  </span>
                </td>
                <td className={styles.muted}>{p.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
