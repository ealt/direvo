"""Stub implementer for an EDEN experiment.

The implementer receives a plan file at .eden/trial/plan.md describing
what changes to make. It should read the plan, modify workspace files
accordingly, and optionally write notes to .eden/trial/notes.md.

This script runs from the trial worktree directory.
"""

from pathlib import Path


def main() -> None:
    plan_path = Path(".eden/trial/plan.md")
    if plan_path.exists():
        plan = plan_path.read_text(encoding="utf-8")
        print(f"Plan received ({len(plan)} chars). TODO: implement changes.")
    else:
        print("No plan file found.")

    # TODO: Implement the changes described in the plan.

    # Optionally write implementation notes for the planner to review:
    notes_path = Path(".eden/trial/notes.md")
    notes_path.write_text("Stub implementer — no changes made.\n", encoding="utf-8")


if __name__ == "__main__":
    main()
