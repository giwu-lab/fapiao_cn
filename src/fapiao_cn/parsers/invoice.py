"""New electronic invoice (普通 / 专用) parser.

Covers:
- 1.pdf / 12101 / 136887 / 3 / 5 / error
- 京东_136342 / 137904 / 138305
- 京东大药房-01 (label chars appear 3x in pdfplumber output; folded by
  ``fapiao_cn.pipeline._clean_text`` before reaching this parser)
- 55351 (成品油)
- 63398 (通行费)

All share the "项目名称" header style; the only variation is the
column header line (通行费 uses "车牌号 车辆类型 通行日期起 …").
"""

from __future__ import annotations

import re
import threading
from typing import List

from fapiao_cn.schemas import InvoiceRecord
from fapiao_cn.utils import (
    extract_people,
    first_match,
    looks_like_company_name,
    normalize_issue_date,
    strip_spaces,
)

# Per-process cache of (company_name -> tax_id) pairs learned from
# successful extractions.  Some issuers print the company name but omit
# the 统一社会信用代码/纳税人识别号 value (e.g. 购_餐饮_03.pdf); this
# cache lets us back-fill the missing slot when we have seen the same
# name with its tax id elsewhere.  Thread-safe so it is safe to share
# across requests handled by a single uvicorn worker.
_KNOWN_TAX_IDS: dict = {}
_KNOWN_TAX_IDS_LOCK = threading.Lock()


def record_company_tax_id(name: str, tax_id: str) -> None:
    """Store ``(name -> tax_id)`` in the in-process cache.

    Empty / whitespace-only inputs are ignored.  Exact-match only — we
    do not attempt fuzzy matching, since legal entity names should be
    stored verbatim in the PDF and a tax id lookup should be
    deterministic.
    """

    name = (name or "").strip()
    tax_id = (tax_id or "").strip()
    if not name or not tax_id:
        return
    with _KNOWN_TAX_IDS_LOCK:
        _KNOWN_TAX_IDS[name] = tax_id


def lookup_company_tax_id(name: str) -> str:
    """Return the cached tax id for ``name`` or ``""`` if absent."""

    name = (name or "").strip()
    if not name:
        return ""
    with _KNOWN_TAX_IDS_LOCK:
        return _KNOWN_TAX_IDS.get(name, "")


def clear_company_tax_id_cache() -> None:
    """Reset the cache (mainly for tests)."""

    with _KNOWN_TAX_IDS_LOCK:
        _KNOWN_TAX_IDS.clear()


# Regex (tolerant to internal spaces and either colon style).
_INVOICE_TYPE_RE = re.compile(
    r"电\s*子\s*发\s*票\s*[（(]?\s*(增\s*值\s*税\s*专\s*用\s*发\s*票|普\s*通\s*发\s*票)\s*[）)]?"
)
_INVOICE_CODE_RE = re.compile(r"发\s*票\s*代\s*码\s*[:：]\s*(\d+)")
_INVOICE_NO_RE = re.compile(r"发\s*票\s*号\s*码\s*[:：]\s*(\d+)")
_INVOICE_NO_EMBEDDED_RE = re.compile(r"统\s*一\s*发\s*票\s*监\s*制\S*\s*(\d{15,25})")
_ISSUE_DATE_RE = re.compile(
    r"开\s*票\s*日\s*期\s*[:：]?\s*((?:20\d{2}|19\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
_ISSUE_DATE_AFTER_TAX_BUREAU_RE = re.compile(
    r"国\s*家\s*税\s*务\s*总\s*局\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
_TAX_ID_RE = re.compile(
    r"统\s*一\s*社\s*会\s*信\s*用\s*代\s*码\s*/?\s*纳\s*税\s*人\s*识\s*别\s*号\s*[:：]?\s*([0-9A-Z]{15,20})"
)
_TAX_ID_AFTER_PHRASE_RE = re.compile(
    r"纳\s*税\s*人\s*识\s*别\s*号\s*[:：]?\s*([0-9A-Z]{15,20})"
)
_NAME_RE = re.compile(r"名\s*称\s*[:：]?\s*([^\n]+)")
_TABLE_HEADER_KEYWORDS = re.compile(r"规格型号|金\s*额\s*税\s*率|数\s*量\s*单\s*价")
_TWO_COLUMN_NAMES_RE = re.compile(
    r"购[\s\S]*?名\s*称\s*[:：]?\s*(\S+(?:\s+\S+)*?)\s+"
    r"销[\s\S]*?名\s*称\s*[:：]?\s*(\S+(?:\s+\S+)*?)(?=\s|信|$|\n)"
)
_TRAILING_SALES_RE = re.compile(r"\s*[销售]\s*$")

# Amount patterns
_PRICE_RE = re.compile(r"[¥￥]\s*(-?\d[\d,]*\.\d{2})")
_PRICE_BARE_RE = re.compile(r"(?<![\d.])(-?\d{1,3}(?:,\d{3})*\.\d{2})(?![\d.])")
_COMBINED_PRICE_RE = re.compile(_PRICE_RE.pattern + "|" + _PRICE_BARE_RE.pattern)
_HEJI_RE = re.compile(
    r"合\s*计\s*\n?\s*[¥￥]?(\d+\.\d{2})\s+[¥￥]?(\d+\.\d{2})"
)
# 免税变体：合计行的税额位置为 ``*``（不征税通配符），如通信发票。
# 仍匹配出 subtotal；税额列须单独解析。
_HEJI_TAX_FREE_RE = re.compile(
    r"合\s*计\s*\n?\s*[¥￥]?(\d+\.\d{2})\s*\*"
)
_TOTAL_CN_RE = re.compile(
    r"价\s*税\s*合\s*计\s*[（(]\s*大\s*写\s*[）)]\s*([^\n]+?)(?=[（(]\s*小\s*写|$)"
)
# Allow 1 or 2 decimal digits in the 总价小写 — some issuers (e.g. 滴滴
# v2) print ``¥ 46.3`` without trailing zero.  We normalise to 2
# decimal places when building the record.
_TOTAL_NUM_RE = re.compile(r"[（(]\s*小\s*写\s*[）)]\s*[¥￥´`]?\s*(\d+(?:\.\d+)?)")
_TOTAL_NUM_BARE_RE = re.compile(r"小\s*写\s*[）)]\s*[¥￥´`]?\s*(\d+(?:\.\d+)?)")
_TOTAL_NUM_SPECIAL_RE = re.compile(r"´\s*(\d+\.\d+)")
# Valid 大写 amount characters: numerals + 圆/元 + 角/分 + 整/正.  Used
# to strip leading/trailing noise (e.g. a stray ``⨂`` watermark glyph
# injected between the ``(大写)`` label and the amount text).
_AMOUNT_CN_CHARSET = "零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正"
_AMOUNT_CN_RE = re.compile(
    f"[{_AMOUNT_CN_CHARSET}]+"
)


def _extract_invoice_type(text: str) -> str:
    m = _INVOICE_TYPE_RE.search(text)
    if m:
        return f"电子发票（{m.group(1).replace(' ', '')}）"
    return ""


def _extract_issue_date(text: str) -> str:
    m = _ISSUE_DATE_RE.search(text)
    if m:
        return normalize_issue_date(strip_spaces(m.group(1)))
    m = _ISSUE_DATE_AFTER_TAX_BUREAU_RE.search(text)
    if m:
        return normalize_issue_date(strip_spaces(m.group(1)))
    return ""


def _extract_invoice_number(text: str) -> tuple:
    """Return ``(invoice_code, invoice_no)``; new e-invoices have no code."""

    code = first_match(_INVOICE_CODE_RE.pattern, text)
    no = first_match(_INVOICE_NO_RE.pattern, text)
    if not code and not no:
        # Some variants embed the number in the 统一发票监制 line
        m = _INVOICE_NO_EMBEDDED_RE.search(text)
        if m:
            no = m.group(1)
    return code, no


def _extract_names_and_tax_ids(text: str) -> tuple:
    """Return ``(buyer_name, buyer_tax, seller_name, seller_tax)``."""

    # Tax ids: first try the merged phrase; fall back to bare label
    tax_ids = _TAX_ID_RE.findall(text)
    if len(tax_ids) < 2:
        tax_ids = _TAX_ID_AFTER_PHRASE_RE.findall(text)

    raw_names: List[str] = []
    for m in _NAME_RE.finditer(text):
        val = m.group(1).strip()
        # Skip false matches from the table column header "项目名称"
        if _TABLE_HEADER_KEYWORDS.search(val):
            continue
        parts = re.split(r"\s+名\s*称\s*[:：]?\s*", val)
        raw_names.extend(p.strip() for p in parts if p.strip())

    if len(raw_names) < 2:
        two_col_match = _TWO_COLUMN_NAMES_RE.search(text)
        if two_col_match is not None:
            raw_names = [two_col_match.group(1).strip(), two_col_match.group(2).strip()]

    # Third fallback: two-column layout where both name values share a
    # single line (e.g. 水_137374).
    if len(raw_names) < 2:
        for ln in text.splitlines():
            tokens = ln.strip().split()
            company_parts = [t for t in tokens if looks_like_company_name(t)]
            if len(company_parts) >= 2:
                raw_names = company_parts[:2]
                break

    # Fourth fallback: 个人代开 variant.  Buyer/seller column headers sit
    # on their own rows ("购","买","方","信","息" and "销","售","方",
    # "信","息" split across multiple visual lines), and the actual
    # name values are merged onto a single physical line below the tax
    # id header row — e.g. "重庆市旺成科技股份有限公司 颜国明" with the
    # two tax ids on the next line.
    if len(raw_names) < 2 or len(tax_ids) < 2:
        name_line, tax_line = _find_personal_dekai_pair(text)
        if name_line and tax_line:
            cand_names = [
                t for t in re.split(r"\s+", name_line)
                if t and _looks_like_name_token(t)
            ]
            cand_ids = [
                m.group(1) for m in re.finditer(r"([0-9A-Z]{15,20})", tax_line)
            ]
            if len(cand_names) >= 2 and len(cand_ids) >= 2:
                raw_names = list(raw_names) + cand_names[:2]
                tax_ids = list(tax_ids) + cand_ids[:2]

    # Dedup while preserving order (in case fallbacks overlap).
    dedup_names = []
    for n in raw_names:
        n = _TRAILING_SALES_RE.sub("", n).strip()
        if n and n not in dedup_names:
            dedup_names.append(n)
    dedup_ids = []
    for tid in tax_ids:
        if tid and tid not in dedup_ids:
            dedup_ids.append(tid)
    raw_names = dedup_names
    tax_ids = dedup_ids

    buyer_name = raw_names[0] if raw_names else ""
    seller_name = raw_names[1] if len(raw_names) > 1 else ""
    buyer_tax = tax_ids[0] if tax_ids else ""
    seller_tax = tax_ids[1] if len(tax_ids) > 1 else ""

    # Final fallback: if we are still missing tax IDs, scan for a line
    # that carries two 15-20-char alphanumeric tokens (two-column layout).
    if len(tax_ids) < 2:
        for ln in text.splitlines():
            id_tokens = [t for t in ln.strip().split() if re.fullmatch(r"[0-9A-Z]{15,20}", t)]
            if len(id_tokens) >= 2:
                buyer_tax, seller_tax = id_tokens[0], id_tokens[1]
                break

    # Record any (name, tax_id) pairs we have just resolved so future
    # invoices can fill in slots where the issuer forgot to print the
    # 统一社会信用代码/纳税人识别号 value.
    if buyer_name and buyer_tax:
        record_company_tax_id(buyer_name, buyer_tax)
    if seller_name and seller_tax:
        record_company_tax_id(seller_name, seller_tax)

    # Back-fill: if a name is known from prior invoices but the current
    # PDF omitted its tax id, consult the in-process cache.
    if buyer_name and not buyer_tax:
        buyer_tax = lookup_company_tax_id(buyer_name)
    if seller_name and not seller_tax:
        seller_tax = lookup_company_tax_id(seller_name)

    return buyer_name, buyer_tax, seller_name, seller_tax


def _extract_amounts(text: str) -> tuple:
    """Return ``(amount_without_tax, tax_amount, total_amount_num, total_cn)``."""

    total_cn_m = _TOTAL_CN_RE.search(text)
    total_cn = ""
    if total_cn_m:
        # Strip noise characters (e.g. ``⨂`` watermark glyphs) that
        # sometimes sit between ``(大写)`` and the actual amount text.
        raw = strip_spaces(total_cn_m.group(1))
        tokens = _AMOUNT_CN_RE.findall(raw)
        if tokens:
            total_cn = max(tokens, key=len)

    # Numeric total (小写)
    total_num_m = _TOTAL_NUM_RE.search(text)
    if not total_num_m:
        total_num_m = _TOTAL_NUM_BARE_RE.search(text)
    if not total_num_m:
        total_num_m = _TOTAL_NUM_SPECIAL_RE.search(text)
    total_num = ""
    if total_num_m:
        try:
            total_num = f"{float(total_num_m.group(1)):.2f}"
        except ValueError:
            total_num = total_num_m.group(1)

    # Subtotal + tax from the "合 计" line.  The amounts can be on the
    # same line ("合 计 ¥124018.66 ¥16122.43") or on the previous line
    # ("¥27.69 ¥0.83\n合 计" — 通行费).
    sub_total = sub_tax = ""
    heji_match = _HEJI_RE.search(text)
    if heji_match:
        sub_total, sub_tax = heji_match.group(1), heji_match.group(2)
    else:
        # 免税变体：合计行第二个数字位置为 ``*`` (如通信发票)。
        tax_free_match = _HEJI_TAX_FREE_RE.search(text)
        if tax_free_match:
            sub_total = tax_free_match.group(1)
            sub_tax = "0.00"
        else:
            # Fallback: take the last two prices in the window just before
            # 价税合计.  Only trust this when the window contains two
            # distinct currency-prefixed values, so we do not falsely
            # promote a single line-item price into ``tax_amount``.
            idx_total = text.find("价税合计")
            if idx_total == -1:
                idx_total = len(text)
            idx_heji = text.rfind("合 计", 0, idx_total + 1)
            if idx_heji == -1:
                idx_heji = max(0, idx_total - 200)
            window = text[max(0, idx_heji - 100):idx_total]
            numeric = []
            for tup in _COMBINED_PRICE_RE.findall(window):
                val = next((x for x in tup if x), None)
                if not val:
                    continue
                try:
                    numeric.append(float(val.replace(",", "")))
                except ValueError:
                    pass
            # 仅在出现 ≥2 个不同货币前缀数字时才赋值税额；否则仅
            # 填 amount_without_tax，避免把同一金额重复赋给税额。
            if len(numeric) >= 2 and numeric[-1] != numeric[-2]:
                sub_total = f"{numeric[-2]:.2f}"
                sub_tax = f"{numeric[-1]:.2f}"
            elif len(numeric) >= 1:
                sub_total = f"{numeric[-1]:.2f}"

    return sub_total, sub_tax, total_num, total_cn


def _looks_like_name_token(s: str) -> bool:
    """Return True if ``s`` looks like a buyer/seller name piece.

    Used by the 个人代开 fallback that scans for the two names printed
    on the same visual line.  Allows short strings (2-30 chars), with at
    least one CJK glyph (so pure ASCII addresses/numbers are rejected),
    and no embedded currency / separator characters.
    """

    if not (2 <= len(s) <= 30):
        return False
    if re.fullmatch(r"[\d.,%/\-]+", s):
        return False
    if re.search(r"[*¥￥:/\\<>]", s):
        return False
    if not re.search(r"[\u4e00-\u9fff]", s):
        return False
    return True


def _find_personal_dekai_pair(text: str) -> tuple:
    """Find the (name_line, tax_line) pair for 个人代开 layout.

    Walks the cleaned text and returns the first adjacent pair that
    matches: a line with 2+ name-shaped tokens, followed by a line with
    2+ tax-id-shaped tokens.  Returns ``("", "")`` when not found.
    """

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    id_re = re.compile(r"[0-9A-Z]{15,20}")
    for i in range(len(lines) - 1):
        cand, nxt = lines[i], lines[i + 1]
        if id_re.findall(cand):
            continue  # already carries a tax id; not the name row
        ids = id_re.findall(nxt)
        if len(ids) < 2:
            continue
        tokens = [t for t in re.split(r"\s+", cand) if t]
        name_tokens = [t for t in tokens if _looks_like_name_token(t)]
        if len(name_tokens) < 2:
            continue
        return cand, nxt
    return "", ""


def parse_new_electronic_invoice(text: str, ctx: dict) -> InvoiceRecord:
    invoice_type = _extract_invoice_type(text)
    code, no = _extract_invoice_number(text)
    issue_date = _extract_issue_date(text)
    buyer_name, buyer_tax, seller_name, seller_tax = _extract_names_and_tax_ids(text)
    sub_total, sub_tax, total_num, total_cn = _extract_amounts(text)
    payee, reviewer, drawer = extract_people(text)

    invoice_number = (code + no) if (code and no) else no

    return InvoiceRecord(
        file="",
        page_count=1,
        invoice_type=invoice_type,
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
