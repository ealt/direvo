#!/bin/sh
# Propagate mounted host auth directories so trial users can reach them.
#
# The orchestrator creates trial-{slot} Unix users and runs the implementer
# (Codex) via `su trial-{slot} -s /bin/sh -c "..."`.  Without the `-` flag,
# su preserves HOME=/root, so CLIs look for auth in /root/.claude and
# /root/.codex.  We need /root to be traversable and the auth files readable.
set -eu

export DIREVO_AUTH_HOME=/root
export DIREVO_RUNTIME_DIR=/tmp/direvo-runtime

mkdir -p "$DIREVO_RUNTIME_DIR" 2>/dev/null || true

chmod 711 /root 2>/dev/null || true

for dir in /root/.claude /root/.codex /root/.config/claude \
           /root/.local/state/claude /root/.local/share/claude \
           /root/.cache/claude; do
    if [ -d "$dir" ]; then
        chmod -R a+rX "$dir" 2>/dev/null || true
    fi
done

for file in /root/.claude.json; do
    if [ -f "$file" ]; then
        chmod a+r "$file" 2>/dev/null || true
    fi
done

sync_results() {
    if [ ! -d /output ]; then
        return
    fi

    mkdir -p /output/.direvo 2>/dev/null || true
    mkdir -p /output/planner/.direvo 2>/dev/null || true

    # Mirror runtime state for live host-side inspection while the demo runs.
    cp -a /experiment/.direvo/. /output/.direvo/ 2>/dev/null || true
    cp /experiment/planner/.direvo/proposals.db /output/planner/.direvo/ 2>/dev/null || true
    cp /experiment/planner/.direvo/proposals.db-wal /output/planner/.direvo/ 2>/dev/null || true
    cp /experiment/planner/.direvo/proposals.db-shm /output/planner/.direvo/ 2>/dev/null || true
    cp -a /experiment/planner/.direvo/proposals /output/planner/.direvo/ 2>/dev/null || true
}

export_results() {
    sync_results
    (cd /experiment/planner/workspace && git bundle create /output/workspace.bundle --all 2>/dev/null) || true
}

sync_output_loop() {
    interval="${DIREVO_SYNC_INTERVAL_SEC:-2}"
    while kill -0 "$child_pid" 2>/dev/null; do
        sync_results
        sleep "$interval"
    done
}

direvo-entrypoint "$@" &
child_pid=$!
sync_pid=""

trap 'kill -INT "$child_pid" 2>/dev/null || true' INT
trap 'kill -TERM "$child_pid" 2>/dev/null || true' TERM

if [ -d /output ]; then
    sync_results
    sync_output_loop &
    sync_pid=$!
fi

exit_code=0
while :; do
    if wait "$child_pid"; then
        exit_code=0
        break
    fi
    exit_code=$?
    if ! kill -0 "$child_pid" 2>/dev/null; then
        break
    fi
done

if [ -n "$sync_pid" ]; then
    kill "$sync_pid" 2>/dev/null || true
    wait "$sync_pid" 2>/dev/null || true
fi

export_results
exit "$exit_code"
