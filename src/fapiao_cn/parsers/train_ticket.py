"""Railway e-ticket (铁路电子客票) parser (e.g. 火车票01.pdf).

Layout is single-column with a 统一发票监制 watermark interleaved into
the type line ("电子发票（统铁一发路票监电子客票）").  Only the buyer
side is printed; the ticket fare (票价 ￥xx.xx) is tax-inclusive and the
ticket carries no tax breakdown, so tax is reported as 0.00 and the fare
is used for both amount_without_tax and total_amount.

Seller info is fixed (中国铁路 / 12306) because railway tickets do not
print a seller name or tax id.
"""

from __future__ import annotations

import re

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import first_match, normalize_issue_date, strip_spaces

_INVOICE_NO_RE = re.compile(r"发\s*票\s*号\s*码\s*[:：]\s*(\d+)")
_ISSUE_DATE_RE = re.compile(
    r"开\s*票\s*日\s*期\s*[:：]?\s*((?:20\d{2}|19\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
_BUYER_RE = re.compile(
    r"购\s*买\s*方\s*名\s*称\s*[:：]\s*(\S+?)\s*统\s*一\s*社\s*会\s*信\s*用\s*代\s*码\s*[:：]\s*([0-9A-Z]{15,20})"
)
_BUYER_NAME_RE = re.compile(r"购\s*买\s*方\s*名\s*称\s*[:：]\s*(\S+)")
_FARE_RE = re.compile(r"[¥￥]\s*(\d+\.\d{2})")


def parse_train_ticket(text: str, ctx: dict) -> InvoiceRecord:
    invoice_no = first_match(_INVOICE_NO_RE.pattern, text)
    issue_date = normalize_issue_date(strip_spaces(first_match(_ISSUE_DATE_RE.pattern, text)))

    buyer_name = ""
    buyer_tax = ""
    m = _BUYER_RE.search(text)
    if m:
        buyer_name, buyer_tax = m.group(1), m.group(2)
    else:
        buyer_name = first_match(_BUYER_NAME_RE.pattern, text)

    fare = first_match(_FARE_RE.pattern, text)

    return InvoiceRecord(
        file="",
        page_count=1,
        invoice_type="电子发票（铁路电子客票）",
        invoice_number=invoice_no,
        issue_date=issue_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax,
        seller_name="中国铁路",
        seller_tax_id="12306",
        amount_without_tax=fare,
        tax_amount="0.00" if fare else "",
        total_amount=fare,
        total_amount_upper="",
        payee="",
        reviewer="",
        drawer="",
    )
