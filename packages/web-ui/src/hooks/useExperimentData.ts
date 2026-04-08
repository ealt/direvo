import { useCallback, useEffect, useRef, useState } from "react";
import { loadDatabase, type Database } from "../db/sqlite";
import { listTrials, listProposals, metricSeries, bestTrial } from "../db/queries";
import { fetchInfo, fetchLogSince } from "../api/files";
import type { ExperimentInfo, Trial, Proposal, MetricSeries, LogEvent } from "../types";

const POLL_INTERVAL_MS = 3000;

export interface ExperimentState {
  info: ExperimentInfo | null;
  trials: Trial[];
  proposals: Proposal[];
  metrics: MetricSeries[];
  best: Trial | null;
  logEvents: LogEvent[];
  loading: boolean;
  error: string | null;
}

export function useExperimentData(): ExperimentState {
  const [state, setState] = useState<ExperimentState>({
    info: null,
    trials: [],
    proposals: [],
    metrics: [],
    best: null,
    logEvents: [],
    loading: true,
    error: null,
  });

  const resultsDbRef = useRef<Database | null>(null);
  const proposalsDbRef = useRef<Database | null>(null);
  const logOffsetRef = useRef(0);
  const resultsEtagRef = useRef("");
  const proposalsEtagRef = useRef("");

  const refreshData = useCallback(async (info: ExperimentInfo) => {
    const files = info.files;

    // Check and reload results.db
    if (files.results_db?.available) {
      const response = await fetch(files.results_db.path, {
        method: "HEAD",
        headers: resultsEtagRef.current
          ? { "If-None-Match": resultsEtagRef.current }
          : {},
      });
      if (response.status !== 304) {
        const newEtag = response.headers.get("etag") ?? "";
        if (newEtag !== resultsEtagRef.current || !resultsDbRef.current) {
          resultsEtagRef.current = newEtag;
          resultsDbRef.current?.close();
          resultsDbRef.current = await loadDatabase(files.results_db.path);
        }
      }
    }

    // Check and reload proposals.db
    if (files.proposals_db?.available) {
      const response = await fetch(files.proposals_db.path, {
        method: "HEAD",
        headers: proposalsEtagRef.current
          ? { "If-None-Match": proposalsEtagRef.current }
          : {},
      });
      if (response.status !== 304) {
        const newEtag = response.headers.get("etag") ?? "";
        if (newEtag !== proposalsEtagRef.current || !proposalsDbRef.current) {
          proposalsEtagRef.current = newEtag;
          proposalsDbRef.current?.close();
          proposalsDbRef.current = await loadDatabase(files.proposals_db.path);
        }
      }
    }

    // Query databases
    const trials = resultsDbRef.current
      ? listTrials(resultsDbRef.current)
      : [];
    const proposals = proposalsDbRef.current
      ? listProposals(proposalsDbRef.current)
      : [];
    const metrics = resultsDbRef.current
      ? metricSeries(resultsDbRef.current, info.metrics_schema)
      : [];
    const best = resultsDbRef.current
      ? bestTrial(
          resultsDbRef.current,
          info.objective.expr,
          info.objective.direction
        )
      : null;

    // Fetch log events
    let logEvents: LogEvent[] = [];
    let logFullReload = false;
    if (files.session_log?.available) {
      const result = await fetchLogSince(logOffsetRef.current);
      logOffsetRef.current = result.newOffset;
      logEvents = result.lines;
      logFullReload = result.fullReload;
    }

    return { trials, proposals, metrics, best, logEvents, logFullReload };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function init() {
      try {
        const info = await fetchInfo();
        const data = await refreshData(info);
        if (!cancelled) {
          setState((prev) => ({
            ...prev,
            info,
            ...data,
            logEvents: data.logFullReload
              ? data.logEvents
              : [...prev.logEvents, ...data.logEvents],
            loading: false,
          }));
        }
      } catch (e) {
        if (!cancelled) {
          setState((prev) => ({
            ...prev,
            loading: false,
            error: e instanceof Error ? e.message : String(e),
          }));
        }
      }
    }

    async function poll() {
      try {
        const info = await fetchInfo();
        const data = await refreshData(info);
        if (!cancelled) {
          setState((prev) => ({
            ...prev,
            info,
            ...data,
            logEvents: data.logFullReload
              ? data.logEvents
              : [...prev.logEvents, ...data.logEvents],
          }));
        }
      } catch {
        // silently ignore poll errors
      }
      if (!cancelled) {
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      }
    }

    init().then(() => {
      if (!cancelled) {
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      }
    });

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      resultsDbRef.current?.close();
      proposalsDbRef.current?.close();
    };
  }, [refreshData]);

  return state;
}
