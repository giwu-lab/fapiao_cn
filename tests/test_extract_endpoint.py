"""End-to-end tests for the ``/invoice`` HTTP endpoint.

The HTTP layer adds a thin wrapper around ``formats.parse_pdf`` (PDF / XML /
OFD auto-detection).  These tests assert that every ``InvoiceRecord`` field
made it through to the wire response — in particular the
``expense_category`` family, which is added by ``expense_category.classify_expense``
and must survive the field-by-field copy in ``formats._build_response`` /
``_parse_xml`` / ``_parse_ofd``.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.server import app
from tests._helpers import ee_feature_enabled

client = TestClient(app)

_TEST_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


def _load(name: str) -> bytes:
    with open(os.path.join(_TEST_DIR, name), "rb") as f:
        return f.read()


class TestExtractExpenseCategoryWired:
    """``expense_category`` 字段必须贯穿到 ``/invoice`` 响应里。"""

    @pytest.mark.parametrize(
        "sample_path, expected_cat, expected_source",
        [
            # PDF 路径 → formats._build_response 必须传递字段
            ("新电子发票-标准/购_滴滴_01.pdf", "transportation", "seller"),
            ("新电子发票-标准/购_顺丰_01.pdf", "courier", "seller"),
            ("新电子发票-标准/购_通信_01.pdf", "telecom", "seller"),
            ("新电子发票-平台下载/购_自来水_01.pdf", "water", "seller"),
            ("新电子发票-标准/购_油品_02.pdf", "fuel", "seller"),
            ("新电子发票-标准/购_餐饮_01.pdf", "dining", "seller"),
            ("航空运输电子客票/航_运输_01.pdf", "transportation", "ticket"),
            ("铁路电子客票/火车票01.pdf", "transportation", "ticket"),
            ("成品油合并代码/购_油品_01.pdf", "fuel", "ticket"),
            # XML 路径 → formats._parse_xml 必须传递字段
            ("全电发票-XML/购_油品_01.xml", "fuel", "seller"),
            ("全电发票-XML/购_油品_02.xml", "fuel", "seller"),
            ("全电发票-XML/购_零售_01.xml", "dining", "item"),
        ],
    )
    def test_extract_response_carries_expense_category(
        self, auth_headers, sample_path, expected_cat, expected_source
    ):
        from fapiao_cn.config import settings

        settings.expense_category_enabled = True
        contents = _load(sample_path)
        resp = client.post(
            "/invoice",
            headers=auth_headers,
            files={"file": (os.path.basename(sample_path), contents)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("expense_category") == expected_cat, (
            f"{sample_path}: got {body.get('expense_category')!r}"
        )
        assert body.get("expense_category_source") == expected_source, (
            f"{sample_path}: source={body.get('expense_category_source')!r}"
        )
        # label 应当等于 CATEGORIES[cat]
        from fapiao_cn.expense_category import CATEGORIES

        assert body.get("expense_category_label") == CATEGORIES[expected_cat]

    @pytest.mark.parametrize(
        "sample_path",
        [
            "报关单/L20260528000009_21708433876_报关单放行条合并文档(1).pdf",
            "报关单/L20260625000008_0522.pdf",
        ],
    )
    def test_extract_customs_skips_classification(
        self, auth_headers, sample_path
    ):
        """把报关单误发给 ``/invoice`` 时，**不能**被静默压成空字段。

        企业版：报关单解析器是「守门包装」—— 报关单记录不是
        ``InvoiceRecord``，发票管线会把它压成一堆空字符串而 message 仍是
        "success"，调用方根本察觉不到数据被吞。包装器直接抛错，于是这里
        拿到 422 + 明确的指引（改走 ``/customs/batch``）。

        开源版：没有报关单解析器 → 落到默认解析器 → 未命中规则 →
        unclassified，分类三字段保持空。
        """
        from fapiao_cn.config import settings

        settings.expense_category_enabled = True
        contents = _load(sample_path)
        resp = client.post(
            "/invoice",
            headers=auth_headers,
            files={"file": (os.path.basename(sample_path), contents)},
        )
        if ee_feature_enabled("customs"):
            # 企业版：守门包装器拦下 → 422，且错误信息指明正确的端点。
            assert resp.status_code == 422, resp.text
            message = resp.json().get("message", "")
            assert message.startswith("error:"), message
            assert "/customs/batch" in message, message
        else:
            # 开源版：没有报关单能力，落到默认解析器，未命中任何分类规则。
            assert resp.status_code == 200
            body = resp.json()
            assert body.get("expense_category", "") in ("", "unclassified")
            assert body.get("expense_category_source", "") == ""

    def test_extract_response_disabled_flag(self, auth_headers):
        """``FC_EXPENSE_CATEGORY_ENABLED=false`` 时响应里三字段必须为空字符串。

        用 `reset_cache_for_tests(0)` 临时禁用 LRU，避免前一个 enabled 测试
        缓存的响应影响断言（同一个 PDF bytes + filename 会命中缓存）。
        """
        from fapiao_cn.cache import reset_cache_for_tests
        from fapiao_cn.config import settings

        settings.expense_category_enabled = False
        reset_cache_for_tests(0)
        try:
            contents = _load("新电子发票-标准/购_滴滴_01.pdf")
            resp = client.post(
                "/invoice",
                headers=auth_headers,
                files={"file": ("购_滴滴_01.pdf", contents)},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body.get("expense_category") == ""
            assert body.get("expense_category_label") == ""
            assert body.get("expense_category_source") == ""
        finally:
            settings.expense_category_enabled = True
            reset_cache_for_tests(128)


@pytest.mark.skipif(
    not ee_feature_enabled("customs"),
    reason="企业版能力：需要安装 fapiao_cn_ee 并提供有效许可证",
)
class TestCustomsBatchEndpointClassify:
    """``/customs/batch`` 不应触发 expense_category 分类逻辑。"""

    def test_customs_endpoint_skips_expense_category(self, auth_headers):
        from fapiao_cn.config import settings

        settings.expense_category_enabled = True
        contents = _load(
            "报关单/L20260528000009_21708433876_报关单放行条合并文档(1).pdf"
        )
        resp = client.post(
            "/customs/batch",
            headers=auth_headers,
            files=[("files", ("bgd.pdf", contents))],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()[0]
        # CustomsDeclarationHeader 没有 expense_category 字段；
        # 这一项作为不变量检查（如果 schema 误把 expense_category 加到
        # CustomsDeclarationHeader，分类逻辑就会泄漏到报关单响应里）
        assert "expense_category" not in body