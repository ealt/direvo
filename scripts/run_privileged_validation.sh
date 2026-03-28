#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-targeted}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for privileged validation" >&2
  exit 1
fi

pytest_args=(tests/test_permission_boundary.py -vv)
if [[ "$mode" == "full" ]]; then
  pytest_args=(-q)
elif [[ "$mode" != "targeted" ]]; then
  echo "usage: $(basename "$0") [targeted|full]" >&2
  exit 2
fi

docker run --rm \
  -v "$repo_root":/repo \
  -w /repo \
  python:3.12-slim \
  bash -lc "
    set -euo pipefail
    apt-get update
    apt-get install -y --no-install-recommends git passwd
    python3 -m pip install --no-cache-dir -e . pytest PyYAML
    DIREVO_RUN_PRIVILEGED_TESTS=1 PYTHONPATH=src python3 -m pytest ${pytest_args[*]}
  "
