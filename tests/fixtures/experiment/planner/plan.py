"""Planner for the seed-sum experiment.

A persistent subprocess that proposes seeds to append to the workspace.
On startup, creates an initial batch of proposals. Then listens on stdin
for trial completion notifications and creates one follow-up proposal per
completed trial, building on that trial's checkpoint.

Seed selection: seeds are assigned sequentially (0, 1, 2, ...) and never
repeated. Priority for initial proposals equals the seed value. Priority
for follow-up proposals equals the completed trial's score.
"""

from __future__ import annotations

from eden.planner_kit import PlannerContext, Proposal, run_planner


def _make_initial_proposals(ctx: PlannerContext) -> list[Proposal]:
    return [
        Proposal(
            slug=f"seed-{i}-init",
            priority=float(i),
            plan_text=f"Append seed {i}",
            parent_commits=[ctx.head_sha],
            log_fields={"seed": i},
        )
        for i in range(ctx.parallel_trials + 2)
    ]


def _make_reactive_proposal(ctx: PlannerContext, proposal_index: int, trial: dict) -> Proposal:
    return Proposal(
        slug=f"seed-{proposal_index}-t{trial['trial_id']}",
        priority=float(trial["score"]),
        plan_text=f"Append seed {proposal_index}",
        parent_commits=[trial["commit_sha"]],
        log_fields={"seed": proposal_index},
    )


if __name__ == "__main__":
    run_planner(
        make_initial_proposals=_make_initial_proposals,
        make_reactive_proposal=_make_reactive_proposal,
    )
