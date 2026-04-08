#!/bin/sh
# Backward-compat wrapper. The canonical entrypoint ships inside the package
# at src/eden/docker/entrypoint.sh and is installed during image build.
#
# If eden-container-entrypoint is available (the canonical entrypoint), delegate
# to it. Otherwise fall back to the inline logic for the base Dockerfile which
# does not install the full docker package scripts.
set -eu

if command -v eden-container-entrypoint >/dev/null 2>&1; then
    exec eden-container-entrypoint "$@"
fi

# Fallback: inline logic for the base Dockerfile.
if [ "$#" -eq 0 ]; then
  echo "usage: eden-entrypoint [run|doctor] --config PATH" >&2
  exit 2
fi

if [ "${1#-}" != "$1" ]; then
  set -- run "$@"
fi

command_name="$1"
shift

case "$command_name" in
  run)
    config_path=""
    previous=""
    for argument in "$@"; do
      if [ "$previous" = "--config" ]; then
        config_path="$argument"
        break
      fi
      previous="$argument"
    done

    if [ -z "$config_path" ]; then
      echo "error: --config is required for run" >&2
      exit 2
    fi

    python3 -m eden.runtime --config "$config_path"
    exec python3 -m eden.cli run "$@"
    ;;
  doctor)
    exec python3 -m eden.cli doctor "$@"
    ;;
  *)
    exec "$command_name" "$@"
    ;;
esac
