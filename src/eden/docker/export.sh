#!/bin/sh
# Default eden export script.
#
# Copies .eden/ artifacts to /output and creates a git bundle of the workspace.
#
# Usage:
#   eden-export sync  <experiment_root>   — periodic sync during run
#   eden-export final <experiment_root>   — final export on exit
set -eu

MODE="${1:-sync}"
EXPERIMENT_ROOT="${2:-/experiment}"

if [ ! -d /output ]; then
    exit 0
fi

sync_results() {
    mkdir -p /output/.eden 2>/dev/null || true
    mkdir -p /output/planner/.eden 2>/dev/null || true

    # Mirror runtime state for live host-side inspection.
    cp -a "$EXPERIMENT_ROOT/.eden/." /output/.eden/ 2>/dev/null || true

    planner_eden="$EXPERIMENT_ROOT/planner/.eden"
    if [ -d "$planner_eden" ]; then
        cp "$planner_eden/proposals.db" /output/planner/.eden/ 2>/dev/null || true
        cp "$planner_eden/proposals.db-wal" /output/planner/.eden/ 2>/dev/null || true
        cp "$planner_eden/proposals.db-shm" /output/planner/.eden/ 2>/dev/null || true
        cp -a "$planner_eden/proposals" /output/planner/.eden/ 2>/dev/null || true
    fi
}

export_final() {
    sync_results

    workspace="$EXPERIMENT_ROOT/planner/workspace"
    if [ -e "$workspace/.git" ]; then
        (cd "$workspace" && git bundle create /output/workspace.bundle --all 2>/dev/null) || true
    fi
}

case "$MODE" in
    sync)
        sync_results
        ;;
    final)
        export_final
        ;;
    *)
        echo "usage: eden-export sync|final <experiment_root>" >&2
        exit 2
        ;;
esac
