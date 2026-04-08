import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import Database from "better-sqlite3";

import {
  connectResultsDb,
  connectProposalsDb,
  getTrial,
  getAllTrials,
  createProposal,
  readTrialArtifact,
  configureLogging,
  logEvent,
  getHeadSha,
} from "../src/index.js";

let tmp: string;

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "planner-kit-test-"));
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

function initResultsDb(path: string): void {
  const db = new Database(path);
  db.exec(`
    CREATE TABLE trials (
      trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
      commit_sha TEXT,
      parent_commits TEXT,
      branch TEXT,
      status TEXT NOT NULL,
      artifacts_uri TEXT,
      description TEXT,
      timestamp TEXT NOT NULL,
      score REAL
    )
  `);
  db.exec("PRAGMA journal_mode = DELETE");
  db.close();
}

function initProposalsDb(path: string): void {
  const db = new Database(path);
  db.exec(`
    CREATE TABLE proposals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      priority REAL NOT NULL,
      slug TEXT NOT NULL,
      parent_commits TEXT NOT NULL,
      artifacts_uri TEXT NOT NULL,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      CHECK (status IN ('drafting', 'ready', 'dispatched', 'completed'))
    )
  `);
  db.close();
}

describe("db", () => {
  it("connectResultsDb opens read-only", () => {
    const dbPath = join(tmp, "results.db");
    initResultsDb(dbPath);

    const db = connectResultsDb(dbPath);
    expect(() => db.exec("INSERT INTO trials (status, timestamp) VALUES ('success', 'now')")).toThrow();
    db.close();
  });

  it("connectProposalsDb opens read-write with WAL", () => {
    const dbPath = join(tmp, "proposals.db");
    initProposalsDb(dbPath);

    const db = connectProposalsDb(dbPath);
    const mode = db.pragma("journal_mode", { simple: true });
    expect(mode).toBe("wal");
    db.close();
  });
});

describe("getTrial", () => {
  it("returns a successful trial by ID", () => {
    const dbPath = join(tmp, "results.db");
    initResultsDb(dbPath);

    const db = new Database(dbPath);
    db.prepare(
      "INSERT INTO trials (commit_sha, status, timestamp, score) VALUES (?, 'success', 'now', ?)",
    ).run("abc123", 0.95);
    db.close();

    const trial = getTrial(dbPath, 1);
    expect(trial).not.toBeNull();
    expect(trial!.trial_id).toBe(1);
    expect(trial!.commit_sha).toBe("abc123");
    expect(trial!.score).toBe(0.95);
  });

  it("returns null for non-existent trial", () => {
    const dbPath = join(tmp, "results.db");
    initResultsDb(dbPath);

    expect(getTrial(dbPath, 999)).toBeNull();
  });

  it("returns null for non-success trial", () => {
    const dbPath = join(tmp, "results.db");
    initResultsDb(dbPath);

    const db = new Database(dbPath);
    db.prepare(
      "INSERT INTO trials (status, timestamp) VALUES ('error', 'now')",
    ).run();
    db.close();

    expect(getTrial(dbPath, 1)).toBeNull();
  });
});

describe("getAllTrials", () => {
  it("returns all successful trials ordered by trial_id", () => {
    const dbPath = join(tmp, "results.db");
    initResultsDb(dbPath);

    const db = new Database(dbPath);
    db.prepare("INSERT INTO trials (status, timestamp, score) VALUES ('success', 'now', ?)").run(0.5);
    db.prepare("INSERT INTO trials (status, timestamp, score) VALUES ('error', 'now', ?)").run(0.0);
    db.prepare("INSERT INTO trials (status, timestamp, score) VALUES ('success', 'now', ?)").run(0.9);
    db.close();

    const trials = getAllTrials(dbPath);
    expect(trials).toHaveLength(2);
    expect(trials[0].trial_id).toBe(1);
    expect(trials[1].trial_id).toBe(3);
  });

  it("supports custom orderBy", () => {
    const dbPath = join(tmp, "results.db");
    initResultsDb(dbPath);

    const db = new Database(dbPath);
    db.prepare("INSERT INTO trials (status, timestamp, score) VALUES ('success', 'now', ?)").run(0.5);
    db.prepare("INSERT INTO trials (status, timestamp, score) VALUES ('success', 'now', ?)").run(0.9);
    db.close();

    const trials = getAllTrials(dbPath, "score DESC");
    expect(trials[0].score).toBe(0.9);
  });
});

describe("createProposal", () => {
  it("creates plan.md and database row", () => {
    const dbPath = join(tmp, "proposals.db");
    const proposalsDir = join(tmp, "proposals");
    initProposalsDb(dbPath);

    createProposal({
      proposalsDb: dbPath,
      proposalsDir,
      priority: 2.0,
      slug: "test-proposal",
      parentCommits: ["abc123"],
      planText: "Do the thing.",
    });

    // Check file
    const planText = readFileSync(join(proposalsDir, "test-proposal", "plan.md"), "utf-8");
    expect(planText).toBe("Do the thing.\n");

    // Check database
    const db = new Database(dbPath, { readonly: true });
    const row = db.prepare("SELECT * FROM proposals WHERE slug = ?").get("test-proposal") as Record<string, unknown>;
    expect(row.priority).toBe(2.0);
    expect(row.status).toBe("ready");
    expect(JSON.parse(row.parent_commits as string)).toEqual(["abc123"]);
    db.close();
  });
});

describe("readTrialArtifact", () => {
  it("reads a text artifact", () => {
    const artifactsDir = join(tmp, "artifacts");
    const trialDir = join(artifactsDir, "trial-1");
    mkdirSync(trialDir, { recursive: true });
    writeFileSync(join(trialDir, "plan.md"), "  My plan  \n", "utf-8");

    const content = readTrialArtifact(artifactsDir, 1, "plan.md");
    expect(content).toBe("My plan");
  });

  it("returns null for missing file", () => {
    expect(readTrialArtifact(join(tmp, "artifacts"), 1, "plan.md")).toBeNull();
  });
});

describe("logging", () => {
  it("configureLogging returns null path when EDEN_LOG_DIR is unset", () => {
    const original = process.env.EDEN_LOG_DIR;
    delete process.env.EDEN_LOG_DIR;
    try {
      const logger = configureLogging();
      expect(logger.path).toBeNull();
    } finally {
      if (original !== undefined) process.env.EDEN_LOG_DIR = original;
    }
  });

  it("logEvent writes sorted JSON lines", () => {
    const logDir = join(tmp, "logs");
    mkdirSync(logDir, { recursive: true });
    const logger = { path: join(logDir, "plan.log") };

    logEvent(logger, { event: "startup", count: 3, alpha: "first" });
    logEvent(logger, { event: "propose", slug: "test" });

    const lines = readFileSync(logger.path, "utf-8").trim().split("\n");
    expect(lines).toHaveLength(2);

    const first = JSON.parse(lines[0]);
    // Keys should be sorted
    expect(Object.keys(first)).toEqual(["alpha", "count", "event"]);
    expect(first.event).toBe("startup");
  });

  it("logEvent is a no-op when path is null", () => {
    const logger = { path: null };
    // Should not throw
    logEvent(logger, { event: "test" });
  });
});

describe("getHeadSha", () => {
  it("returns HEAD sha from a git repo", () => {
    const repo = join(tmp, "workspace");
    mkdirSync(repo);
    execFileSync("git", ["init"], { cwd: repo });
    execFileSync("git", ["-c", "user.name=test", "-c", "user.email=test@test", "commit", "--allow-empty", "-m", "init"], { cwd: repo });

    const sha = getHeadSha(repo);
    expect(sha).toMatch(/^[0-9a-f]{40}$/);
  });
});
