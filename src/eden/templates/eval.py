"""Stub evaluator for an EDEN experiment.

The evaluator runs after the implementer commits its changes. It should
read the workspace state, compute metrics, and print a JSON object to
stdout with keys matching the metrics_schema in config.yaml.

This script runs from the trial worktree directory.
"""

import json


def main() -> None:
    # TODO: Replace with your evaluation logic.
    # Read the workspace state and compute metrics.
    metrics = {"score": 0.0}
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
