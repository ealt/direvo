"""Execution agent for the seed-sum experiment.

Reads the seed from the trial plan and appends it to seeds.md.
"""

import logging

logger = logging.getLogger("experiment")


def parse_plan() -> str:
    """Parse the seed from the trial plan document."""
    with open(".direvo/trial/plan.md") as file:
        return file.read().split()[-1].strip()


def execute_plan(seed: str) -> None:
    """Append the seed to the workspace seed list."""
    with open("seeds.md", "a") as file:
        file.write(f"{seed}\n")


def main() -> None:
    """Main entry point."""
    seed = parse_plan()
    execute_plan(seed)
    logger.info("appended seed: %s", seed)


if __name__ == "__main__":
    main()
