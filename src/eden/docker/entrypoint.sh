#!/bin/sh
# Generic eden container entrypoint.
#
# Phases:
#   1. Auth propagation (make mounted host auth dirs accessible to trial users)
#   2. Background export sync (if /output is mounted and export not disabled)
#   3. Run eden runtime setup + CLI
#   4. Final export on exit
set -eu

# --- Phase 1: Auth propagation ---
# Set env vars that runtime.py, execution.py, and planner.py consume.
export EDEN_AUTH_HOME="${EDEN_AUTH_HOME:-/root}"
export EDEN_RUNTIME_DIR="${EDEN_RUNTIME_DIR:-/tmp/eden-runtime}"

# Source (not execute) auth-setup so its side effects persist in this shell.
if command -v eden-auth-setup >/dev/null 2>&1; then
    . "$(command -v eden-auth-setup)"
fi

# --- Phase 2: Parse arguments ---
if [ "$#" -eq 0 ]; then
    echo "usage: eden-container-entrypoint [run|doctor] --config PATH" >&2
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

        # Runtime setup (user creation, permissions).
        python3 -m eden.runtime --config "$config_path"

        # --- Phase 3: Start eden with optional export sync ---
        export_cmd="${EDEN_EXPORT_COMMAND:-eden-export}"
        export_disabled="${EDEN_EXPORT_DISABLED:-}"
        sync_interval="${EDEN_SYNC_INTERVAL_SEC:-2}"

        # Determine experiment root from config path.
        config_dir=$(dirname "$config_path")
        config_dirname=$(basename "$config_dir")
        if [ "$config_dirname" = ".eden" ]; then
            experiment_root=$(dirname "$config_dir")
        else
            experiment_root="$config_dir"
        fi

        python3 -m eden.cli run "$@" &
        child_pid=$!
        sync_pid=""

        trap 'kill -INT "$child_pid" 2>/dev/null || true' INT
        trap 'kill -TERM "$child_pid" 2>/dev/null || true' TERM

        # Helper to run the export command (handles both simple names and commands with args).
        run_export() {
            if command -v "$export_cmd" >/dev/null 2>&1; then
                "$export_cmd" "$@"
            else
                sh -c "$export_cmd \"\$@\"" -- "$@"
            fi
        }

        # Start background sync loop if /output exists and export not disabled.
        if [ -d /output ] && [ -z "$export_disabled" ]; then
            (
                run_export sync "$experiment_root"
                while kill -0 "$child_pid" 2>/dev/null; do
                    run_export sync "$experiment_root"
                    sleep "$sync_interval"
                done
            ) &
            sync_pid=$!
        fi

        # Wait for eden to finish.
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

        # --- Phase 4: Final export ---
        if [ -n "$sync_pid" ]; then
            kill "$sync_pid" 2>/dev/null || true
            wait "$sync_pid" 2>/dev/null || true
        fi

        if [ -d /output ] && [ -z "$export_disabled" ]; then
            run_export final "$experiment_root"
        fi

        exit "$exit_code"
        ;;
    doctor)
        exec python3 -m eden.cli doctor "$@"
        ;;
    *)
        exec "$command_name" "$@"
        ;;
esac
