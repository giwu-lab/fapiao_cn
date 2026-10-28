#!/usr/bin/env python3
"""Reorganise ``samples/`` into per-invoice-type subdirectories.

Calls ``scripts/classify_samples.py`` to authoritatively decide where
each file belongs, mirrors the tree under each target subdirectory and
either prints the plan (``--check``) or moves the files (``--apply``).
Also writes ``samples/SAMPLES_INDEX.md`` describing the final layout.

Layout (keyed by parser name; ``samples/报关单/`` already exists and is
left untouched because the parser is not yet registered):

    新电子发票-标准/         parser "new"      (excluding 购_油品_02.pdf)
    新电子发票-旧格式/       parser "old"      (购_材料_02.pdf)
    新电子发票-平台下载/     parser "platform" (标签块在上、值块在下)
    成品油合并代码/          parser "merged_code"
    铁路电子客票/            parser "train"
    航空运输电子客票/        parser "airline"
    中央非税收入统一票据/    parser "non_tax_receipt"
    全电发票-XML/            parser "xml_invoice"

    报关单/                  UNSUPPORTED (no parser yet — leaves as-is)

The mapping is hard-coded here rather than inferred from the parsers module
itself, because the directory names are a *product* decision
(Chinese-language labels grouped by ticket kind, not by parser key) and
should remain stable even if internal parser names change.

Exit codes:
    0  success
    1  --apply refused (target already populated by a different file, or
       ``samples`` dry-run found unmapped files)
    2  bad CLI arguments
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = REPO_ROOT / "samples"

# parser_key -> target subdirectory name (relative to samples/)
LAYOUT: dict[str, str] = {
    "new": "新电子发票-标准",
    "old": "新电子发票-旧格式",
    "platform": "新电子发票-平台下载",
    "merged_code": "成品油合并代码",
    "train": "铁路电子客票",
    "airline": "航空运输电子客票",
    "non_tax_receipt": "中央非税收入统一票据",
    "xml_invoice": "全电发票-XML",
    "ofd": "全电发票-OFD",
}

# parser_key -> human-readable description (matches get_format_descriptions)
DESCRIPTIONS: dict[str, str] = {
    "new": (
        "电子发票（增值税专用发票/普通发票）- 标准新格式"
        "（含京东、水费、通行费变体）"
    ),
    "old": "电子发票（普通发票）- 旧格式（含密码区、校验码）",
    "platform": (
        "电子发票（普通发票）- 平台下载版"
        "（票面标签块在上、值块在下，按词坐标重建阅读顺序）"
    ),
    "merged_code": "电子发票（普通发票）- 合并代码/成品油变体",
    "train": "电子发票（铁路电子客票）",
    "airline": "电子发票（航空运输电子客票行程单）",
    "non_tax_receipt": (
        "中央非税收入统一票据（电子）（如专利年费、行政事业性收费）"
    ),
    "xml_invoice": "XML 全电发票",
    "ofd": "OFD 全电发票（GB/T 33190 数电票 OFD 版）",
}


def _target_subdir(parser_key: str) -> str | None:
    return LAYOUT.get(parser_key)


def _plan(samples_dir: Path = SAMPLES_DIR) -> list[tuple[Path, Path, str]]:
    """Return ``[(current_path, target_path, parser_key), ...]`` for every
    sample that needs moving. Already-correctly-placed files are not
    included in the plan.
    """

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.classify_samples import collect  # noqa: E402

    plan: list[tuple[Path, Path, str]] = []
    for rec in collect(samples_dir):
        rel = Path(rec["path"])
        parser_key = rec["parser"]
        if parser_key in {"UNSUPPORTED", "ERROR"}:
            continue
        target_name = _target_subdir(parser_key)
        if target_name is None:
            print(
                f"[sync_samples_layout] {rel}: unmapped parser "
                f"{parser_key!r}; skipping",
                file=sys.stderr,
            )
            continue
        current = samples_dir / rel
        target = samples_dir / target_name / rel.name
        if current.resolve() == target.resolve():
            continue
        plan.append((current, target, parser_key))
    return plan


def _apply(plan: list[tuple[Path, Path, str]]) -> list[tuple[Path, Path, str]]:
    """Execute ``plan``: create subdirs, move files. Return moves done."""

    if not plan:
        return []
    moved: list[tuple[Path, Path, str]] = []
    for src, dst, parser_key in plan:
        if dst.exists():
            print(
                f"[sync_samples_layout] REFUSE: target exists {dst} "
                f"(source {src})",
                file=sys.stderr,
            )
            raise SystemExit(1)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append((src, dst, parser_key))
    return moved


def _format_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KiB"
    return f"{num / (1024 * 1024):.1f} MiB"


def _render_index(samples_dir: Path = SAMPLES_DIR) -> str:
    """Build the SAMPLES_INDEX.md body. Re-collects from disk so the
    output reflects the *actual* final state after --apply."""

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.classify_samples import collect  # noqa: E402

    records = collect(samples_dir)
    by_dir: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        rel = Path(rec["path"])
        parent = rel.parent.as_posix() if rel.parent != Path(".") else ""
        by_dir[parent].append(rec)

    lines: list[str] = []
    lines.append("# Samples Index")
    lines.append("")
    lines.append("> Auto-generated by `scripts/sync_samples_layout.py`.")
    lines.append(
        "> Each sample is dispatched through "
        "`fapiao_cn.parsers.dispatch` and grouped by ticket kind."
    )
    lines.append("")

    grand_total = len(records)
    grand_bytes = sum(r["size"] for r in records)
    lines.append(f"**Total**: {grand_total} files, {_format_size(grand_bytes)}.")
    lines.append("")

    # Order: parser-keyed dirs first (by LAYOUT), then 报关单/ (unsupported),
    # then any leftover top-level files (shouldn't happen after --apply).
    ordered_dirs: list[tuple[str, str]] = []
    for parser_key, subdir in LAYOUT.items():
        ordered_dirs.append((subdir, parser_key))
    if "报关单" in by_dir:
        ordered_dirs.append(("报关单", "UNSUPPORTED"))
    leftover = sorted(d for d in by_dir if d and d not in {d0 for d0, _ in ordered_dirs})
    for d in leftover:
        ordered_dirs.append((d, "?"))

    for subdir, parser_key in ordered_dirs:
        recs = by_dir.get(subdir, [])
        if not recs:
            continue
        lines.append(f"## `{subdir}/`")
        lines.append("")
        if parser_key == "UNSUPPORTED":
            lines.append(
                "**Parser**: _none registered yet — currently unhandled "
                "by the pipeline._"
            )
        elif parser_key == "?":
            lines.append(f"**Parser**: {parser_key}")
        else:
            lines.append(f"**Parser**: `{parser_key}` — {DESCRIPTIONS.get(parser_key, '')}")
        sub_bytes = sum(r["size"] for r in recs)
        lines.append(f"**Files**: {len(recs)}, {_format_size(sub_bytes)}.")
        lines.append("")
        lines.append("| File | Size | mtime (UTC) |")
        lines.append("| --- | ---: | --- |")
        for r in sorted(recs, key=lambda x: x["path"]):
            name = Path(r["path"]).name
            lines.append(f"| `{name}` | {_format_size(r['size'])} | {r['mtime']} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Files under `samples/` are real-world invoices and are excluded "
        "from git via `.gitignore`."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reorganise samples/ into per-invoice-type subdirectories "
            "(dry-run by default)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files (default: print plan only)",
    )
    parser.add_argument(
        "--regen-index",
        action="store_true",
        help="(Re)write samples/SAMPLES_INDEX.md from current layout",
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
            f"[sync_samples_layout] samples dir not found: {args.samples_dir}",
            file=sys.stderr,
        )
        return 2

    plan = _plan(args.samples_dir)

    if args.regen_index:
        body = _render_index(args.samples_dir)
        index_path = args.samples_dir / "SAMPLES_INDEX.md"
        index_path.write_text(body, encoding="utf-8")
        print(f"[sync_samples_layout] wrote {index_path}")
        return 0

    if not plan:
        print(
            "[sync_samples_layout] nothing to move — already organised.",
            file=sys.stderr,
        )
    else:
        for src, dst, parser_key in plan:
            rel_src = src.relative_to(args.samples_dir)
            rel_dst = dst.relative_to(args.samples_dir)
            print(f"{rel_src} -> {rel_dst}  [{parser_key}]")

    if args.apply:
        moved = _apply(plan)
        print(
            f"[sync_samples_layout] moved {len(moved)} file(s)",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())