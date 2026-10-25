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
    "old",
    ("货物或应税劳务、服务名称", "密码区", "校 验 码"),
    parse_old_invoice,
    description="电子发票（普通发票）- 旧格式（含密码区、校验码）",
)

# 报关单（customs_declaration）**不在这里注册**：它是企业版能力，解析器
# 随 fapiao_cn_ee 发行包一起发布，由企业版插件在**获得授权时**通过
# register_external() 注入。开源版的 dispatch 表里没有这条规则，因此
# /formats 不会列出报关单，/extract 也不会把报关单 PDF 派给报关单解析器。


DEFAULT_NAME = "new"
DEFAULT_PARSER_FUNC: Callable[[str, dict], object] = parse_new_electronic_invoice
# The default parser covers the most common 新电子发票 (standard, 京东, 水费, 通行费 …)
_DESCRIPTIONS[DEFAULT_NAME] = (
    "电子发票（增值税专用发票/普通发票）- 标准新格式"
    "（含京东、水费、通行费变体）"
)


def register_external(
    name: str,
    keywords: Tuple[str, ...],
    parser: Callable[[str, dict], object],
    description: str = "",
) -> None:
    """外部（企业版）插件注册一条解析规则。

    幂等：同名规则会被**替换**而不是追加，重复调用不会产生重复规则。
    企业版插件在启动期和 SIGHUP 重载许可证时都会调这里，所以幂等是
    必需的。
    """
    for idx, rule in enumerate(_RULES):
        if rule[0] == name:
            _RULES[idx] = (name, keywords, parser)
            if description:
                _DESCRIPTIONS[name] = description
            return
    _register(name, keywords, parser, description)


def unregister_external(name: str) -> None:
    """撤销一条外部注册的规则（授权失效时调用）。"""
    _RULES[:] = [rule for rule in _RULES if rule[0] != name]
    _DESCRIPTIONS.pop(name, None)


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


__all__ = [
    "dispatch",
    "get_format_descriptions",
    "register_external",
    "unregister_external",
]
