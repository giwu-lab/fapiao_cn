#!/usr/bin/env bash
# scripts/cli.sh — start / stop / restart / test the fapiao_cn HTTP service.
#
# This is a thin wrapper that drives the ``fapiao_cn`` console script inside
# the in-tree ``parse_vevn/`` virtualenv.  Use it when you haven't
# activated the venv by hand.  Prefer ``fapiao_cn <sub>`` from an active
# venv when possible.
#
# Usage:
#     bash scripts/cli.sh start   [PORT]
#     bash scripts/cli.sh stop
#     bash scripts/cli.sh restart [PORT]
#     bash scripts/cli.sh test
#     bash scripts/cli.sh status

set -euo pipefail

VENV_DIR="parse_vevn"
HOST="${FC_HOST:-0.0.0.0}"
PORT="${PORT:-${FC_PORT:-8091}}"

activate_venv() {
    if [ -f "$VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
    else
        echo "[cli] virtualenv missing — running scripts/setup.sh first"
        bash "$(dirname "$0")/setup.sh" install
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
    fi
}

cmd_start() {
    activate_venv
    PORT="$PORT" python -m fapiao_cn start --host "$HOST" --port "$PORT"
}

cmd_stop() {
    activate_venv
    python -m fapiao_cn stop
}

cmd_test() {
    activate_venv
    python -m fapiao_cn test
}

cmd_status() {
    activate_venv
    python -m fapiao_cn status
}

case "${1:-help}" in
    start)   shift; PORT="${1:-$PORT}"; cmd_start ;;
    stop)    cmd_stop ;;
    restart) shift; PORT="${1:-$PORT}"; cmd_stop; sleep 1; cmd_start ;;
    test)    cmd_test ;;
    status)  cmd_status ;;
    *)       echo "Usage: $0 {start [PORT] | stop | restart [PORT] | test | status}" ;;
esac