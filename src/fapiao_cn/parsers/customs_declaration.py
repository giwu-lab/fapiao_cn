"""Customs export declaration parser (海关出口货物报关单).

Covers both layout variants found in ``samples/报关单/``:

* **操作联** (operational copy) — first page is titled
  ``"中华人民共和国海关出口货物报关单"``.  Header labels use
  ``境内发货人 / 境外收货人 / 监管方式`` etc.
* **出口退税联** (export tax refund copy) — adds three pre-title lines
  (``JG09`` / ``报关单证明第 N 次打印 出口退税联`` / ``主页``) and
  uses ``境内收发货人 / 贸易方式 / 结汇方式`` etc.  Multi-page
  continuations repeat the header verbatim and append additional line
  items.

Both variants share the same ``CustomsDeclarationHeader`` schema.  The
``declaration_variant`` field distinguishes them.

The parser is **per-page** for header fields (most rows repeat across
pages and ``_aggregate`` picks the first non-empty value), but uses
``ctx["full_text"]`` for line-item extraction so multi-page 退税联
declarations accumulate every item into ``items[]`` and ``total_amount``.

Merged documents (报关单 + 放行条 in one PDF) — the 放行条 page lacks
the 报关单 title, so the per-page guard at the top of
``parse_customs_declaration`` returns an empty record for it.  The
aggregator then naturally keeps the 报关单 page's values.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from fapiao_cn.schemas import CustomsDeclarationHeader, CustomsDeclarationItem
from fapiao_cn.utils import strip_spaces

# Required title — pages lacking this string are not 报关单 pages (e.g.
# 放行条).  When this guard fails the parser returns an empty record so
# ``_aggregate``'s first/last-non-empty logic leaves page-0 fields
# untouched.
_DECLARATION_TITLE = "中华人民共和国海关出口货物报关单"

# Internal whitespace (used to make label regexes robust to pdfplumber's
# variable spacing).
_WS = re.compile(r"\s+")

# Tax-id / customs registration codes inside parens: ranges from a
# 10-digit 海关注册编码 to a 18-char 统一社会信用代码.  Accepts both
# ASCII and full-width parens to match pdfplumber's variable output.
_TAX_ID_RE = re.compile(r"[（(]\s*([0-9A-Z]{10,20})\s*[)）]")

# 8-digit dates (YYYYMMDD) — typical for 出口日期 / 申报日期 on 报关单
_DATE_8_RE = re.compile(r"\b(\d{8})\b")

# ISO date (YYYY-MM-DD) — used for 打印日期 on 退税联
_DATE_ISO_RE = re.compile(r"\b(20\d{2})[-.](\d{1,2})[-.](\d{1,2})\b")


def _collapse_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _find_value_line(lines: List[str], label_re: str) -> Tuple[int, str]:
    """Find the first line containing ``label_re`` and return
    ``(line_index, value_line)`` where ``value_line`` is the **next**
    non-empty line.  Returns ``(-1, "")`` if not found.
    """

    pat = re.compile(label_re)
    for i, line in enumerate(lines):
        if pat.search(line):
            for j in range(i + 1, len(lines)):
                if lines[j]:
                    return j, lines[j]
    return -1, ""


def _first_token(ln: str) -> str:
    """First whitespace-separated token of ``ln``."""

    return ln.split()[0] if ln else ""


def _label_column_index(label_line: str, label_re: str) -> int:
    """Return the **visual column index** of ``label_re`` inside
    ``label_line``.

    Visual columns differ from whitespace tokens when a label cell has
    an embedded code in parens — e.g. ``境内发货人 （91500106660851636U）
    出境关别 (2225) ...`` is **5 visual columns** but **7 whitespace
    tokens**.  We collapse ``<label> ... `` runs (where ``...`` is a
    bracketed code immediately following the label) into a single
    cell so the column index matches the value-line alignment.
    """

    cells = _cells_of_label_line(label_line)
    pat = re.compile(label_re)
    for idx, cell in enumerate(cells):
        if pat.search(cell):
            return idx
    return 0


def _cells_of_label_line(label_line: str) -> List[str]:
    """Split a label header line into visual cells.

    A *cell* is either:

    * a whitespace-delimited token, or
    * a token immediately followed by a code (Chinese parens, English
      parens, brackets) which together render as one column on the page.

    This matters for the operational (操作联) layout — e.g.

        ``境内发货人 （91500106660851636U） 出境关别 (2225) 出口日期 申报日期 备案号``

    is split into 5 cells::

        ["境内发货人（91500106660851636U）", "出境关别(2225)", "出口日期", "申报日期", "备案号"]

    matching the 5 value cells on the next row::

        ``重庆旺成贸易有限公司 外港海关 20260530 20260527``

    Returns the cells as plain strings.
    """

    collapsed = _collapse_ws(label_line)
    cells: List[str] = []
    tokens = collapsed.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # If the next token is a bracketed code (Chinese or ASCII
        # parens, possibly with whitespace inside) attach it to the
        # current cell.
        if i + 1 < len(tokens) and re.fullmatch(
            r"[（(]\s*[^()（）]*\s*[)）]",
            tokens[i + 1],
        ):
            cells.append(f"{tok} {tokens[i + 1]}")
            i += 2
            continue
        cells.append(tok)
        i += 1
    return cells


def _extract_section_value(
    lines: List[str],
    label_re: str,
    *,
    col: Optional[int] = None,
    take: str = "single",
) -> str:
    """Find the label header line, then return the value of the
    corresponding column from the next non-empty line.

    ``take``:

    * ``"single"`` (default) — return the single token at the detected
      column.  Suitable for values that are guaranteed one token.
    * ``"last"`` — return the **last** token of the value line.
      Suitable for trailing fields like ``提运单号`` whose value sits at
      the right edge of the row even when neighbouring columns have
      multi-token values.
    * ``"remaining"`` — return all tokens from the detected column to
      the end of the value line, joined by spaces.  Suitable for fields
      like ``运输工具名称`` whose value may span multiple tokens (e.g.
      vessel name + voyage number).

    Column alignment accounts for label codes (Chinese parens) embedded
    in the header line — e.g. ``境内发货人 （91500106660851636U）`` is
    treated as one cell, matching the value cell ``重庆旺成贸易有限公司``.
    When fewer value tokens than cells exist, the function tries both
    no-shift and full-shift alignments and returns the **first non-empty**
    candidate (operational layouts leave the trailing cell empty, while
    refund layouts leave the leading cell empty — we accept either).
    """

    pat = re.compile(label_re)
    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        cells = _cells_of_label_line(line)
        col_idx = col if col is not None else _label_column_index(line, label_re)
        for j in range(i + 1, len(lines)):
            if lines[j]:
                tokens = _collapse_ws(lines[j]).split()
                if not tokens:
                    return ""
                if take == "last":
                    return tokens[-1]
                max_shift = max(0, len(cells) - len(tokens))
                # Try alignments from smallest to largest shift; return
                # the first non-empty candidate.
                for shift in range(max_shift + 1):
                    effective_col = col_idx - shift
                    if 0 <= effective_col < len(tokens):
                        if take == "remaining":
                            return " ".join(tokens[effective_col:])
                        val = tokens[effective_col]
                        if val:
                            return val
                return ""
    return ""


def _extract_with_code_label(
    lines: List[str],
    label_re: str,
    *,
    name_col: Optional[int] = None,
    code_pattern: str = r"\(([^)]+)\)",
) -> Tuple[str, str]:
    """For labels whose code lives on the label line in parens, e.g.
    ``出口口岸(2248) ...`` (B 版) or ``境内发货人 (91500106660851636U) ...``
    (A 版).

    Returns ``(name, code)``.  ``name`` comes from the same column as
    the label in the header line (or ``name_col`` when supplied).
    ``code`` is captured from the label line.

    See ``_extract_section_value`` for the shift heuristic.
    """

    pat = re.compile(label_re)
    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        m_code = re.search(label_re + r"\s*\(([^)]+)\)", line)
        code = m_code.group(1) if m_code else ""
        col_idx = (
            name_col if name_col is not None else _label_column_index(line, label_re)
        )
        cells = _cells_of_label_line(line)
        for j in range(i + 1, len(lines)):
            if lines[j]:
                tokens = _collapse_ws(lines[j]).split()
                max_shift = max(0, len(cells) - len(tokens))
                name = ""
                for shift in range(max_shift + 1):
                    effective_col = col_idx - shift
                    if 0 <= effective_col < len(tokens):
                        val = tokens[effective_col]
                        if val:
                            name = val
                            break
                return name, code
    return "", ""


# ── 直接字段 (value 与 label 在同一行) ──


def _extract_customs_no(text: str) -> str:
    """海关编号``：<digits>``; strip surrounding ``*`` border decoration."""

    m = re.search(r"海\s*关\s*编\s*号\s*[：:]\s*\*?(\d{15,20})\*?", text)
    return m.group(1) if m else ""


def _extract_pre_entry_no(text: str) -> str:
    m = re.search(r"预\s*录\s*入\s*编\s*号\s*[：:]\s*(\S+?)(?=\s|$)", text)
    return m.group(1) if m else ""


def _extract_print_date(text: str) -> str:
    m = re.search(r"打\s*印\s*日\s*期\s*[：:]\s*(\d{4}[-.]\d{1,2}[-.]\d{1,2})", text)
    return m.group(1) if m else ""


def _extract_page_n_of_total(text: str) -> Tuple[int, int]:
    m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"页码/页数\s*[::]?\s*(\d+)/(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


# ── 日期 (value 在 label 下方一行) ──


def _extract_date_after_label(lines: List[str], label_pattern: str) -> str:
    pat = re.compile(label_pattern)
    for i, line in enumerate(lines):
        if pat.search(line):
            for j in range(i + 1, len(lines)):
                if lines[j]:
                    m = _DATE_8_RE.search(lines[j])
                    if m:
                        return m.group(1)
    return ""


def _normalize_8digit_date(s: str) -> str:
    """``YYYYMMDD`` → ``YYYY-MM-DD``."""

    if not s or len(s) != 8:
        return ""
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _normalize_iso_date(s: str) -> str:
    """``YYYY-MM-DD`` / ``YYYY.MM.DD`` → ``YYYY-MM-DD`` with zero-padded
    month/day."""

    m = _DATE_ISO_RE.search(s)
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


# ── 主体 (label 行带税号括号，value 行后为名称) ──


def _extract_with_inline_tax_id(
    lines: List[str],
    label_pattern: str,
    *,
    tax_id_required: bool = True,
) -> Tuple[str, str]:
    """For labels whose tax-id sits inside the label header line:

    ``境内发货人 (91500106660851636U) ...``

    The label line is matched; the tax id is captured (if present), and
    the **first non-empty token of the next line** is treated as the
    name.
    """

    pat = re.compile(label_pattern)
    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        m_tax = _TAX_ID_RE.search(line)
        tax_id = m_tax.group(1) if m_tax else ""
        cells = _cells_of_label_line(line)
        col_idx = _label_column_index(line, label_pattern)
        for j in range(i + 1, len(lines)):
            if not lines[j]:
                continue
            tokens = _collapse_ws(lines[j]).split()
            max_shift = max(0, len(cells) - len(tokens))
            name = ""
            for shift in range(max_shift + 1):
                effective_col = col_idx - shift
                if 0 <= effective_col < len(tokens):
                    val = tokens[effective_col]
                    if val:
                        name = val
                        break
            if tax_id_required and not tax_id:
                m_val_tax = _TAX_ID_RE.search(lines[j])
                if m_val_tax:
                    tax_id = m_val_tax.group(1)
            return name, tax_id
    return "", ""


def _extract_producer(lines: List[str]) -> Tuple[str, str]:
    """``生产销售单位`` — value on next line, tax id may be on either
    line."""

    pat = re.compile(r"生\s*产\s*销\s*售\s*单\s*位")
    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        m_tax = _TAX_ID_RE.search(line)
        tax_id = m_tax.group(1) if m_tax else ""
        cells = _cells_of_label_line(line)
        col_idx = _label_column_index(line, r"生\s*产\s*销\s*售\s*单\s*位")
        for j in range(i + 1, len(lines)):
            if not lines[j]:
                continue
            tokens = _collapse_ws(lines[j]).split()
            max_shift = max(0, len(cells) - len(tokens))
            name = ""
            for shift in range(max_shift + 1):
                effective_col = col_idx - shift
                if 0 <= effective_col < len(tokens):
                    val = tokens[effective_col]
                    if val:
                        name = val
                        break
            if not tax_id:
                m_val_tax = _TAX_ID_RE.search(lines[j])
                if m_val_tax:
                    tax_id = m_val_tax.group(1)
            return name, tax_id
    return "", ""


def _extract_declarant(lines: List[str]) -> Tuple[str, str]:
    """``申报单位(<tax_id>)<name>`` — declarant tax id and name share
    the same line, after the label.

    Operational (A 版): ``申报单位(911101131025694095)北京巡宇国际货运代理有限公司``
    Refund (B 版): same format, e.g.
    ``申报单位(91310109132289635R) 上海航联报关有限责任公司 申报单位（签章）``
    """

    pat = re.compile(r"申\s*报\s*单\s*位")
    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        collapsed = _collapse_ws(line)
        # Capture: 申报单位(<id>)<name> [申报单位（签章）]
        m = re.search(
            r"申\s*报\s*单\s*位\s*\(\s*([0-9A-Z]{15,20})\s*\)\s*"
            r"(.*?)"
            r"(?:\s*申\s*报\s*单\s*位\s*[（(]\s*签\s*章\s*[)）]\s*)?$",
            collapsed,
        )
        if m:
            return strip_spaces(m.group(2)), m.group(1)
        # Fallback: tax id without name (e.g. name is on another line)
        m_tax = _TAX_ID_RE.search(collapsed)
        if m_tax:
            rest = collapsed[m_tax.end():].strip()
            rest = re.sub(
                r"\s*申\s*报\s*单\s*位\s*[（(]\s*签\s*章\s*[)）]\s*$",
                "",
                rest,
            )
            return strip_spaces(rest), m_tax.group(1)
    return "", ""


def _extract_consignee(lines: List[str]) -> Tuple[str, str]:
    """``境外收货人`` — name on next line, tax id in parentheses on the
    same line OR same as name.
    """

    pat = re.compile(r"境\s*外\s*收\s*货\s*人")
    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        m_tax = _TAX_ID_RE.search(line)
        tax_id = m_tax.group(1) if m_tax else ""
        cells = _cells_of_label_line(line)
        col_idx = _label_column_index(line, r"境\s*外\s*收\s*货\s*人")
        for j in range(i + 1, len(lines)):
            if not lines[j]:
                continue
            tokens = _collapse_ws(lines[j]).split()
            max_shift = max(0, len(cells) - len(tokens))
            name = ""
            for shift in range(max_shift + 1):
                effective_col = col_idx - shift
                if 0 <= effective_col < len(tokens):
                    val = tokens[effective_col]
                    if val:
                        name = val
                        break
            if not tax_id:
                m_val_tax = _TAX_ID_RE.search(lines[j])
                if m_val_tax:
                    tax_id = m_val_tax.group(1)
            return name, tax_id
    return "", ""


# ── 关务 / 物流 / 贸易 (label 行 + value 行, 用列号取值) ──


# ── 国家 / 港口 / 来源地区对 (代码 + 名称) ──


def _extract_country_pair_after_label(
    lines: List[str],
    label_re: str,
) -> Tuple[str, str]:
    """A row like ``<label> + (<code>) <header> ...`` on the label row and
    ``[<name>]`` on the value row aligned with the same column.

    Example B 版::

        运抵国（地区）(502) 指运港（地区）(502) 境内货源地(50069)
        美国 美国 沙坪坝区
    """

    pat = re.compile(label_re)
    for i, line in enumerate(lines):
        if not pat.search(line):
            continue
        cells = _cells_of_label_line(line)
        col_idx = _label_column_index(line, label_re)
        m_code = re.search(label_re + r".*?\((\d+)\)", line)
        code = m_code.group(1) if m_code else ""
        for j in range(i + 1, len(lines)):
            if not lines[j]:
                continue
            tokens = _collapse_ws(lines[j]).split()
            max_shift = max(0, len(cells) - len(tokens))
            name = ""
            for shift in range(max_shift + 1):
                effective_col = col_idx - shift
                if 0 <= effective_col < len(tokens):
                    val = tokens[effective_col]
                    if val:
                        name = val
                        break
            return name, code
    return "", ""


# ── Items extraction ──


# Patterns tuned to the 退税联 layout (clean 5-column item header) and the
# operational layout (8-column item header).  Both layouts repeat ``项号
# ... 商品名称 ... 数量 ... 总价` on each page's first occurrence, then
# list items as multi-line blocks.

_ITEM_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d{10})\s+(\S.*?)\s+(.+)$"
)


def _split_item_value_line(line: str) -> List[str]:
    """Split a value line by whitespace, keeping digit-only tokens (used to
    recover the fixed-position fields)."""

    return line.split()


def _extract_items(text: str) -> List[CustomsDeclarationItem]:
    """Extract line items from the declaration body.

    Strategy: find each ``<num>`` row immediately after a header line
    containing ``项号`` or ``商品序号``.  The first column is the item
    number (项号); the second column is the HS code (商品编号); the
    third column is the name (possibly truncated).  The remaining
    tokens are the value columns.

    Both layouts (操作联 / 退税联) emit an item with two continuation
    lines:

    * 操作联 — ``0|2|...|<spec>`` row contains the spec extension **and**
    the total price.  The unit price appears on the same line as the
    item header.
    * 退税联 — header row contains 法定数量 / 法定单位 / 目的国 / 单价 /
    总价 / 币制, and a second row carries the spec / 第二数量 / 第二单位
    / 币种名.

    For simplicity we emit one item per ``项号`` and pack whatever
    tokens we can identify into the appropriate schema fields.
    """

    items: List[CustomsDeclarationItem] = []
    lines = [_collapse_ws(ln) for ln in text.splitlines() if ln.strip()]

    # Find item block start: header line containing 项号 or 商品序号
    item_header_idxs: List[int] = []
    for i, line in enumerate(lines):
        if re.match(r"^\s*(?:项号|商品序号)\s+", line):
            item_header_idxs.append(i)

    # If the header itself contains data (退税联 表格头), the header is
    # repeated on each page — take only the first occurrence per
    # block.  Items follow immediately until the next header.
    if not item_header_idxs:
        return items

    # Process each header; items appear on consecutive lines until the
    # next header (or end of text).
    boundaries = item_header_idxs + [len(lines)]
    for k in range(len(item_header_idxs)):
        start = item_header_idxs[k] + 1
        end = boundaries[k + 1]
        block = lines[start:end]
        if not block:
            break
        # The first item starts on the first non-empty line of the block
        # (each item is a multi-line group until the next item number).
        items.extend(_items_in_block(block))

    return items


def _items_in_block(block: List[str]) -> List[CustomsDeclarationItem]:
    """Parse one contiguous block of item lines.  Items are emitted per
    a leading ``<num> <HS>`` item header row.  Subsequent rows are
    continuations of the most recent item.

    The 退税联 item layout is::

        <num> <HS> <name> 法定数量/法定单位 <qty> <unit> <country> <unit_price> <total> <currency>
        <spec> 第二数量/第二单位 <qty2> <unit2> (<code>) <currency_name>
        申报数量/申报单位 <qty3> <unit3>

    where the second line may itself be split by line wrapping.  The
    first numeric ``<HS>`` (10-digit) is the row marker.
    """

    items: List[CustomsDeclarationItem] = []
    current: Optional[CustomsDeclarationItem] = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            items.append(current)
            current = None

    for raw in block:
        tokens = raw.split()
        if not tokens:
            continue
        # New item starts with digits: <item_no> <hs_code> <name...>
        if (
            len(tokens) >= 2
            and re.fullmatch(r"\d{1,3}", tokens[0])
            and re.fullmatch(r"\d{10}", tokens[1])
        ):
            _flush()
            current = CustomsDeclarationItem(
                item_no=tokens[0],
                hs_code=tokens[1],
                name_and_spec=tokens[2] if len(tokens) > 2 else "",
            )
            # Strip away the labels that may have leaked into the name
            # (e.g. "初级输出轴 法定数量/法定单位 210个 ...").
            if current.name_and_spec and any(
                kw in current.name_and_spec for kw in ("法定数量", "法定单位")
            ):
                current.name_and_spec = current.name_and_spec.split()[0]
            # Try to capture unit_price + total_price + currency from the
            # remainder of the header row.
            _capture_item_header_row(current, tokens[3:])
            continue
        # Continuation of the current item — capture quantities / price /
        # country / tax_status if present.
        if current is None:
            continue
        _accumulate_item_continuation(current, raw)

    _flush()
    return items


def _capture_item_header_row(item: CustomsDeclarationItem, tokens: list[str]) -> None:
    """Pull unit_price / total_price / currency out of an item header row.

    For 退税联 the header row carries the trailing numeric tail
    ``... <unit_price> <total_price> <currency>``.  We scan from the
    end so we don't depend on the exact column count.
    """

    if not tokens:
        return

    # Locate the first numeric token (unit_price candidate) and the
    # second numeric token (total_price).
    nums = [(idx, t) for idx, t in enumerate(tokens) if re.fullmatch(r"[\d,]+\.\d+", t)]
    if len(nums) >= 2:
        item.unit_price = nums[-2][1].replace(",", "")
        item.total_price = nums[-1][1].replace(",", "")
        # Last token (if alphabetic / USD / 美元) is the currency.
        last = tokens[-1]
        if re.fullmatch(r"[A-Z]{3}|[\u4e00-\u9fff]{2,4}", last):
            item.currency_name = last
            item.currency_code = ""  # code is on the spec row when present
    elif len(nums) == 1:
        item.total_price = nums[-1][1].replace(",", "")

    # Destination country: usually a single Chinese word preceding the
    # prices (e.g. "美国").  We can pick the first non-numeric token
    # AFTER the legal quantity label.
    for idx, t in enumerate(tokens):
        if idx > 0 and re.fullmatch(r"[\u4e00-\u9fff]{2,}", t):
            item.final_destination_name = t
            break


def _accumulate_item_continuation(item: CustomsDeclarationItem, line: str) -> None:
    """Push value tokens into the appropriate item fields based on which
    keywords appear on the continuation line."""

    # Legal quantity row: ``法定数量/法定单位 210个`` (退税联)
    if "法定数量" in line or "法定单位" in line:
        m = re.search(r"([\d.]+(?:\.\d+)?)\s*(\S+)", line)
        if m:
            item.legal_qty = f"{m.group(1)}{m.group(2)}"
        # Capture origin / destination country names if present (退税联
        # spec row carries ``(502) 美元`` after 第二数量).
        m_code = re.search(r"\(([A-Z]{3})\)", line)
        if m_code:
            item.final_destination_code = m_code.group(1)
        return

    # Second quantity row: ``第二数量/第二单位 325.5千克``
    if "第二数量" in line or "第二单位" in line:
        m = re.search(r"([\d.]+(?:\.\d+)?)\s*(\S+)", line)
        if m:
            item.second_qty = f"{m.group(1)}{m.group(2)}"
        # Spec continuation like ``0|0|将发动机的动力传 ...`` gets
        # appended to ``name_and_spec`` so the long description isn't
        # lost.
        spec_match = re.search(r"\d\|[^\d].*?(?=\s+\d+|\s*$)", line)
        if spec_match:
            spec = spec_match.group(0).strip()
            if spec and spec not in item.name_and_spec:
                item.name_and_spec = (
                    f"{item.name_and_spec}|{spec}" if item.name_and_spec else spec
                )
        return

    # Declared quantity row: ``申报数量/申报单位 210个``
    if "申报数量" in line or "申报单位" in line:
        m = re.search(r"([\d.]+(?:\.\d+)?)\s*(\S+)", line)
        if m:
            item.declared_qty = f"{m.group(1)}{m.group(2)}"
        return

    # Operational layout continuation ``<spec> <total> (<CHN>) (<PAK>) —
    # the spec portion starts with ``0|`` or similar digit-pipe pattern.
    m_total = re.search(r"([\d,]+\.\d{1,4})\s+(\(([A-Z]{3})\))?\s*(\(([A-Z]{3})\))?", line)
    if m_total and "|" in line.split(m_total.group(1))[0]:
        item.total_price = m_total.group(1).replace(",", "")
        if m_total.group(3):
            item.final_destination_code = m_total.group(3)
        if m_total.group(5):
            item.origin_country_code = m_total.group(5)
        spec = line.split(m_total.group(1))[0].strip()
        if spec and spec not in item.name_and_spec:
            item.name_and_spec = (
                f"{item.name_and_spec}|{spec}" if item.name_and_spec else spec
            )
        return

    # Plain continuation text → append to name_and_spec if it looks
    # like part of the description.
    if line and not any(kw in line for kw in ("法定", "第二", "申报", "原产", "目的")):
        if item.name_and_spec and line not in item.name_and_spec:
            item.name_and_spec = f"{item.name_and_spec}|{line}"


# ── Main entry point ──


def parse_customs_declaration(text: str, ctx: dict) -> CustomsDeclarationHeader:
    """Parse one page of 海关出口货物报关单 and return a
    ``CustomsDeclarationHeader`` record.

    Pages without the 报关单 title (e.g. 放行条 in merged documents)
    return an empty record so ``_aggregate``'s first/last-non-empty
    policy naturally ignores them.
    """

    rec = CustomsDeclarationHeader()

    # 副页守卫 — 非报关单页面直接返回空 record
    if _DECLARATION_TITLE not in text:
        return rec

    rec.page_count = int(ctx.get("page_count", 1)) or 1

    # Variant + direction
    if "出口退税联" in text:
        rec.declaration_variant = "出口退税联"
    else:
        rec.declaration_variant = "操作联"
    if "出口货物" in text:
        rec.direction = "出口"

    # Combine all pages for item extraction (multi-page 退税联)
    full_text = ctx.get("full_text") or text
    # 合并文档 (报关单 + 放行条) — 截断到放行条出现之前
    release_idx = full_text.find("通关无纸化")
    if release_idx > 0:
        full_text = full_text[:release_idx]

    # Header lines (per-page) for label/value extraction
    raw_lines = [ln for ln in text.splitlines() if ln.strip()]
    lines = [_collapse_ws(ln) for ln in raw_lines]

    # 直接字段
    rec.customs_no = _extract_customs_no(full_text)
    rec.pre_entry_no = _extract_pre_entry_no(full_text)
    rec.print_date = _normalize_iso_date(_extract_print_date(full_text))

    rec.export_date = _normalize_8digit_date(
        _extract_date_after_label(lines, r"出\s*口\s*日\s*期")
    )
    rec.declaration_date = _normalize_8digit_date(
        _extract_date_after_label(lines, r"申\s*报\s*日\s*期")
    )

    # 贸易主体
    rec.shipper_name, rec.shipper_tax_id = _extract_with_inline_tax_id(
        lines,
        r"境\s*内\s*(?:发|收\s*发)\s*货\s*人\s*[（(]",
    )
    rec.consignee_name, rec.consignee_tax_id = _extract_consignee(lines)
    rec.producer_name, rec.producer_tax_id = _extract_producer(lines)
    rec.declarant_name, rec.declarant_tax_id = _extract_declarant(lines)

    # 关务 / 物流
    rec.customs_office_name, rec.customs_office_code = _extract_with_code_label(
        lines, r"出\s*境\s*关\s*别"
    )
    if not rec.customs_office_name:
        rec.customs_office_name, rec.customs_office_code = _extract_with_code_label(
            lines, r"出\s*口\s*口\s*岸"
        )

    rec.transport_mode = _extract_section_value(lines, r"运\s*输\s*方\s*式")
    rec.transport_mode_code = ""
    m = re.search(r"运\s*输\s*方\s*式\s*\(?(\d)\)?", text)
    if m:
        rec.transport_mode_code = m.group(1)

    rec.vessel_or_flight = _extract_section_value(
        lines,
        r"(?:运输工具名称(?:及航次号)?|运输工具名称\s*及航次\s*号)",
        take="remaining",
    )
    rec.bill_of_lading_no = _extract_section_value(
        lines,
        r"提\s*运\s*单\s*号",
        take="last",
    )

    # 监管方式 (操作联) / 贸易方式 (退税联)
    sm_name, sm_code = _extract_with_code_label(lines, r"监\s*管\s*方\s*式")
    if not sm_name:
        sm_name, sm_code = _extract_with_code_label(lines, r"贸\s*易\s*方\s*式")
    rec.supervision_mode_name = sm_name
    rec.supervision_mode_code = sm_code

    # 征免性质
    rec.tax_exemption_name, rec.tax_exemption_code = _extract_with_code_label(
        lines, r"征\s*免\s*性\s*质"
    )

    rec.settlement_method = _extract_section_value(
        lines, r"结\s*汇\s*方\s*式"
    )

    # 贸易条款
    rec.trade_term_name = _extract_section_value(lines, r"成\s*交\s*方\s*式")
    m_term = re.search(r"成\s*交\s*方\s*式\s*\(?(\d)\)?", text)
    rec.trade_term_code = m_term.group(1) if m_term else ""

    # 运费/保费/杂费 — 三个字段并排，col index 区分
    rec.freight = _extract_section_value(lines, r"运\s*费")
    rec.insurance = _extract_section_value(lines, r"保\s*费")
    rec.misc_charges = _extract_section_value(lines, r"杂\s*费")

    # 包装 / 重量
    rec.package_type_name, rec.package_type_code = _extract_with_code_label(
        lines, r"包\s*装\s*种\s*类"
    )
    rec.packages_count = _extract_section_value(lines, r"件\s*数")
    rec.gross_weight_kg = _extract_section_value(
        lines, r"毛\s*重(?:[（(]\s*千\s*克\s*[)）])?"
    )
    rec.net_weight_kg = _extract_section_value(
        lines, r"净\s*重(?:[（(]\s*千\s*克\s*[)）])?"
    )

    # 贸易国 / 运抵国 / 指运港 / 离境口岸 / 境内货源地
    rec.trade_country_name, rec.trade_country_code = _extract_country_pair_after_label(
        lines, r"贸\s*易\s*国"
    )
    rec.destination_country_name, rec.destination_country_code = _extract_country_pair_after_label(
        lines, r"运\s*抵\s*国"
    )
    rec.destination_port_name, rec.destination_port_code = _extract_country_pair_after_label(
        lines, r"指\s*运\s*港"
    )
    rec.exit_port_name, rec.exit_port_code = _extract_country_pair_after_label(
        lines, r"离\s*境\s*口\s*岸"
    )
    rec.source_region_name, rec.source_region_code = _extract_country_pair_after_label(
        lines, r"境\s*内\s*货\s*源\s*地"
    )

    # 单证
    rec.license_no = _extract_section_value(lines, r"许\s*可\s*证\s*号")
    rec.approval_no = _extract_section_value(lines, r"批\s*准\s*文\s*号")
    rec.contract_no = _extract_section_value(lines, r"合\s*同\s*协\s*议\s*号")

    # 关联单据 / 集装箱 / 备注
    rec.attached_docs = _extract_attached_docs(lines)
    rec.container_no = _extract_section_value(lines, r"集\s*装\s*箱\s*号")
    rec.container_summary = _extract_section_value(
        lines, r"集\s*装\s*箱\s*标\s*箱\s*数\s*及\s*号\s*码", col=0
    )
    rec.marks_and_remarks = _extract_marks_and_remarks(lines)

    # 报关员 / 电话 — 在最末整合时出现
    rec.declarant_person_id = _extract_section_value(lines, r"报关人员证号")
    rec.declarant_phone = _extract_section_value(lines, r"电话")
    rec.declarant_person = _extract_section_value(lines, r"报关人员")
    # 若 "报关人员" 匹配到了下一行的"报关人员证号"开头 (后者以数字开头)，
    # 则取真正的报关员名字为空——上面 _extract_section_value col=0 会取
    # "报关人员" 后面第一行的第一个 token。但如果它是列名，可能需要改用
    # _first_token approach.

    # Items
    items = _extract_items(full_text)
    rec.items = items
    rec.item_count = len(items)
    if items:
        rec.currency = items[0].currency_name
        total = 0.0
        for it in items:
            if it.total_price:
                try:
                    total += float(it.total_price.replace(",", ""))
                except ValueError:
                    pass
        rec.total_amount = f"{total:.2f}" if total > 0 else ""

    return rec


# ── 辅助：随附单据 / 备注 等需要多行拼接的字段 ──


def _extract_attached_docs(lines: List[str]) -> str:
    """随附单证及编号 / 随附单据 — 标题下一行内容即是。"""

    for i, line in enumerate(lines):
        if re.match(r"^\s*随\s*附\s*单\s*证\s*及\s*编\s*号", line) or re.match(
            r"^\s*随\s*附\s*单\s*据", line
        ):
            for k in range(i + 1, len(lines)):
                if lines[k]:
                    return lines[k]
    return ""


def _extract_marks_and_remarks(lines: List[str]) -> str:
    """标记唛码及备注 — 标题后到下一个表头（项号 / 商品序号）之前
    的所有非空行拼接。"""

    out: List[str] = []
    for i, line in enumerate(lines):
        if re.match(r"^\s*标\s*记\s*唛\s*码\s*及\s*备\s*注", line):
            for k in range(i + 1, len(lines)):
                ln = lines[k]
                if re.match(r"^\s*(?:项号|商品序号)\s+", ln):
                    break
                out.append(ln)
            break
    return " | ".join(out)