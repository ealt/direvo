"""Evaluation script for the seed-sum experiment.

Reads seeds from seeds.md, maps each to a deterministic random int,
and outputs the sum as a JSON metric.
"""

import json
import random


def eval_trial() -> int:
    """Evaluate the current workspace state."""
    with open("seeds.md") as file:
        seeds = file.read().splitlines()

    def random_int(seed: str) -> int:
        random.seed(seed)
        return random.randint(0, 100)

    return sum(map(random_int, seeds))


def main() -> None:
    """Main entry point."""
    score = eval_trial()
    print(json.dumps({"score": score}))


if __name__ == "__main__":
    main()
