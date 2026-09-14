#!/usr/bin/env bash
# scripts/build-portable.sh — build the offline portable tarball.
#
# Delegates to ``build-scripts/package_portable.py`` which does the
# heavy lifting (wheel pre-download → simulated offline install →
# tarball packaging → wheels/ cleanup).
#
# Usage:
#     bash scripts/build-portable.sh                # → fapiao_cn-portable.tar.gz
#     bash scripts/build-portable.sh --output other.tar.gz
#     bash scripts/build-portable.sh --skip-tests  # skip pre-package pytest
#     bash scripts/build-portable.sh --skip-smoke  # skip the /health smoke test
#
# Environment overrides:
#     PYTHON      — interpreter to use (default: ./parse_vevn/bin/python if it
#                   exists, else python3)
#     WHEELS_DIR  — where to stage wheels (default: ./wheels, deleted after)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ -z "${PYTHON:-}" ] && [ -x "$REPO_ROOT/parse_vevn/bin/python" ]; then
    PYTHON="$REPO_ROOT/parse_vevn/bin/python"
fi
PYTHON="${PYTHON:-python3}"

exec "$PYTHON" "build-scripts/package_portable.py" "$@"