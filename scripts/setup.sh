#!/usr/bin/env bash
# scripts/setup.sh — create the in-tree venv and install fapiao_cn (editable).
#
# Usage:
#     bash scripts/setup.sh           # install (idempotent)
#     bash scripts/setup.sh reinstall # blow away parse_vevn/ and re-create
#
# The ``parse_vevn/`` virtualenv is what the portable deployment ships
# with; everything offline assumes this script has been run on the
# source machine first.

set -euo pipefail

VENV_DIR="parse_vevn"

cmd_install() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "[setup] Creating virtualenv ..."
        python3 -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    python -m pip install --upgrade pip wheel
    echo "[setup] Installing fapiao_cn (editable) + dev extras ..."
    # ``.[dev]`` is not an extra we declare; we install both the package
    # itself and the [dependency-groups] dev deps separately.
    python -m pip install -e .
    python -m pip install pytest
    echo "[setup] Done."
}

cmd_reinstall() {
    rm -rf "$VENV_DIR"
    cmd_install
}

case "${1:-install}" in
    install)   cmd_install ;;
    reinstall) cmd_reinstall ;;
    *)         echo "Usage: $0 {install|reinstall}" ;;
esac