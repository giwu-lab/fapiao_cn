"""Pipeline: open PDF with pdfplumber, iterate pages, dispatch parser.

The ``PageView`` wrapper exposes a per-page text view that lazily
extracts ``extract_words()`` only when a parser actually asks for it.
That avoids paying the cost of the layout-based word extraction for
PDFs where no parser needs it (the typical case — most new e-invoice
parsers operate on plain text + line geometry only).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

import pdfplumber

from fapiao_cn.config import settings
from fapiao_cn.expense_category import classify_expense
from fapiao_cn.parsers import dispatch
from fapiao_cn.schemas import InvoiceRecord, PdfRaw
from fapiao_cn.utils import normalize_invoice_text, normalize_lines

if TYPE_CHECKING:
    from pdfplumber.page import Page


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

    实现下沉到 :func:`fapiao_cn.utils.normalize_invoice_text` —— 平台下载版
    电子发票解析器要按词坐标重建文本，重建后的文本也要走同一套规整，
    放在 ``utils`` 里可以避开 ``pipeline`` ↔ ``parsers`` 的循环导入。
    """

    return normalize_invoice_text(text)


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
    # 消费类别分类：默认按 FC_EXPENSE_CATEGORY_ENABLED 控制；分类规则
    # 会按 L1 票种 → L2 卖方关键词 → L3 明细 → L4 税收编码 命中。
    cat, label, source = classify_expense(
        record,
        parser_name=parser_name,
        enabled=settings.expense_category_enabled,
    )
    record.expense_category = cat
    record.expense_category_label = label
    record.expense_category_source = source

    pdf_raw = PdfRaw()
    if with_raw:
        pdf_raw = PdfRaw(
            text=full_text,
            lines=normalize_lines(full_text),
            page_count=len(pages),
        )

    return ParseResult(record=record, pdf_raw=pdf_raw, message="success")


# 报关单（海关出口货物报关单）解析**不在本模块**：它是企业版能力，实现随
# fapiao_cn_ee 发行包发布（见 fapiao_cn.plugins 的插件契约）。这里只保留
# 页面加载的公开入口，供企业版插件复用同一套页面视图与文本清洗逻辑。


def load_pages(contents: bytes) -> List[PageView]:
    """打开 PDF 并返回页面视图列表。

    企业版的报关单 pipeline 需要同样的页面视图（同一套文本清洗、同样
    的懒加载 words），所以把内部实现公开出来，避免两个包各写一份。
    """
    return _load_pages(contents)


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