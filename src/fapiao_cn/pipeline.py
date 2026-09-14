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
from fapiao_cn.schemas import InvoiceRecord, PdfRaw
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
    pdf_raw: Optional[PdfRaw]
    warnings: List[str]


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
    if not pages:
        return ParseResult(
            record=InvoiceRecord(),
            pdf_raw=PdfRaw(text="", lines=[], page_count=0) if with_raw else None,
            warnings=["empty_pdf"],
        )

    parser_name, parser_func = dispatch(pages[0].text)

    per_page_records = [
        parser_func(
            page.text,
            {"page_index": i, "parser_name": parser_name, "page": page},
        )
        for i, page in enumerate(pages)
    ]

    record = _aggregate(per_page_records, file_label="", page_count=len(pages))

    pdf_raw: Optional[PdfRaw] = None
    if with_raw:
        all_texts = "\n".join(p.text for p in pages)
        pdf_raw = PdfRaw(
            text=all_texts,
            lines=normalize_lines(all_texts),
            page_count=len(pages),
        )

    return ParseResult(record=record, pdf_raw=pdf_raw, warnings=[])


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