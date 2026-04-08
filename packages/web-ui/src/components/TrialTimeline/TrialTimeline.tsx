import type { Trial, LogEvent } from "../../types";
import styles from "./TrialTimeline.module.css";

interface Props {
  trials: Trial[];
  logEvents: LogEvent[];
  parallelTrials: number;
}

const statusColors: Record<string, string> = {
  success: "var(--success)",
  error: "var(--error)",
  eval_error: "var(--eval-error)",
  starting: "var(--starting)",
};

function buildSlotMap(logEvents: LogEvent[]): Map<number, number> {
  const map = new Map<number, number>();
  for (const event of logEvents) {
    if (event.event === "trial_started" && typeof event.trial_id === "number" && typeof event.slot === "number") {
      map.set(event.trial_id, event.slot);
    }
  }
  return map;
}

function slugFromBranch(branch: string | null): string {
  if (!branch || !branch.includes("-")) return "";
  return branch.split("-").slice(1).join("-");
}

export function TrialTimeline({ trials, logEvents, parallelTrials }: Props) {
  const slotMap = buildSlotMap(logEvents);
  const hasSlotData = slotMap.size > 0;

  if (!hasSlotData) {
    // Flat list fallback
    return (
      <div className={styles.timeline}>
        <div className={styles.notice}>
          Slot assignments unavailable — session log not found.
        </div>
        <div className={styles.flatList}>
          {trials.map((trial) => (
            <TrialCard key={trial.trial_id} trial={trial} />
          ))}
        </div>
        {trials.length === 0 && (
          <p className={styles.empty}>No trials yet.</p>
        )}
      </div>
    );
  }

  // Group by slot
  const slots: Trial[][] = Array.from({ length: parallelTrials }, () => []);
  for (const trial of trials) {
    const slot = slotMap.get(trial.trial_id) ?? 0;
    if (slot < slots.length) {
      slots[slot].push(trial);
    }
  }

  return (
    <div className={styles.timeline}>
      {slots.map((slotTrials, slot) => (
        <div key={slot} className={styles.lane}>
          <div className={styles.laneLabel}>Slot {slot}</div>
          <div className={styles.laneCards}>
            {slotTrials.map((trial) => (
              <TrialCard key={trial.trial_id} trial={trial} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function TrialCard({ trial }: { trial: Trial }) {
  const slug = slugFromBranch(trial.branch);
  return (
    <div
      className={styles.card}
      style={{ borderLeftColor: statusColors[trial.status] ?? "var(--border)" }}
    >
      <div className={styles.cardHeader}>
        <span className={styles.trialId}>#{trial.trial_id}</span>
        <span
          className={styles.badge}
          style={{
            background: statusColors[trial.status] ?? "var(--text-muted)",
          }}
        >
          {trial.status}
        </span>
      </div>
      {slug && <div className={styles.slug}>{slug}</div>}
    </div>
  );
}
