"""Invoice parsers — registration & dispatch.

Each ticket / invoice layout lives in its own module and exports a
``parse_<kind>(text, ctx) -> InvoiceRecord`` function. ``dispatch``
scans the registered rules in declaration order; the first one whose
keywords appear on the first page wins, and a default covers anything
else.  Adding a new parser is a one-line ``_register(...)`` call.
"""

from __future__ import annotations

from typing import Callable, List, Tuple

from fapiao_cn.parsers.airline_ticket import parse_airline_ticket
from fapiao_cn.parsers.invoice import parse_new_electronic_invoice
from fapiao_cn.parsers.merged_code import parse_merged_code_invoice
from fapiao_cn.parsers.non_tax_receipt import parse_central_non_tax_receipt
from fapiao_cn.parsers.old_invoice import parse_old_invoice
from fapiao_cn.parsers.train_ticket import parse_train_ticket

# Most specific rules first.
_RULES: List[Tuple[str, Tuple[str, ...], Callable[[str, dict], object]]] = []


def _register(
    name: str,
    keywords: Tuple[str, ...],
    parser: Callable[[str, dict], object],
) -> None:
    _RULES.append((name, keywords, parser))


_register(
    "airline",
    ("航空运输电子客票行程单", "销售网点代号"),
    parse_airline_ticket,
)
_register(
    "train",
    ("中国铁路", "买票请到12306"),
    parse_train_ticket,
)
_register(
    "merged_code",
    ("050001700211", "成品油 重庆增值税电子普通发票"),
    parse_merged_code_invoice,
)
_register(
    "non_tax_receipt",
    ("中央非税收入统一票据", "交款人统一社会信用代码"),
    parse_central_non_tax_receipt,
)
_register(
    "old",
    ("货物或应税劳务、服务名称", "密码区", "校 验 码"),
    parse_old_invoice,
)


DEFAULT_NAME = "new"
DEFAULT_PARSER_FUNC: Callable[[str, dict], object] = parse_new_electronic_invoice


def dispatch(first_page_text: str) -> Tuple[str, Callable[[str, dict], object]]:
    """Return ``(parser_name, parser_func)`` based on first page text."""

    for name, kws, parser in _RULES:
        for kw in kws:
            if kw in first_page_text:
                return name, parser
    return DEFAULT_NAME, DEFAULT_PARSER_FUNC


__all__ = ["dispatch"]