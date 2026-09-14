#!/usr/bin/env bash
# scripts/lint.sh — run ruff, mypy, and the test suite over the project.
#
# Usage:
#     bash scripts/lint.sh                  # default checks
#     bash scripts/lint.sh --fix            # let ruff auto-fix what it can
#
# Activates ``parse_vevn/`` if it exists, otherwise runs from the ambient
# environment.  Installs missing tools into the venv if needed.

set -euo pipefail

VENV_DIR="parse_vevn"

if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
fi

ensure_tool() {
    local tool="$1"
    local pkg="$2"
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[lint] $tool not installed; running 'pip install $pkg'"
        python -m pip install --quiet "$pkg"
    fi
}

ensure_tool ruff "ruff"
ensure_tool mypy "mypy"
ensure_tool pytest "pytest"

cd "$(dirname "$0")/.."

echo "=== ruff ==="
ruff check src tests "$@"

echo "=== mypy (advisory) ==="
mypy src || echo "mypy returned non-zero (advisory only)"

echo "=== pytest ==="
exec pytest tests/ -q --tb=short