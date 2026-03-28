#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
  echo "usage: direvo-entrypoint [run|doctor] --config PATH" >&2
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

    python3 -m direvo.runtime --config "$config_path"
    exec python3 -m direvo.cli run "$@"
    ;;
  doctor)
    exec python3 -m direvo.cli doctor "$@"
    ;;
  *)
    exec "$command_name" "$@"
    ;;
esac
