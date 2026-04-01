#!/usr/bin/env bash
# Build and run the data-fitting demo in Docker.
#
# Usage:
#   ./run.sh                    # build and run
#   ./run.sh --build-only       # just build the image
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE_NAME="direvo-data-fitting-demo"

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

# --- Run ---

echo ""
echo "Starting data-fitting experiment..."
echo "  Planner:     Claude CLI (single session with accumulated context)"
echo "  Implementer: Codex CLI"
echo "  Objective:   maximize R²"
echo "  Parallel:    3 trials"
echo "  Max trials:  15"
echo ""

docker run --rm --privileged \
    -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
    -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
    "$IMAGE_NAME"
