#!/bin/sh
# Propagate mounted host auth directories so trial users can reach them.
#
# The orchestrator creates trial-{slot} Unix users and runs the implementer
# via `su trial-{slot} -s /bin/sh -c "..."`.  Without the `-` flag, su
# preserves HOME=/root, so CLIs look for auth in /root/.claude and
# /root/.codex.  We need /root to be traversable and the auth files readable.
set -eu

export EDEN_AUTH_HOME="${EDEN_AUTH_HOME:-/root}"
export EDEN_RUNTIME_DIR="${EDEN_RUNTIME_DIR:-/tmp/eden-runtime}"

mkdir -p "$EDEN_RUNTIME_DIR" 2>/dev/null || true

chmod 711 /root 2>/dev/null || true

# Propagate any auth dirs that exist under /root.
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
