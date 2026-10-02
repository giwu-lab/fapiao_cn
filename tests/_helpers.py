"""Shared helpers for tests.

Not a pytest fixture module — these are reusable functions imported
directly by tests (e.g. ``from tests._helpers import parse_pdf``).
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber

from fapiao_cn.parsers import dispatch
from fapiao_cn.parsers.ofd_invoice import parse_ofd_invoice
from fapiao_cn.parsers.xml_invoice import parse_xml_invoice
from fapiao_cn.pipeline import _aggregate, _clean_text, run_customs
from fapiao_cn.schemas import CustomsDeclarationHeader, InvoiceRecord

TEST_DIR = Path(__file__).resolve().parent.parent / "samples"


def parse_pdf(filename: str) -> InvoiceRecord:
    """Parse one PDF / XML / OFD through the matching parser and return
    the record.

    XML files are routed through ``fapiao_cn.parsers.xml_invoice.parse_xml_invoice``
    and OFD through ``fapiao_cn.parsers.ofd_invoice.parse_ofd_invoice`` so
    field semantics stay identical to the API endpoint.
    """

    path = str(TEST_DIR / filename)
    with open(path, "rb") as f:
        contents = f.read()
    if filename.lower().endswith(".xml") or contents[:5] == b"<?xml":
        return parse_xml_invoice(contents)
    if filename.lower().endswith(".ofd") or _looks_like_ofd(contents):
        return parse_ofd_invoice(contents)
    with pdfplumber.open(path) as pdf:
        page_data = [(_clean_text(p.extract_text() or ""), p.extract_words()) for p in pdf.pages]
    texts = [t for t, _ in page_data]
    words_list = [w for _, w in page_data]
    parser_name, parser_func = dispatch(texts[0])
    n = len(texts)
    ctx_list = [
        {"page_index": i, "parser_name": parser_name, "words": words_list[i]}
        for i in range(n)
    ]
    recs = [parser_func(t, ctx) for t, ctx in zip(texts, ctx_list)]
    return _aggregate(recs, file_label=filename, page_count=n)


def parse_customs_pdf(filename: str) -> CustomsDeclarationHeader:
    """Parse a 海关出口货物报关单 PDF through the customs pipeline."""

    path = str(TEST_DIR / filename)
    with open(path, "rb") as f:
        contents = f.read()
    return run_customs(contents, file_label=filename)


def _looks_like_ofd(contents: bytes) -> bool:
    return contents[:2] == b"PK" and b"OFD.xml" in contents[: 2 * 1024 * 1024]