"""Merged-code / 成品油 special-layout parser.

Covers  油_138303.pdf .

The PDF uses the old layout but the standard ``发票代码:`` /
``发票号码:`` / ``开票日期:`` lines are empty — the actual values are
embedded in adjacent lines::

    货物或应税劳务、服务名称 规格型号 单位 数 量 单 价 金 额
    050001700211                          <-- invoice code
    成品油 重庆增值税电子普通发票
    统一发票监 93369231                   <-- invoice number "93369231"
    国 制
    全 国家税务总局 章 2026 05 20        <-- issue date "2026年05月20日"
    机器编号: 661936847696 重庆市税务局 56183 87546 14426 67002

The buyer / seller info lives at unusual positions:

* Buyer name + tax id sit on lines that begin with the actual value
  and are immediately followed by cipher-text characters
  (``>63*4<706-041<+...``).

* Seller name + tax id sit right after the 价税合计 amount near the
  bottom of the page.
"""

from __future__ import annotations

import re

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import (
    CIPHER_RE,
    first_match,
    looks_like_company_name,
    looks_like_tax_id,
    normalize_issue_date,
    strip_spaces,
)

_INVOICE_CODE_RE = re.compile(r"(?m)^\s*(050001700211)\s*$")
_INVOICE_NO_RE = re.compile(r"统\s*一\s*发\s*票\s*监\s*(\d{6,12})")
_ISSUE_DATE_RE = re.compile(
    r"国\s*家\s*税\s*务\s*总\s*局\s*章\s*(\d{2,4})\s+(\d{1,2})\s+(\d{1,2})"
)
_TAX_ID_LINE_RE = re.compile(r"^([0-9A-Z]{15,20})")
_HEJI_RE = re.compile(r"[¥￥]\s*(\d+\.\d+)\s+[¥￥]\s*(\d+\.\d+)")
_TOTAL_CN_RE = re.compile(
    r"价\s*税\s*合\s*计\s*[\(（]\s*大\s*写\s*[\)）]\s*([^\n]+?)(?=[\(（]\s*小\s*写|$)"
)
_TOTAL_CN_BARE_RE = re.compile(
    r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整]+)\s*[¥￥]\s*\d+\.\d{2}"
)
_TOTAL_NUM_RE = re.compile(r"[\(（]?\s*小\s*写\s*[\)）]?\s*[¥￥]?\s*(\d+\.\d{2})")
_TOTAL_NUM_BARE_RE = re.compile(
    r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整]+\s+[¥￥]\s*(\d+\.\d{2})"
)
_CJK_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")
# Tokens that flag a buyer/seller name line and split it from the
# cipher characters that follow.  Reusing the cipher alphabet keeps
# the buyer-side and cipher-side boundary detection consistent.
_CIPHER_SPLIT_RE = re.compile(r"[\s><*+\-/\\]+")
_CIPHER_PREFIX_RE = re.compile(r"^([^><*+\-/\\]+)")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{4,40}")


def _extract_company_prefix(ln: str) -> str:
    """Pull the leading company-name portion out of a cipher-mixed line.

    Three-tier strategy — first match wins:

    1. Split by whitespace + cipher characters and return the first
       non-empty token that ``looks_like_company_name`` accepts.
    2. Search the whole line for a 4-40 char CJK run that passes
       ``looks_like_company_name`` (covers the no-space variant
       where the company name abuts cipher characters directly).
    3. Take everything from line start up to the first cipher
       character, stripped.
    """

    for tok in _CIPHER_SPLIT_RE.split(ln):
        tok = tok.strip()
        if tok and looks_like_company_name(tok):
            return tok
    for run_match in _CJK_RUN_RE.finditer(ln):
        if looks_like_company_name(run_match.group(0)):
            return run_match.group(0)
    prefix_match = _CIPHER_PREFIX_RE.match(ln)
    if prefix_match is None:
        return ln.strip()
    return prefix_match.group(1).strip()


def _normalize_date(y: str, mo: str, d: str) -> str:
    return f"{int(y):04d}年{int(mo):02d}月{int(d):02d}日"


def _extract_buyer_from_cipher_lines(lines: list) -> tuple:
    """Scan lines for the buyer name + tax id that sit just before the
    货物 or table header row, on lines that also contain cipher text
    (special characters such as >, <, *, +, /)."""

    for i, ln in enumerate(lines):
        if not CIPHER_RE.search(ln):
            continue
        # Extract the leading company-name portion (before the cipher block).
        name = _extract_company_prefix(ln)
        if not looks_like_company_name(name):
            continue
        if i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            m_tax = _TAX_ID_LINE_RE.match(next_line)
            if m_tax:
                return name, m_tax.group(1)
        return name, ""
    return "", ""


def _extract_seller_after_total(lines: list) -> tuple:
    """Find the seller name + tax id that appear right after the
    价税合计 line at the bottom of the page.

    The template at the top of the page has "价税合计(大写) (小写)" without
    amounts; the real data at the bottom has either
    "价税合计（大写）xxx（小写）¥xxx" with actual values or just
    "叁佰贰拾壹圆整 ¥321.00".
    """

    # The seller block sits somewhere below 价税合计.  Walk the lines in
    # reverse looking for the first line that looks like a company name;
    # the next 1-3 lines will carry the tax id.  ``tail_only=False`` is
    # required because the seller's own name is on its own line but the
    # keyword set only covers trailing substrings, so we let the helper
    # scan the whole string.
    for i in range(len(lines) - 1, -1, -1):
        candidate = lines[i].strip()
        if not candidate or not looks_like_company_name(candidate, tail_only=False):
            continue
        for k in range(i + 1, min(i + 4, len(lines))):
            next_line = lines[k].strip()
            if looks_like_tax_id(next_line):
                return candidate, next_line
        return candidate, ""
    return "", ""


def _extract_people_from_words(words: list) -> tuple:
    label_keywords = {"收款人:", "复核:", "开票人:", "销售方:(章)"}
    if not words:
        return "", "", ""
    bottom_y = max(w["top"] for w in words)
    bottom = sorted(
        (w for w in words if w["top"] >= bottom_y - 10),
        key=lambda w: w["x0"],
    )
    names = [
        w["text"] for w in bottom
        if _CJK_NAME_RE.fullmatch(w["text"])
        and w["text"] not in label_keywords
    ]
    if len(names) == 3:
        return names[0], names[1], names[2]
    return "", "", ""


def parse_merged_code_invoice(text: str, ctx: dict) -> InvoiceRecord:
    invoice_code = first_match(_INVOICE_CODE_RE.pattern, text)
    invoice_no = first_match(_INVOICE_NO_RE.pattern, text)

    issue_date = ""
    m = _ISSUE_DATE_RE.search(text)
    if m:
        issue_date = normalize_issue_date(_normalize_date(m.group(1), m.group(2), m.group(3)))

    lines = text.splitlines()
    buyer_name, buyer_tax = _extract_buyer_from_cipher_lines(lines)
    seller_name, seller_tax = _extract_seller_after_total(lines)

    # ---------- Amounts : 合计 / 价税合计 ----------
    # The subtotal line in merged-code format has explicit ¥ prefixes:
    #   "¥284.07 ¥36.93"
    # Use that to avoid matching unit prices from the 货物 table.
    heji_m = _HEJI_RE.search(text)
    sub_total, sub_tax = (heji_m.group(1), heji_m.group(2)) if heji_m else ("", "")

    total_cn = first_match(_TOTAL_CN_RE.pattern, text)
    if not total_cn or re.fullmatch(r"\s*", total_cn):
        m_cn = _TOTAL_CN_BARE_RE.search(text)
        if m_cn:
            total_cn = m_cn.group(1)
    total_cn = strip_spaces(total_cn)

    total_num = first_match(_TOTAL_NUM_RE.pattern, text)
    if not total_num:
        m = _TOTAL_NUM_BARE_RE.search(text)
        if m:
            total_num = m.group(1)

    # ---------- People : coordinate-based extraction ----------
    page_view = ctx.get("page")
    words = page_view.words() if page_view is not None else ctx.get("words", [])
    payee, reviewer, drawer = _extract_people_from_words(words)

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
