#!/bin/sh
# Propagate mounted host auth directories so trial users can reach them.
#
# The orchestrator creates trial-{slot} Unix users and runs the implementer
# (Codex) via `su trial-{slot} -s /bin/sh -c "..."`.  Without the `-` flag,
# su preserves HOME=/root, so CLIs look for auth in /root/.claude and
# /root/.codex.  We need /root to be traversable and the auth files readable.
set -eu

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

export_results() {
    if [ ! -d /output ]; then
        return
    fi

    cp -a /experiment/.direvo /output/ 2>/dev/null || true
    mkdir -p /output/planner/.direvo 2>/dev/null || true
    cp /experiment/planner/.direvo/proposals.db /output/planner/.direvo/ 2>/dev/null || true
    cp -a /experiment/planner/.direvo/proposals /output/planner/.direvo/ 2>/dev/null || true
    (cd /experiment/planner/workspace && git bundle create /output/workspace.bundle --all 2>/dev/null) || true
}

direvo-entrypoint "$@" &
child_pid=$!

trap 'kill -INT "$child_pid" 2>/dev/null || true' INT
trap 'kill -TERM "$child_pid" 2>/dev/null || true' TERM

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

export_results
exit "$exit_code"
