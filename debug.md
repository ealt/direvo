---
name: structured-debug
description: 'Structured debugging workflow using scientific method. Trigger phrases: "debug this", "help me debug", "why is this failing", "find the bug", "this is broken", "fix this error".'
argument-hint: '[error message or problem description]'
---

# Debug — Structured Debugging Workflow

## Overview

A structured debugging guide that applies the scientific method to software bugs: observe, reproduce, hypothesize, test, isolate, fix, verify, audit. Works across languages, frameworks, and bug categories.

Use judgment. The goal is disciplined investigation, not mechanical box-checking. Follow the workflow strongly enough to avoid guessing, but adapt the order and depth to the bug in front of you.

## When to Use

- "debug this" / "help me debug" — general debugging assistance
- "why is this failing" / "find the bug" — root cause investigation
- "this is broken" / "fix this error" — error diagnosis and repair
- Test failures, crashes, wrong output, build errors, flaky behavior

## Principles

- **Make things reproducible.** A bug you can't trigger on demand is hard to verify. If you cannot reproduce it, switch into evidence-collection mode instead of pretending you are fixing it.
- **Separate facts from theories.** Collect raw evidence first. State hypotheses explicitly.
- **Compare broken to known-good.** A working example in the same codebase or environment often narrows the search faster than pure reasoning.
- **Trace boundaries, not just symptoms.** In multi-step systems, instrument the handoff points to find where reality first diverges from expectations.
- **Use external knowledge deliberately.** After you understand the local facts, search official docs, release notes, and issue trackers first, then broader web results for matching failure modes or known incompatibilities.
- **Tighten the loop.** Minimize the time between "change something" and "observe the result."
- **Reduce human effort.** Batch diagnostics, write helper scripts when useful, and avoid one-command-per-round-trip debugging.

## Guardrails

These rules apply throughout the workflow:

1. **Read the actual error** — don't skim or paraphrase. Quote it verbatim.
2. **Prefer reproduction before explanation** — confirm the failure is observable and consistent. If you cannot reproduce it, treat that as a different debugging problem and gather evidence accordingly.
3. **Distinguish diagnostics from fixes** — temporary logs, probes, repro scripts, and untracked helpers are investigation tools. Repo-tracked fixes should wait until a hypothesis is supported.
4. **Track investigation state** — maintain a ledger of hypotheses, tests, and results so work isn't repeated.
5. **One hypothesis at a time** — test each candidate independently before moving to the next.
6. **Compare against a known-good case** whenever one exists — another test, a healthy environment, an older commit, or a similar working code path.
7. **Widen before deepening** — if repeated related hypotheses fail, step back and question your assumptions, system boundaries, or the architecture.
8. **Verify independently and clean up** — the fix must pass the original reproduction case, broader regression checks, and leave no accidental debugging residue behind.

## Workflow

### Phase 1: Observe

Collect raw evidence before forming theories. Do not skip this phase.

**Steps:**

1. **Check for project-level debugging context.** Before doing anything else, look for repo-specific guidance that may shortcut the investigation:
   - `AGENTS.md`, `CLAUDE.md` — may document known footguns, architecture gotchas, or debugging tips
   - `docs/debugging.md` or similar — dedicated debugging notes for this project
   - If project context mentions this class of bug, follow its guidance first.
2. **Ask the user (if interactive):**
   - **What exactly happens?** Error messages, hangs, unexpected output, nothing at all.
   - **What did they expect?** Clarify the gap between expected and actual behavior.
   - **What changed?** Recent code changes, config changes, environment changes, updates.
   - **Can they reproduce it?** Every time, intermittently, only in certain conditions.
3. **Read the full error output** — stack trace, error message, log lines. Quote the exact text. Consult `references/error-reading-guide.md` if the error format is unfamiliar.
4. **Classify the bug category:**
   - **Crash / Runtime Error** — unhandled exception, segfault, panic
   - **Wrong Output / Logic Error** — runs without error but produces incorrect results
   - **Test Failure** — one or more tests fail
   - **Build / Compile Error** — code won't compile or bundle
   - **Performance** — too slow, memory leak, resource exhaustion
   - **Intermittent / Flaky** — fails sometimes, passes other times
   - **Environment / Configuration** — works elsewhere, fails here
5. **Capture an environment fingerprint** — runtime and tool versions, OS and architecture, relevant env vars, cwd, feature flags, container or host, CI or local, and any relevant dependent services.
6. **Establish timeline** — check recent commits, recent file changes, and any environment changes. Identify what changed since it last worked.
   - **For regressions:** find the last known good point. Use `git bisect` when the search space is large enough to justify it.

**Output:** A concise fact summary: exact error, category, expected vs actual behavior, environment fingerprint, reproduction status, timeline of recent changes, and user observations.

### Phase 2: Reproduce and Isolate

Break the problem into the smallest reproducible unit.

**Steps:**

1. **Run the failing command or test.** Record the exact invocation and output. If it cannot be reproduced, note the conditions under which it was reported.
2. **Create a minimal reproduction** — a script, test, or single command that triggers the issue. Keep it local and temporary until you know whether it should become a permanent regression test.
3. **Progressively isolate variables** — start from the simplest case and add one variable at a time. Fail fast at the first breakpoint.
4. **Find a known-good comparison** — a passing test, older commit, similar code path, healthy environment, or equivalent request that works. Compare inputs, outputs, and assumptions.
5. **If you still cannot reproduce it, switch modes** — gather evidence that will make future reproduction easier: frequency, timestamps, seeds, order of operations, logs, traces, and environment details.
6. **Treat "works for me" as a red flag.** When your test passes but the user's fails, the priority shifts from "fix the bug" to "find why the environments differ."

**Output:** A minimal reproduction or an explicit evidence-collection plan, plus the exact command or sequence and observed output.

**Non-repro playbook:**

- Capture the exact command, inputs, timestamps, machine or environment, and failure window.
- Record the environment fingerprint and compare it to a known-good environment.
- Turn on targeted logging or tracing at likely subsystem boundaries.
- Capture determinism inputs such as random seeds, ordering, parallelism, and timezone.
- Search docs, release notes, and issue trackers for the exact error or symptom shape.
- Do not jump to repo-tracked fixes. The immediate goal is to make the next reproduction attempt easier and more informative.

### Phase 3: Hypothesize

Generate ranked candidate root causes.

**Steps:**

1. **Check the obvious first:**
   - Correct branch? (`git branch --show-current`)
   - Dependencies installed and up to date? (`node_modules`, `venv`, `go.sum`, etc.)
   - File saved? No unsaved editor buffers?
   - Environment variables set? Correct config file?
2. **Consult category-specific strategies** in `references/debugging-patterns.md` for the bug category identified in Phase 1. If a language or framework-specific guide exists in `references/languages/`, consult it for targeted strategies and common pitfalls.
3. **Search for known failure modes.** Once you know the exact error, environment, and symptom shape, search official docs, release notes, issue trackers, and targeted web results for similar failures. Use them to refine hypotheses, not to skip local verification.
4. **State the invariants.** What should be true at each major checkpoint? Use those expectations to decide where to inspect next.
5. **Question the inputs, not just the mechanism.** When a complex mechanism keeps failing, step back and ask whether the inputs are valid, not just the plumbing. Check boundary conditions, lengths, encoding, permissions, and stale state.
6. **Generate 2–5 ranked hypotheses.** For each:
   - **Hypothesis:** one-sentence statement of the suspected cause
   - **Test plan:** concrete action to confirm or eliminate it
   - **Predicted outcome:** what you expect to see if it is correct
7. **Rank by likelihood and cost to test.** Cheap, high-probability tests go first.

**Output:** Numbered hypothesis list with tests, predicted outcomes, and the known-good comparison you are using when relevant.

### Phase 4: Test

Work through hypotheses systematically.

**Steps:**

1. **Start an investigation ledger** (mental or written):

   ```text
   | # | Hypothesis | Test | Result | Verdict |
   |---|-----------|------|--------|---------|
   | 1 | ...       | ...  | ...    | ✓/✗     |
   ```

2. **Batch diagnostics** when it helps — one helper that answers several adjacent questions is often better than one test per round-trip.
3. **Test one hypothesis at a time.** Run the test plan. Record the actual result.
4. **Interpret the result:**
   - **Confirmed** → proceed to Phase 5 (Fix).
   - **Eliminated** → move to the next hypothesis.
   - **Inconclusive** → refine the test or gather more data.
5. **Instrument boundaries in multi-component systems** — if the bug spans layers such as client to API, CI to build, or service to database, inspect what enters and exits each boundary until you find the first bad handoff.
6. **If all hypotheses are eliminated:** do not generate more variants of the same theory. Instead:
   - Re-read the error output — did you miss something?
   - Question your assumptions — is the bug where you think it is?
   - Widen the search — check adjacent systems, upstream dependencies, environment differences, or the architecture itself.
   - Generate a new batch of hypotheses from this wider perspective.
7. **If repeated fix attempts fail, step back harder.** Stop stacking local patches and re-examine the problem framing, comparison point, or system design. Multiple failed "obvious" fixes are a signal, not bad luck.

**Isolating the root cause:**

Once a hypothesis is confirmed, trace backward from the failure to find the
original trigger. The crash site is almost never the root cause — it is the
place where bad state finally becomes visible. The root cause is upstream,
wherever valid state first became invalid.

1. **Start at the symptom.** Note the exact error and the line where it occurs.
2. **Find the immediate cause.** What value, state, or condition directly
   triggered the failure? Read the code at the crash site.
3. **Ask: what produced that value?** Follow the data backward — what function
   returned it, what caller passed it, what constructed the input?
4. **Keep tracing up the call chain.** At each level, ask the same question:
   where did this value come from? Stop when you reach the point where valid
   data was first corrupted or where an incorrect assumption was introduced.
5. **Verify the causal chain.** Confirm that the root cause you identified
   actually produces the observed failure. Trace the full path forward: root
   cause → intermediate effects → observed error.

**When you cannot trace manually,** add temporary instrumentation:
- Log the value and its origin before the operation that fails (use stderr in
  tests — stdout may be suppressed).
- Capture a stack trace at the instrumentation point (`new Error().stack`,
  `traceback.print_stack()`, etc.) to see the full call chain.
- Log at boundary points between components to find where reality first
  diverges from expectation.

**The principle:** never fix just the symptom. A null check at the crash site
hides the bug — find and fix why the value is null in the first place.

**Output:** Updated ledger with results. Confirmed root cause with full causal chain.

### Phase 5: Fix and Verify

Apply the minimum change to fix the root cause.

**Steps:**

1. **One fix per iteration.** Don't stack multiple changes — you won't know which one mattered, or which one made things worse.
2. **Design the fix:**
   - Target the root cause, not the symptom.
   - Prefer the smallest change that correctly addresses the issue.
   - Consider edge cases introduced by the fix.
3. **Apply the fix.**
4. **Keep diagnostics only as long as needed.** Temporary logging and helper scripts are fine during verification, but remove or intentionally promote them before finalizing.
5. **Verify against the reproduction case** — run the exact command from Phase 2. Confirm it now succeeds.
6. **Verify against a nearby comparison point** — a known-good example, a close edge case, or the opposite side of the branch condition that failed.
7. **Run the broader test suite** — ensure no regressions.
8. **Add guards to make the failure mode harder to reintroduce.** A single
   validation check can be bypassed by different code paths, refactoring, or
   mocks. Consider which layers are appropriate for the bug you just fixed:
   - **Entry point validation** — reject obviously invalid input at API or
     function boundaries (empty strings, null values, out-of-range arguments).
   - **Business logic validation** — check that data makes sense for the
     specific operation, not just that it exists.
   - **Environment guards** — prevent dangerous operations in specific
     contexts (for example, refusing destructive actions outside a temp
     directory during tests).
   - **Durable observability** — structured logging or tracing at key
     boundaries that stays in production, so the next occurrence is easier to
     diagnose. This is distinct from temporary debug instrumentation, which
     is removed in Phase 6.
   - **Tests** — a regression test that reproduces the original failure mode,
     plus edge-case tests for related inputs.

   Not every fix needs all layers. Use judgment — but when a single check
   feels fragile, adding a second layer at a different point in the data flow
   makes the bug structurally harder to reintroduce.

**Output:** Fixed code, passing reproduction case, passing test suite.

### Phase 6: Audit

Review all changes made during the debugging session.

**Steps:**

1. **Review every change made during the session.** Speculative fixes accumulate during debugging. Ask: "does this still make sense now that we know the actual cause?" Revert or simplify anything that was addressing a misdiagnosis.
2. **Clean up** — remove temporary diagnostics, helper scripts, and temporary test code that are not useful as permanent instrumentation.
3. **Write a summary:**
   - **Root cause:** what was wrong and why
   - **Fix:** what was changed
   - **Verification:** how it was confirmed
   - **Regression risk:** what else could be affected (low/medium/high with explanation)
4. **Document the root cause** in the commit message or changelog so the reasoning is preserved.
5. **Reflect on lessons learned.** Ask: what slowed this investigation down? What would have found the root cause faster? If the session revealed a recurring pattern, a missing diagnostic tool, or a gap in the workflow, capture it as a concrete improvement.

## Anti-Patterns to Avoid

- **Shotgun debugging** — making random changes hoping something works. Every change should test a specific hypothesis.
- **Fixing the symptom** — adding a null check at the crash site instead of fixing why the value is null.
- **Cargo-cult fixes** — copying a fix without understanding why it works.
- **Ignoring known-good evidence** — arguing from theory when a working example exists to compare against.
- **Scope creep** — refactoring or improving code you encounter during debugging. Stay focused on the bug.
- **Fix spirals** — repeated failed fixes without stepping back to question the framing, boundaries, or architecture.
- **One-at-a-time round-trips** — sending one diagnostic command per iteration when a small helper could answer several questions at once.
- **Untested tests** — using a reproduction script or helper you have not verified. A buggy test wastes an entire iteration.

## Recommended Session Shape

Use this structure when reporting progress. It is a helpful default, not a required template:

- **Facts** — exact error, reproduction command, actual behavior, expected behavior, impact
- **Repro** — whether you can reproduce it, how often, and under what conditions
- **Hypotheses** — ranked candidate causes with tests and predicted outcomes
- **Tests run** — what you checked and what happened
- **Confirmed cause** — the earliest proven divergence from expected behavior
- **Fix** — the minimum change that addresses the confirmed cause
- **Verification** — original repro, nearby edge case or known-good comparison, and broader regression surface
- **Remaining uncertainty** — anything you still cannot prove

## Worked Examples

Load these only when you want a concrete model for what a good debugging session looks like:

- `references/examples/environment-mismatch.md` — local success, user failure, environment drift
- `references/examples/flaky-test.md` — intermittent test failure, determinism capture, and isolation
