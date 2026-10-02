"""Pipeline: open PDF with pdfplumber, iterate pages, dispatch parser.

The ``PageView`` wrapper exposes a per-page text view that lazily
extracts ``extract_words()`` only when a parser actually asks for it.
That avoids paying the cost of the layout-based word extraction for
PDFs where no parser needs it (the typical case — most new e-invoice
parsers operate on plain text + line geometry only).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

import pdfplumber

from fapiao_cn.parsers import dispatch
from fapiao_cn.parsers.customs_declaration import parse_customs_declaration
from fapiao_cn.schemas import CustomsDeclarationHeader, InvoiceRecord, PdfRaw
from fapiao_cn.utils import normalize_lines

if TYPE_CHECKING:
    from pdfplumber.page import Page

# Kangxi radical variants that pdfplumber sometimes emits.
# (Add new codepoints to this dict when they appear in the wild.)
_KANGXI_TO_CJK = {
    0x2F26: "\u5b50",  # ⼦ → 子
}
_KANGXI_TRANSLATE = str.maketrans(
    {chr(src): dst for src, dst in _KANGXI_TO_CJK.items()}
)

# 京东大药房等变体的表头/标签字符在 pdfplumber 输出中会被连续重复
# 落字（如 "购购购 销销销 买买买 售售售"、"发发发票票票号号号码码码：：："）。
# 折叠为单字后现有解析器正则原地生效。仅合并 3+ 连续同字，避开
# "滴滴"、"敌敌畏"、"税款"、"家家" 等合法的双重字。
# 字符类覆盖 CJK 表意文字 + 全角符号（含 ：， ；， （）， 等京东大药房
# 重复出现的全角冒号）。
_RE_REPEAT_CJK = re.compile(r"([\u4e00-\u9fff\uff00-\uffef])\1{2,}")


@dataclass
class ParseResult:
    record: InvoiceRecord
    pdf_raw: PdfRaw
    message: str = "success"


class PageView:
    """Per-page view passed to parsers via ``ctx``.

    Holds the cleaned text eagerly (it is needed for parser dispatch and
    is cheap).  ``extract_words()`` is deferred to the first call to
    ``.words()``; subsequent calls reuse the cached result.  This keeps
    the cost of layout-based word extraction out of the common path
    where no parser needs the coordinates.
    """

    __slots__ = ("_page", "_text", "_words_cache")

    def __init__(self, page: "Page", clean_text: str) -> None:
        self._page = page
        self._text = clean_text
        self._words_cache: Optional[list] = None

    @property
    def text(self) -> str:
        return self._text

    def words(self) -> list:
        """Return the page's word list, extracting it on first call."""
        if self._words_cache is None:
            self._words_cache = self._page.extract_words()
        return self._words_cache


def _clean_text(text: str) -> str:
    """Normalize the text so that regex anchors behave deterministically.

    pdfplumber's default output uses single spaces between glyphs that
    share a row. We keep that structure but collapse runs of spaces
    caused by line breaks inside paragraphs.
    """

    if not text:
        return ""
    # Replace NBSP / fullwidth spaces, then normalise Kangxi radicals.
    text = text.replace("\u3000", " ").replace("\xa0", " ").translate(_KANGXI_TRANSLATE)
    # Keep \n as line separator; collapse multi-space inside a line.
    out_lines = []
    for ln in text.splitlines():
        ln = " ".join(ln.split())
        if ln:
            ln = _RE_REPEAT_CJK.sub(r"\1", ln)
            out_lines.append(ln)
    return "\n".join(out_lines)


def _load_pages(contents: bytes) -> List[PageView]:
    """Open the PDF and return a list of per-page ``PageView``s.

    Word lists are **not** extracted here — they are pulled on demand
    via ``PageView.words()`` the first time a parser needs them.
    """

    pages: List[PageView] = []
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page in pdf.pages:
            pages.append(PageView(page, _clean_text(page.extract_text() or "")))
    return pages


def run(contents: bytes, *, with_raw: bool = False) -> ParseResult:
    pages = _load_pages(contents)
    if not pages or all(not p.text for p in pages):
        return ParseResult(
            record=InvoiceRecord(),
            pdf_raw=PdfRaw(text="", lines=[], page_count=len(pages)),
            message="not_text",
        )

    parser_name, parser_func = dispatch(pages[0].text)

    # Aggregate text across all pages so parsers that span pages (e.g.
    # 海关出口货物报关单 出口退税联 with 24 items) can read it via
    # ``ctx["full_text"]``.  Parsers that only need a single page (the
    # default) ignore this key.
    full_text = "\n".join(p.text for p in pages)

    per_page_records = [
        parser_func(
            page.text,
            {
                "page_index": i,
                "parser_name": parser_name,
                "page": page,
                "full_text": full_text,
                "page_count": len(pages),
            },
        )
        for i, page in enumerate(pages)
    ]

    record = _aggregate(per_page_records, file_label="", page_count=len(pages))

    pdf_raw = PdfRaw()
    if with_raw:
        pdf_raw = PdfRaw(
            text=full_text,
            lines=normalize_lines(full_text),
            page_count=len(pages),
        )

    return ParseResult(record=record, pdf_raw=pdf_raw, message="success")


# ── Customs declaration (报关单) pipeline ──
#
# Mirrors ``run`` but produces a ``CustomsDeclarationHeader``.  The
# per-page guard inside ``parse_customs_declaration`` returns an empty
# record for any non-报关单 page (e.g. 放行条 in merged documents), so
# pages where the title is absent contribute nothing to the aggregate.
#
# Multi-page 出口退税联 (e.g. 24 items across 6 pages) gets ``full_text``
# injected into ``ctx`` so the parser can scan every page's items in a
# single pass and return a complete ``items[]`` on each per-page record.
# The aggregator then dedupes items by ``(item_no, hs_code, total_price)``
# so the same item emitted on every page survives exactly once.

_CUSTOMS_RECORDS_FIELDS = (
    "declaration_variant",
    "direction",
    "customs_no",
    "pre_entry_no",
    "record_no",
    "export_date",
    "declaration_date",
    "print_date",
    "shipper_name",
    "shipper_tax_id",
    "consignee_name",
    "consignee_tax_id",
    "producer_name",
    "producer_tax_id",
    "declarant_name",
    "declarant_tax_id",
    "customs_office_code",
    "customs_office_name",
    "transport_mode",
    "transport_mode_code",
    "vessel_or_flight",
    "bill_of_lading_no",
    "supervision_mode_code",
    "supervision_mode_name",
    "tax_exemption_code",
    "tax_exemption_name",
    "settlement_method",
    "license_no",
    "approval_no",
    "contract_no",
    "trade_country_code",
    "trade_country_name",
    "destination_country_code",
    "destination_country_name",
    "destination_port_code",
    "destination_port_name",
    "exit_port_code",
    "exit_port_name",
    "source_region_code",
    "source_region_name",
    "package_type_code",
    "package_type_name",
    "packages_count",
    "gross_weight_kg",
    "net_weight_kg",
    "trade_term_code",
    "trade_term_name",
    "freight",
    "insurance",
    "misc_charges",
    "attached_docs",
    "container_no",
    "container_summary",
    "marks_and_remarks",
    "declarant_person",
    "declarant_person_id",
    "declarant_phone",
    "currency",
    "total_amount",
)


def run_customs(contents: bytes, *, file_label: str = "") -> CustomsDeclarationHeader:
    """Run the customs declaration parser across all pages and return a
    single ``CustomsDeclarationHeader`` aggregated from per-page
    results.

    The function returns an empty record when the PDF does not look like
    a 报关单 at all; callers can detect this via
    ``record.customs_no == ""``.
    """

    pages = _load_pages(contents)
    if not pages or all(not p.text for p in pages):
        rec = CustomsDeclarationHeader()
        rec.page_count = len(pages)
        rec.message = "not_text"
        rec.file = file_label
        return rec

    full_text = "\n".join(p.text for p in pages)

    per_page_records = [
        parse_customs_declaration(
            page.text,
            {
                "page_index": i,
                "parser_name": "customs_declaration",
                "page": page,
                "full_text": full_text,
                "page_count": len(pages),
            },
        )
        for i, page in enumerate(pages)
    ]

    return _summarize_customs(per_page_records, page_count=len(pages), file_label=file_label)


def _summarize_customs(
    records: List[CustomsDeclarationHeader],
    *,
    page_count: int,
    file_label: str,
) -> CustomsDeclarationHeader:
    """Aggregate per-page ``CustomsDeclarationHeader`` records.

    String fields use ``first_non_empty`` (headers repeat on every
    page).  ``items`` are de-duped by ``(item_no, hs_code, total_price)``
    — each per-page record carries the same full list (extracted from
    ``full_text``), so dedup by natural key keeps one entry per logical
    item across multi-page 退税联 declarations.  ``total_amount`` is
    pulled from the first non-empty record (every page emits the same
    cumulative total since it is derived from ``full_text``).
    """

    out = CustomsDeclarationHeader()
    out.page_count = page_count
    out.file = file_label

    def first_non_empty(field: str) -> str:
        for r in records:
            v = getattr(r, field, "")
            if v:
                return v
        return ""

    for field in _CUSTOMS_RECORDS_FIELDS:
        setattr(out, field, first_non_empty(field))

    seen: set = set()
    merged_items: List = []
    for r in records:
        for it in r.items:
            key = (it.item_no, it.hs_code, it.total_price)
            if key in seen:
                continue
            seen.add(key)
            merged_items.append(it)
    out.items = merged_items
    out.item_count = len(merged_items)

    # Re-derive total_amount from merged items across the whole PDF.
    try:
        total = sum(float(t.total_price or 0) for t in merged_items)
        if total > 0:
            out.total_amount = f"{total:.2f}"
    except ValueError:
        pass

    return out


def _aggregate(records: list, file_label: str, page_count: int) -> InvoiceRecord:
    """Pick the right per-page values, then build the final record."""

    def first_non_empty(field: str) -> str:
        for r in records:
            v = getattr(r, field, "")
            if v:
                return v
        return ""

    def last_non_empty(field: str) -> str:
        for r in reversed(records):
            v = getattr(r, field, "")
            if v:
                return v
        return ""

    # Pick invoice_type from first page (other pages repeat it).
    inv_type = first_non_empty("invoice_type")

    # Invoice number: prefer last page (some duplicates use the more complete one).
    inv_no = last_non_empty("invoice_number") or first_non_empty("invoice_number")

    return InvoiceRecord(
        file=file_label,
        page_count=page_count,
        invoice_type=inv_type,
        invoice_number=inv_no,
        issue_date=first_non_empty("issue_date"),
        buyer_name=first_non_empty("buyer_name"),
        buyer_tax_id=first_non_empty("buyer_tax_id"),
        seller_name=first_non_empty("seller_name"),
        seller_tax_id=first_non_empty("seller_tax_id"),
        amount_without_tax=last_non_empty("amount_without_tax"),
        tax_amount=last_non_empty("tax_amount"),
        total_amount=last_non_empty("total_amount"),
        total_amount_upper=last_non_empty("total_amount_upper"),
        payee=first_non_empty("payee"),
        reviewer=first_non_empty("reviewer"),
        drawer=first_non_empty("drawer"),
    ).stripped()