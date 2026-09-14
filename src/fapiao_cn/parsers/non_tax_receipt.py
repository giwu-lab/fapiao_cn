"""Central Non-Tax Revenue Unified Receipt (中央非税收入统一票据（电子）) parser.

Covers e.g. 专利_01.pdf — a 国家知识产权局专利局 patent annuity
receipt.  The receipt reuses field names common to fapiao but does not
carry 增值税, so ``tax_amount`` is left empty and the fee subtotal
equals the grand total.

Layout highlights::

    中央非税收入统一票据（电子）
    票据代码:00010126 票据号码：0012090205
    交款人统一社会信用代码:91500106622015389N 校验码:2b5d4a
    交款人:重庆市旺成科技股份有限公司 开票日期:2026-06-29
    项目编码 项目名称 单位 数量 标准 金额（元） 备注
    056990124030发明专利第3年年费 元 0.15 900.00 135.00
    金额合计（大写）壹 佰叁拾伍元整 （小写）135.00
    申请号:2024103711884_缴费日期:2026-06-26_…
    其 他 信 息
    收款单位（章）：国 家知识产权局专利局 复核人： 王舒 收款人：胡瀛

Field mapping into the standard InvoiceRecord:

* ``invoice_number``  ← 票据号码 (票据代码 is dropped; the schema only
  carries one number slot).
* ``buyer_name`` / ``buyer_tax_id`` ← 交款人 / 交款人统一社会信用代码.
* ``seller_name`` ← 收款单位（章） (the issuing authority, no tax id).
* ``amount_without_tax`` ← 金额合计小写 (single fee, no VAT).
* ``total_amount`` / ``total_amount_upper`` ← 金额合计（小写/大写）.
* ``payee`` ← 收款人; ``reviewer`` ← 复核人; ``drawer`` ← 开票人
  (usually empty for these receipts).
"""

from __future__ import annotations

import re

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import (
    _DRAWER_RE,
    _PAYEE_RE,
    _REVIEWER_RE,
    first_match,
    normalize_issue_date,
    strip_spaces,
)

_RECEIPT_NO_RE = re.compile(r"票\s*据\s*号\s*码\s*[:：]\s*(\d+)")
_BUYER_NAME_RE = re.compile(r"交\s*款\s*人\s*[:：]\s*([^开\s][^\n]*?)(?=\s*开\s*票\s*日\s*期|$)")
_BUYER_TAX_RE = re.compile(
    r"交\s*款\s*人\s*统\s*一\s*社\s*会\s*信\s*用\s*代\s*码\s*[:：]\s*([0-9A-Z]{15,20})"
)
_SELLER_RE = re.compile(
    r"收\s*款\s*单\s*位\s*[\(（]\s*章\s*[\)）]\s*[:：]\s*([^\n]+?)(?=\s*复\s*核\s*人|$)"
)
_TOTAL_CN_RE = re.compile(
    r"金\s*额\s*合\s*计\s*[\(（]\s*大\s*写\s*[\)）]\s*([^\n]+?)(?=[\(（]\s*小\s*写|$)"
)
_TOTAL_NUM_RE = re.compile(r"[\(（]\s*小\s*写\s*[\)）]\s*(\d+\.\d{2})")
_ISSUE_DATE_RE = re.compile(r"开\s*票\s*日\s*期\s*[:：]\s*(\d{4}-\d{1,2}-\d{1,2})")


def _clean_seller_name(s: str) -> str:
    """Collapse whitespace inside the seller (收款单位) name.

    The 国家知识产权局 patent receipts print ``国 家知识产权局专利局`` on
    a single visual line but pdfplumber emits it with a stray space
    inside ``国 家``; we merge runs of internal whitespace so the name
    reads naturally.
    """

    return strip_spaces(s.strip().strip(":："))


def parse_central_non_tax_receipt(text: str, ctx: dict) -> InvoiceRecord:
    invoice_no = first_match(_RECEIPT_NO_RE.pattern, text)

    m_date = _ISSUE_DATE_RE.search(text)
    issue_date = normalize_issue_date(m_date.group(1)) if m_date else ""

    buyer_name = strip_spaces(first_match(_BUYER_NAME_RE.pattern, text))
    buyer_tax = first_match(_BUYER_TAX_RE.pattern, text)

    seller_raw = first_match(_SELLER_RE.pattern, text)
    seller_name = _clean_seller_name(seller_raw)

    total_cn = strip_spaces(first_match(_TOTAL_CN_RE.pattern, text))
    total_num = first_match(_TOTAL_NUM_RE.pattern, text)

    payee = first_match(_PAYEE_RE.pattern, text)
    reviewer = first_match(_REVIEWER_RE.pattern, text)
    drawer = first_match(_DRAWER_RE.pattern, text)

    return InvoiceRecord(
        file="",
        page_count=1,
        invoice_type="中央非税收入统一票据（电子）",
        invoice_number=invoice_no,
        issue_date=issue_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax,
        seller_name=seller_name,
        seller_tax_id="",
        amount_without_tax=total_num,
        tax_amount="",
        total_amount=total_num,
        total_amount_upper=total_cn,
        payee=payee,
        reviewer=reviewer,
        drawer=drawer,
    )