#!/usr/bin/env python3
"""Print the audit-log ``key=`` fingerprints for every API key in a keys.json file.

Used by ops to map a short 8-char hash seen in ``logs/audit.log`` back to
the operator-friendly ``label`` (and a truncated ``key`` for cross-check).
Output never includes the full secret — only the first 14 characters,
which is enough to spot copy/paste errors when rotating keys.

Usage::

    python3 scripts/show_key_ids.py keys.json
    python3 scripts/show_key_ids.py deploy/keys.json.example
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fapiao_cn.auth.keys import _compute_key_id  # noqa: E402


def _load_entries(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[show_key_ids] {path}: {exc}", file=sys.stderr)
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[show_key_ids] {path}: invalid JSON: {exc}", file=sys.stderr)
        return []
    if not isinstance(parsed, list):
        print(
            f"[show_key_ids] {path}: expected JSON array, got "
            f"{type(parsed).__name__}",
            file=sys.stderr,
        )
        return []
    return [e for e in parsed if isinstance(e, dict)]


def _render_row(entry: dict) -> tuple[str, str, str, str]:
    key = str(entry.get("key", "")).strip()
    label = str(entry.get("label", "")).strip() or "<no-label>"
    declared = str(entry.get("key_id", "")).strip()
    if not key:
        return label, "-", "(no key)", declared or "-"
    computed = _compute_key_id(key)
    truncated_key = key if len(key) <= 14 else key[:14] + "…"
    status = "match" if not declared or declared == computed else "MISMATCH"
    return label, computed, truncated_key, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print the audit-log key_id (sha256[:8]) for every entry in "
            "keys.json. Helps ops map short hashes seen in audit.log back "
            "to the operator label."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="keys.json",
        help="Path to keys.json (default: ./keys.json)",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    entries = _load_entries(path)
    if not entries:
        return 1

    print(f"# {path}  ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})")
    print(f"{'label':<18} {'key_id':<10} {'key (trunc)':<16} {'status'}")
    print("-" * 64)
    for entry in entries:
        label, kid, ktrunc, status = _render_row(entry)
        print(f"{label:<18} {kid:<10} {ktrunc:<16} {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())