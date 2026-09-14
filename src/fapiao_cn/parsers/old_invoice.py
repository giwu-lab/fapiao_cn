"""Old-format electronic invoice parser (e.g. 2.pdf).

Layout: ``发票代码:``, ``发票号码:``, ``开票日期:`` then two ``名称:``
+ ``纳税人识别号:`` blocks for buyer and seller, and a single line
``收款人: x 复核: y 开票人: z``.
"""

from __future__ import annotations

import re

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import extract_people, first_match, normalize_issue_date, strip_spaces

_INVOICE_CODE_RE = re.compile(r"发\s*票\s*代\s*码\s*[:：]\s*(\d+)")
_INVOICE_NO_RE = re.compile(r"发\s*票\s*号\s*码\s*[:：]\s*(\d+)")
_ISSUE_DATE_RE = re.compile(
    r"开\s*票\s*日\s*期\s*[:：]?\s*((?:20\d{2}|19\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
_NAME_RE = re.compile(r"名\s*称\s*[:：]\s*([^\n]+)")
_TAX_ID_RE = re.compile(r"纳\s*税\s*人\s*识\s*别\s*号\s*[:：]\s*([0-9A-Z]{15,20})")
_HEJI_RE = re.compile(r"合\s*计\s+(\d+\.\d+)\s+(\d+\.\d+)")
_TOTAL_CN_RE = re.compile(
    r"价\s*税\s*合\s*计\s*[\(（]\s*大\s*写\s*[\)）]\s*([^\n]+?)(?=[\(（]\s*小\s*写|$)"
)
_TOTAL_NUM_RE = re.compile(r"[\(（]\s*小\s*写\s*[\)）]\s*[¥￥]?\s*(\d+\.\d{2})")


def parse_old_invoice(text: str, ctx: dict) -> InvoiceRecord:
    invoice_code = first_match(_INVOICE_CODE_RE.pattern, text)
    invoice_no = first_match(_INVOICE_NO_RE.pattern, text)
    issue_date = normalize_issue_date(strip_spaces(first_match(_ISSUE_DATE_RE.pattern, text)))

    # Old-format uses "名称:" + "纳税人识别号:" — no 统一社会信用代码 phrase
    names = [m.group(1).strip() for m in _NAME_RE.finditer(text)]
    tax_ids = _TAX_ID_RE.findall(text)

    buyer_name = names[0] if names else ""
    seller_name = names[1] if len(names) > 1 else ""
    buyer_tax = tax_ids[0] if tax_ids else ""
    seller_tax = tax_ids[1] if len(tax_ids) > 1 else ""

    # 合计 line: "合 计 257.43 2.57"  (no ¥ prefix in old format)
    heji_m = _HEJI_RE.search(text)
    sub_total, sub_tax = (heji_m.group(1), heji_m.group(2)) if heji_m else ("", "")

    total_cn_m = _TOTAL_CN_RE.search(text)
    total_cn = strip_spaces(total_cn_m.group(1)) if total_cn_m else ""
    total_num = first_match(_TOTAL_NUM_RE.pattern, text)

    # People (shared helper handles both inline and per-label patterns)
    payee, reviewer, drawer = extract_people(text)

    invoice_number = (invoice_code + invoice_no) if (invoice_code and invoice_no) else invoice_no

    return InvoiceRecord(
        file="",
        page_count=1,
        invoice_type="电子发票（普通发票）",
        invoice_number=invoice_number,
        issue_date=issue_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax,
        seller_name=seller_name,
        seller_tax_id=seller_tax,
        amount_without_tax=sub_total,
        tax_amount=sub_tax,
        total_amount=total_num,
        total_amount_upper=total_cn,
        payee=payee,
        reviewer=reviewer,
        drawer=drawer,
    )
