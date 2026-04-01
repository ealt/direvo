"""Evaluation script for the seed-sum experiment.

Reads seeds from seeds.md, maps each to a deterministic random int,
and outputs the sum as a JSON metric.
"""

from __future__ import annotations

import json
import os
import random

_LOG_DIR = os.environ.get("EDEN_LOG_DIR")


def _log(**fields: object) -> None:
    """Append a JSON log line to eval.log if EDEN_LOG_DIR is set."""
    if _LOG_DIR is None:
        return
    with open(os.path.join(_LOG_DIR, "eval.log"), "a") as f:
        f.write(json.dumps(fields, sort_keys=True) + "\n")


def eval_trial() -> int:
    """Evaluate the current workspace state."""
    with open("seeds.md") as file:
        seeds = file.read().splitlines()

    def random_int(seed: str) -> int:
        random.seed(seed)
        return random.randint(0, 100)

    values = list(map(random_int, seeds))
    score = sum(values)
    _log(event="eval", seeds=seeds, values=values, score=score)
    return score


def main() -> None:
    """Main entry point."""
    score = eval_trial()
    print(json.dumps({"score": score}))


if __name__ == "__main__":
    main()
