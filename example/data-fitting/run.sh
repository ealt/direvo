#!/usr/bin/env bash
# Build and run the data-fitting demo in Docker.
#
# Authentication: mounts your local Claude and Codex CLI auth directories
# into the container (same pattern as garth).  Log in on the host first:
#   claude auth login
#   codex auth login
#
# Usage:
#   ./run.sh                    # build and run
#   ./run.sh --build-only       # just build the image
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE_NAME="eden-data-fitting-demo"
OUTPUT_DIR="${EDEN_OUTPUT_DIR:-./eden-output-$(date +%Y%m%d-%H%M%S)}"

case "$OUTPUT_DIR" in
    /*) OUTPUT_HOST_DIR="$OUTPUT_DIR" ;;
    *) OUTPUT_HOST_DIR="$(pwd)/$OUTPUT_DIR" ;;
esac

# --- Prerequisites ---

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is required to run this demo." >&2
    exit 1
fi

# --- Build ---

echo "Building Docker image..."
docker build -t "$IMAGE_NAME" -f "$REPO_ROOT/example/data-fitting/Dockerfile" "$REPO_ROOT"

if [ "${1:-}" = "--build-only" ]; then
    echo "Image built: $IMAGE_NAME"
    exit 0
fi

# --- Auth mounts ---
# Mount host CLI auth directories so the container can use existing sessions.
# Follows the same pattern as garth's auth passthrough.

AUTH_MOUNTS=()

# Claude CLI auth
[ -d "$HOME/.claude" ]              && AUTH_MOUNTS+=(-v "$HOME/.claude:/root/.claude")
[ -f "$HOME/.claude.json" ]         && AUTH_MOUNTS+=(-v "$HOME/.claude.json:/root/.claude.json")
[ -d "$HOME/.config/claude" ]       && AUTH_MOUNTS+=(-v "$HOME/.config/claude:/root/.config/claude")
[ -d "$HOME/.local/state/claude" ]  && AUTH_MOUNTS+=(-v "$HOME/.local/state/claude:/root/.local/state/claude")
[ -d "$HOME/.local/share/claude" ]  && AUTH_MOUNTS+=(-v "$HOME/.local/share/claude:/root/.local/share/claude")
[ -d "$HOME/.cache/claude" ]        && AUTH_MOUNTS+=(-v "$HOME/.cache/claude:/root/.cache/claude")

# Codex CLI auth
[ -d "$HOME/.codex" ]               && AUTH_MOUNTS+=(-v "$HOME/.codex:/root/.codex")

if [ ${#AUTH_MOUNTS[@]} -eq 0 ]; then
    echo "Warning: no CLI auth directories found.  Log in first:" >&2
    echo "  claude auth login" >&2
    echo "  codex auth login" >&2
fi

# --- Run ---

echo ""
echo "Starting data-fitting experiment..."
echo "  Planner:     Claude CLI (single session with accumulated context)"
echo "  Implementer: Codex CLI"
echo "  Objective:   maximize R²"
echo "  Parallel:    3 trials"
echo "  Max trials:  15"
echo ""

mkdir -p "$OUTPUT_HOST_DIR"

rc=0
docker run --rm --privileged \
    -v "$OUTPUT_HOST_DIR:/output" \
    "${AUTH_MOUNTS[@]}" \
    "$IMAGE_NAME" || rc=$?

echo ""
echo "Results: $OUTPUT_HOST_DIR/"
exit "$rc"
