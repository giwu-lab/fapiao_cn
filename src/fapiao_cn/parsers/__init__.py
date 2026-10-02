"""Invoice parsers — registration & dispatch.

Each ticket / invoice layout lives in its own module and exports a
``parse_<kind>(text, ctx) -> InvoiceRecord`` function. ``dispatch``
scans the registered rules in declaration order; the first one whose
keywords appear on the first page wins, and a default covers anything
else.  Adding a new parser is a one-line ``_register(...)`` call.

The ``/formats`` endpoint pulls its human-readable descriptions from
the same registry (``get_format_descriptions``), so adding a parser
registers both routing and documentation in one place (H7).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

from fapiao_cn.parsers.airline_ticket import parse_airline_ticket
from fapiao_cn.parsers.customs_declaration import parse_customs_declaration
from fapiao_cn.parsers.invoice import parse_new_electronic_invoice
from fapiao_cn.parsers.merged_code import parse_merged_code_invoice
from fapiao_cn.parsers.non_tax_receipt import parse_central_non_tax_receipt
from fapiao_cn.parsers.old_invoice import parse_old_invoice
from fapiao_cn.parsers.train_ticket import parse_train_ticket

# Most specific rules first.
_RULES: List[Tuple[str, Tuple[str, ...], Callable[[str, dict], object]]] = []
# Per-parser descriptions surfaced via /formats.  Keyed by parser name.
_DESCRIPTIONS: Dict[str, str] = {}


def _register(
    name: str,
    keywords: Tuple[str, ...],
    parser: Callable[[str, dict], object],
    description: str = "",
) -> None:
    _RULES.append((name, keywords, parser))
    if description:
        _DESCRIPTIONS[name] = description


_register(
    "airline",
    ("航空运输电子客票行程单", "销售网点代号"),
    parse_airline_ticket,
    description="电子发票（航空运输电子客票行程单）",
)
_register(
    "train",
    ("中国铁路", "买票请到12306"),
    parse_train_ticket,
    description="电子发票（铁路电子客票）",
)
_register(
    "merged_code",
    ("050001700211", "成品油 重庆增值税电子普通发票"),
    parse_merged_code_invoice,
    description="电子发票（普通发票）- 合并代码/成品油变体",
)
_register(
    "non_tax_receipt",
    ("中央非税收入统一票据", "交款人统一社会信用代码"),
    parse_central_non_tax_receipt,
    description="中央非税收入统一票据（电子）（如专利年费、行政事业性收费）",
)
_register(
    "customs_declaration",
    ("中华人民共和国海关出口货物报关单", "海关编号"),
    parse_customs_declaration,
    description="中华人民共和国海关出口货物报关单（操作联 / 出口退税联）",
)
_register(
    "old",
    ("货物或应税劳务、服务名称", "密码区", "校 验 码"),
    parse_old_invoice,
    description="电子发票（普通发票）- 旧格式（含密码区、校验码）",
)


DEFAULT_NAME = "new"
DEFAULT_PARSER_FUNC: Callable[[str, dict], object] = parse_new_electronic_invoice
# The default parser covers the most common 新电子发票 (standard, 京东, 水费, 通行费 …)
_DESCRIPTIONS[DEFAULT_NAME] = (
    "电子发票（增值税专用发票/普通发票）- 标准新格式"
    "（含京东、水费、通行费变体）"
)


def dispatch(first_page_text: str) -> Tuple[str, Callable[[str, dict], object]]:
    """Return ``(parser_name, parser_func)`` based on first page text."""

    for name, kws, parser in _RULES:
        for kw in kws:
            if kw in first_page_text:
                return name, parser
    return DEFAULT_NAME, DEFAULT_PARSER_FUNC


def get_format_descriptions() -> Dict[str, str]:
    """Return ``{parser_name: human_description}`` for all registered parsers.

    Used by the ``/formats`` endpoint so descriptions stay in lock-step
    with the dispatch table (H7).
    """

    return dict(_DESCRIPTIONS)


__all__ = ["dispatch", "get_format_descriptions"]
