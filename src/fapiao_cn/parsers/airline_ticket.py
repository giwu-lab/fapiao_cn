"""Air-transport e-ticket itinerary parser (e.g. 航55396.pdf).

Layout is completely different from a regular invoice; fields are
flat labels with values inline.
"""

from __future__ import annotations

import re

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import first_match, normalize_issue_date, strip_spaces

_INVOICE_NO_RE = re.compile(r"发\s*票\s*号\s*码\s*[:：]\s*(\d+)")
_ISSUE_DATE_RE = re.compile(
    r"填\s*开\s*日\s*期\s*[:：]\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
_SELLER_NAME_RE = re.compile(
    r"填\s*开\s*单\s*位\s*[:：]\s*(\S+?)(?=\s*填开日期|\s*购买方名称|\s*$)"
)
_SELLER_TAX_RE = re.compile(r"销\s*售\s*网\s*点\s*代\s*号\s*[:：]\s*(\S+)")
_BUYER_NAME_RE = re.compile(
    r"购\s*买\s*方\s*名\s*称\s*[:：]\s*(\S+?)(?=\s*统一社会信用代码|\s*$)"
)
_BUYER_TAX_RE = re.compile(
    r"统\s*一\s*社\s*会\s*信\s*用\s*代\s*码\s*/?\s*纳\s*税\s*人\s*识\s*别\s*号\s*[:：]\s*(\S+)"
)
# Each label row on the itinerary is followed by a single line of CNY
# values that appear in a fixed order:
#   票价, 燃油附加费, 增值税税率(无 CNY, 而是 "9%"), 增值税税额,
#   民航发展基金, 其他税费(可选), 合计
# So once we strip "9%" from the CNY list we get either 5 or 6 numbers
# depending on whether the "其他税费" cell is populated.
_CNY_RE = re.compile(r"CNY\s*(\d+(?:\.\d+)?)", flags=re.IGNORECASE)


def parse_airline_ticket(text: str, ctx: dict) -> InvoiceRecord:
    invoice_no = first_match(_INVOICE_NO_RE.pattern, text)
    issue_date = normalize_issue_date(strip_spaces(first_match(_ISSUE_DATE_RE.pattern, text)))

    seller_name = first_match(_SELLER_NAME_RE.pattern, text)
    seller_tax_id = first_match(_SELLER_TAX_RE.pattern, text)
    buyer_name = first_match(_BUYER_NAME_RE.pattern, text)
    buyer_tax = first_match(_BUYER_TAX_RE.pattern, text)

    cny_values = [float(m.group(1)) for m in _CNY_RE.finditer(text)]
    # Map values positionally: first 4 are stable, last is always 合计,
    # and the 5th slot is 其他税费 only when a 6th CNY value exists.
    if len(cny_values) >= 6:
        fare, fuel, vat, fund, other, total = cny_values[:6]
    elif len(cny_values) == 5:
        fare, fuel, vat, fund, total = cny_values[:5]
        other = 0.0
    else:
        fare = cny_values[0] if len(cny_values) > 0 else 0.0
        fuel = cny_values[1] if len(cny_values) > 1 else 0.0
        vat = cny_values[2] if len(cny_values) > 2 else 0.0
        fund = cny_values[3] if len(cny_values) > 3 else 0.0
        other = 0.0
        total = cny_values[-1] if cny_values else 0.0
    pre_tax = fare + fuel + fund + other

    def _f(v: float) -> str:
        return f"{v:.2f}" if v else ""

    return InvoiceRecord(
        file="",
        page_count=1,
        invoice_type="电子发票（航空运输电子客票行程单）",
        invoice_number=invoice_no,
        issue_date=issue_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax,
        seller_name=seller_name,
        seller_tax_id=seller_tax_id,
        amount_without_tax=_f(pre_tax),
        tax_amount=_f(vat),
        total_amount=_f(total),
        total_amount_upper="",
        payee="",
        reviewer="",
        drawer="",
    )
