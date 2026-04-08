import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Trial } from "../../types";
import { fetchArtifact, listArtifacts } from "../../api/files";
import styles from "./ArtifactViewer.module.css";

interface Props {
  trials: Trial[];
  initialTrialId?: number | null;
}

export function ArtifactViewer({ trials, initialTrialId }: Props) {
  const [selectedTrial, setSelectedTrial] = useState<number | null>(initialTrialId ?? null);

  // Update selection when navigated to from another view.
  useEffect(() => {
    if (initialTrialId != null) {
      setSelectedTrial(initialTrialId);
    }
  }, [initialTrialId]);
  const [artifacts, setArtifacts] = useState<string[]>([]);
  const [activeFile, setActiveFile] = useState<string>("");
  const [content, setContent] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (selectedTrial == null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    listArtifacts(selectedTrial)
      .then((files) => {
        if (cancelled) return;
        setArtifacts(files);
        setActiveFile(files[0] ?? "");
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTrial]);

  useEffect(() => {
    if (selectedTrial == null || !activeFile) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchArtifact(selectedTrial, activeFile)
      .then((text) => {
        if (!cancelled) setContent(text);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTrial, activeFile]);

  return (
    <div className={styles.viewer}>
      <div className={styles.sidebar}>
        <h4>Trials</h4>
        <div className={styles.trialList}>
          {trials.map((trial) => (
            <button
              key={trial.trial_id}
              className={`${styles.trialItem} ${trial.trial_id === selectedTrial ? styles.selected : ""}`}
              onClick={() => setSelectedTrial(trial.trial_id)}
            >
              <span className={styles.trialId}>#{trial.trial_id}</span>
              <span className={styles.trialStatus}>{trial.status}</span>
            </button>
          ))}
          {trials.length === 0 && (
            <p className={styles.empty}>No trials.</p>
          )}
        </div>
      </div>

      <div className={styles.content}>
        {selectedTrial == null ? (
          <p className={styles.placeholder}>Select a trial to view artifacts.</p>
        ) : error ? (
          <p className={styles.placeholder} style={{ color: "var(--error)" }}>{error}</p>
        ) : loading ? (
          <p className={styles.placeholder}>Loading...</p>
        ) : artifacts.length === 0 ? (
          <p className={styles.placeholder}>No artifacts for trial #{selectedTrial}.</p>
        ) : (
          <>
            <div className={styles.fileTabs}>
              {artifacts.map((file) => (
                <button
                  key={file}
                  className={`${styles.fileTab} ${file === activeFile ? styles.activeTab : ""}`}
                  onClick={() => setActiveFile(file)}
                >
                  {file}
                </button>
              ))}
            </div>
            <div className={styles.fileContent}>
              {activeFile.endsWith(".md") ? (
                <div className={styles.markdown}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {content}
                  </ReactMarkdown>
                </div>
              ) : activeFile.endsWith(".json") ? (
                <pre className={styles.json}>
                  {(() => {
                    try {
                      return JSON.stringify(JSON.parse(content), null, 2);
                    } catch {
                      return content;
                    }
                  })()}
                </pre>
              ) : (
                <pre className={styles.pre}>{content}</pre>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
