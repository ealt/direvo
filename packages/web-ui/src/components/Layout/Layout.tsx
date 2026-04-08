import { type ReactNode, useState } from "react";
import styles from "./Layout.module.css";

interface Tab {
  id: string;
  label: string;
  content: ReactNode;
  disabled?: boolean;
}

interface LayoutProps {
  tabs: Tab[];
  status: "live" | "ended" | "unknown";
  activeTabId?: string;
  onTabChange?: (id: string) => void;
}

const statusLabels: Record<string, string> = {
  live: "Live",
  ended: "Ended",
  unknown: "Unknown",
};

const statusColors: Record<string, string> = {
  live: "var(--success)",
  ended: "var(--text-muted)",
  unknown: "var(--warning)",
};

export function Layout({ tabs, status, activeTabId, onTabChange }: LayoutProps) {
  const [internalTab, setInternalTab] = useState(tabs[0]?.id ?? "");
  const activeTab = activeTabId ?? internalTab;
  const setActiveTab = onTabChange ?? setInternalTab;

  const activeContent = tabs.find((t) => t.id === activeTab)?.content;

  return (
    <div className={styles.layout}>
      <header className={styles.header}>
        <div className={styles.title}>
          <span className={styles.logo}>EDEN</span>
          <span
            className={styles.status}
            style={{ color: statusColors[status] }}
          >
            {statusLabels[status]}
          </span>
        </div>
        <nav className={styles.tabs}>
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={`${styles.tab} ${tab.id === activeTab ? styles.active : ""}`}
              onClick={() => setActiveTab(tab.id)}
              disabled={tab.disabled}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>
      <main className={styles.main}>{activeContent}</main>
    </div>
  );
}
