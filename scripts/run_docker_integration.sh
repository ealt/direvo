#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$repo_root"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for Docker integration validation" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  EDEN_RUN_DOCKER_TESTS=1 uv run -m pytest -q tests/test_docker_integration.py -vv "$@"
  exit 0
fi

EDEN_RUN_DOCKER_TESTS=1 PYTHONPATH=src python3 -m pytest -q tests/test_docker_integration.py -vv "$@"
