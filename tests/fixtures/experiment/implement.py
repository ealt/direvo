"""Implementation agent for the seed-sum experiment.

Reads the seed from the trial plan and appends it to seeds.md.
"""

from __future__ import annotations

import json
import os

_LOG_DIR = os.environ.get("EDEN_LOG_DIR")


def _log(**fields: object) -> None:
    """Append a JSON log line to execute.log if EDEN_LOG_DIR is set."""
    if _LOG_DIR is None:
        return
    with open(os.path.join(_LOG_DIR, "execute.log"), "a") as f:
        f.write(json.dumps(fields, sort_keys=True) + "\n")


def _read_seeds() -> list[str]:
    """Read current seeds from seeds.md."""
    with open("seeds.md") as f:
        return [line for line in f.read().splitlines() if line.strip()]


def parse_plan() -> str:
    """Parse the seed from the trial plan document."""
    with open(".eden/trial/plan.md") as file:
        return file.read().split()[-1].strip()


def execute_plan(seed: str) -> None:
    """Append the seed to the workspace seed list."""
    with open("seeds.md", "a") as file:
        file.write(f"{seed}\n")


def main() -> None:
    """Main entry point."""
    seed = parse_plan()
    seeds_before = _read_seeds()
    execute_plan(seed)
    seeds_after = _read_seeds()
    _log(event="execute", seed=seed, seeds_before=seeds_before, seeds_after=seeds_after)


if __name__ == "__main__":
    main()
