#!/usr/bin/env python3
"""Classify every sample file under ``samples/`` by which parser would handle it.

Scans every ``.pdf`` and ``.xml`` reachable from ``samples/`` (recursively,
so 报关单/ is included), invokes ``fapiao_cn.parsers.dispatch`` on the
first page of each PDF (XMLs are tagged directly), and emits one record
per file:

    <relative_path>\t<parser_name>\t<size_bytes>\t<mtime_iso>

Defaults to stdout TSV; ``--json`` switches to JSON (one object per line).
Dry-run only. To actually reorganise the directory tree use
``scripts/sync_samples_layout.py`` (which calls this script under the
hood).

Subdirectory ``报关单/`` is flagged ``UNSUPPORTED`` — those customs
declaration forms have no registered parser yet.

Exit codes:
    0  success
    1  unexpected exception (logged to stderr)
    2  bad CLI arguments
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = REPO_ROOT / "samples"
UNSUPPORTED_DIR = "报关单"
UNSUPPORTED_TAG = "UNSUPPORTED"
XML_TAG = "xml_invoice"
OFD_TAG = "ofd"


def _iter_sample_files(samples_dir: Path) -> Iterable[Path]:
    """Yield every PDF/XML/OFD under ``samples_dir`` recursively, sorted."""

    if not samples_dir.is_dir():
        return
    for path in sorted(samples_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".pdf", ".xml", ".ofd"}:
            continue
        yield path


def _classify_one(path: Path) -> str:
    rel = path.relative_to(SAMPLES_DIR)
    if UNSUPPORTED_DIR in rel.parts:
        return UNSUPPORTED_TAG
    if path.suffix.lower() == ".xml":
        return XML_TAG
    if path.suffix.lower() == ".ofd":
        return OFD_TAG

    import pdfplumber

    from fapiao_cn.parsers import dispatch

    with pdfplumber.open(path) as pdf:
        text = pdf.pages[0].extract_text() or ""
    name, _ = dispatch(text)
    return name


def _format_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def collect(samples_dir: Path = SAMPLES_DIR) -> list[dict]:
    """Return ``[{path, parser, size, mtime}, ...]`` for every sample file."""

    out: list[dict] = []
    for path in _iter_sample_files(samples_dir):
        try:
            parser = _classify_one(path)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[classify_samples] {path.relative_to(samples_dir)}: {exc}",
                file=sys.stderr,
            )
            parser = "ERROR"
        stat = path.stat()
        out.append(
            {
                "path": str(path.relative_to(samples_dir)),
                "parser": parser,
                "size": stat.st_size,
                "mtime": _format_mtime(stat.st_mtime),
            }
        )
    return out


def _print_tsv(records: list[dict]) -> None:
    print("path\tparser\tsize\tmtime")
    for r in records:
        print(f"{r['path']}\t{r['parser']}\t{r['size']}\t{r['mtime']}")


def _print_json(records: list[dict]) -> None:
    for r in records:
        print(json.dumps(r, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Classify every sample under samples/ by which parser it would "
            "route to. Dry-run only."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per line instead of TSV",
    )
    parser.add_argument(
        "--samples-dir",
        type=Path,
        default=SAMPLES_DIR,
        help=f"Override the samples root (default: {SAMPLES_DIR})",
    )
    args = parser.parse_args(argv)

    if not args.samples_dir.is_dir():
        print(
            f"[classify_samples] samples dir not found: {args.samples_dir}",
            file=sys.stderr,
        )
        return 2

    records = collect(args.samples_dir)
    if args.json:
        _print_json(records)
    else:
        _print_tsv(records)
    return 0


if __name__ == "__main__":
    sys.exit(main())