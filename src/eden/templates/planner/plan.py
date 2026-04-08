"""Stub planner for an EDEN experiment.

Uses the run_planner() convenience loop from eden.planner_kit.
Replace the callback implementations with your own logic.
"""

from __future__ import annotations

from eden.planner_kit import PlannerContext, Proposal, run_planner


def _make_initial_proposals(ctx: PlannerContext) -> list[Proposal]:
    """Create the first batch of proposals.

    These run before any trial results are available. Propose diverse
    strategies to explore the search space.
    """
    # TODO: Replace with your initial strategies.
    return [
        Proposal(
            slug="baseline",
            priority=1.0,
            plan_text="Implement a baseline approach.",
            parent_commits=[ctx.head_sha],
        ),
    ]


def _make_reactive_proposal(
    ctx: PlannerContext, proposal_index: int, trial: dict
) -> Proposal | None:
    """Create a follow-up proposal after a trial completes.

    Read the trial results and artifacts to decide what to try next.
    Return None to skip proposing (e.g., if no improvement is needed).
    """
    all_trials = ctx.get_all_trials()

    # TODO: Analyze results and propose a refined strategy.
    parent_sha = trial["commit_sha"] or ctx.head_sha

    return Proposal(
        slug=f"strategy-{proposal_index}-t{trial['trial_id']}",
        priority=float(proposal_index),
        plan_text="Improve on the previous approach.",
        parent_commits=[parent_sha],
    )


if __name__ == "__main__":
    run_planner(
        make_initial_proposals=_make_initial_proposals,
        make_reactive_proposal=_make_reactive_proposal,
    )
