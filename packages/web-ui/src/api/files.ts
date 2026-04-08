import type { ExperimentInfo, LogEvent } from "../types";

export async function fetchInfo(): Promise<ExperimentInfo> {
  const response = await fetch("/experiment/info");
  if (!response.ok) {
    throw new Error(`Failed to fetch experiment info: ${response.status}`);
  }
  return response.json();
}

export async function fetchArtifact(
  trialId: number,
  filename: string
): Promise<string> {
  const response = await fetch(
    `/experiment/data/artifacts/trial-${trialId}/${filename}`
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch artifact: ${response.status}`);
  }
  return response.text();
}

export async function listArtifacts(trialId: number): Promise<string[]> {
  const response = await fetch(
    `/experiment/data/artifacts/${trialId}/_list`
  );
  if (!response.ok) {
    return [];
  }
  const data = await response.json();
  return data.files ?? [];
}

let logCarryover = "";

export async function fetchLogSince(
  offset: number
): Promise<{ lines: LogEvent[]; newOffset: number; fullReload: boolean }> {
  const response = await fetch("/experiment/data/session.log", {
    headers: { Range: `bytes=${offset}-` },
  });

  if (response.status === 416 || response.status === 404) {
    return { lines: [], newOffset: offset, fullReload: false };
  }

  const text = await response.text();
  const totalBytes = new TextEncoder().encode(text).length;
  const isFullFile = response.status === 200;

  // On full-file response, discard stale carryover from prior ranged reads.
  if (isFullFile) {
    logCarryover = "";
  }

  const combined = logCarryover + text;
  const rawLines = combined.split("\n");
  logCarryover = rawLines.pop() ?? "";

  const lines: LogEvent[] = [];
  for (const line of rawLines) {
    if (!line.trim()) continue;
    try {
      lines.push(JSON.parse(line));
    } catch {
      // skip malformed lines
    }
  }

  const carryoverBytes = new TextEncoder().encode(logCarryover).length;
  const newOffset = isFullFile
    ? totalBytes - carryoverBytes
    : offset + totalBytes - carryoverBytes;
  return { lines, newOffset, fullReload: isFullFile && offset > 0 };
}
