import { useCallback, useState } from "react";
import { useExperimentData } from "./hooks/useExperimentData";
import { Layout } from "./components/Layout/Layout";
import { MetricsDashboard } from "./components/MetricsDashboard/MetricsDashboard";
import { TrialTimeline } from "./components/TrialTimeline/TrialTimeline";
import { ProposalQueue } from "./components/ProposalQueue/ProposalQueue";
import { ArtifactViewer } from "./components/ArtifactViewer/ArtifactViewer";
import { Explorer } from "./components/Explorer/Explorer";

export default function App() {
  const state = useExperimentData();
  const [activeTab, setActiveTab] = useState("metrics");
  const [selectedTrialId, setSelectedTrialId] = useState<number | null>(null);

  const handleTrialSelect = useCallback((trialId: number) => {
    setSelectedTrialId(trialId);
    setActiveTab("artifacts");
  }, []);

  if (state.loading) {
    return (
      <div style={{ padding: 40, color: "var(--text-muted)", textAlign: "center" }}>
        Loading experiment data...
      </div>
    );
  }

  if (state.error) {
    return (
      <div style={{ padding: 40, color: "var(--error)" }}>
        <h2>Error</h2>
        <p>{state.error}</p>
      </div>
    );
  }

  const info = state.info!;
  const hasProposals = info.files.proposals_db?.available ?? false;

  const tabs = [
    {
      id: "metrics",
      label: "Metrics",
      content: (
        <MetricsDashboard
          metrics={state.metrics}
          best={state.best}
          metricsSchema={info.metrics_schema}
          objective={info.objective}
          onTrialSelect={handleTrialSelect}
        />
      ),
    },
    {
      id: "timeline",
      label: "Timeline",
      content: (
        <TrialTimeline
          trials={state.trials}
          logEvents={state.logEvents}
          parallelTrials={info.parallel_trials}
        />
      ),
    },
    {
      id: "artifacts",
      label: "Artifacts",
      content: <ArtifactViewer trials={state.trials} initialTrialId={selectedTrialId} />,
    },
    {
      id: "proposals",
      label: "Proposals",
      content: <ProposalQueue proposals={state.proposals} />,
      disabled: !hasProposals,
    },
    {
      id: "explorer",
      label: "Explorer",
      content: (
        <Explorer
          trials={state.trials}
          proposals={state.proposals}
          logEvents={state.logEvents}
          info={info}
        />
      ),
    },
  ];

  return <Layout tabs={tabs} status={info.status} activeTabId={activeTab} onTabChange={setActiveTab} />;
}
