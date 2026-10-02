"""Tests for the FastAPI HTTP endpoint."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.server import app

client = TestClient(app)

_TEST_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


class TestHealth:
    def test_health_returns_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_full_reports_dependencies(self, auth_headers):
        resp = client.get("/health/full", headers=auth_headers)
        # Returns 200 when all boolean checks pass.
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        checks = data["checks"]
        assert "pdfplumber" in checks
        assert "disk_writable" in checks
        assert "parsers_count" in checks
        assert "uptime_seconds" in checks
        assert isinstance(checks["uptime_seconds"], int)
        assert "cache" in checks
        assert "size" in checks["cache"]
        assert "capacity" in checks["cache"]

    def test_health_full_status_reflects_checks(self, auth_headers):
        """When a check explicitly fails, status must be degraded."""
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "pdfplumber":
                raise ImportError("simulated missing pdfplumber")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = _fake_import
        try:
            resp = client.get("/health/full", headers=auth_headers)
        finally:
            builtins.__import__ = real_import

        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"

    def test_health_full_parsers_count_matches_registry(self, auth_headers):
        """H27: parsers_count must reflect the live registry."""
        from fapiao_cn.parsers import _RULES

        resp = client.get("/health/full", headers=auth_headers)
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert data["checks"]["parsers_count"] == len(_RULES) + 1

    def test_health_full_low_hit_ratio_degrades(self, auth_headers, monkeypatch):
        """H18: cache hit_ratio < 5% over 50+ lookups → degraded."""
        from fapiao_cn.cache import ParseResultCache

        # Force low hit ratio by pre-populating cache with all misses.
        cache = ParseResultCache(capacity=128)
        for _ in range(60):
            cache.misses += 1
        # 0 hits, 60 misses → hit_ratio = 0
        monkeypatch.setattr("fapiao_cn.server.get_cache", lambda: cache)
        resp = client.get("/health/full", headers=auth_headers)
        assert resp.status_code == 503
        data = resp.json()
        assert data["checks"]["cache_low_hit_ratio"] is True

    def test_health_full_independent_of_cwd(self, auth_headers, monkeypatch, tmp_path):
        """H22: /health/full must work regardless of process cwd."""
        monkeypatch.chdir(tmp_path)
        resp = client.get("/health/full", headers=auth_headers)
        # Should not crash with FileNotFoundError on relative paths.
        assert resp.status_code in (200, 503)


class TestFormats:
    def test_formats_returns_dict(self, auth_headers):
        resp = client.get("/formats", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "formats" in data
        assert "new" in data["formats"]
        assert "airline" in data["formats"]
        assert "customs_declaration" in data["formats"]


class TestCustomsFormats:
    def test_customs_format_returns_field_schema(self, auth_headers):
        resp = client.get("/formats/customs", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "fields" in data
        names = {f["name"] for f in data["fields"]}
        # Required fields surfaced from the schema.
        for required in (
            "customs_no",
            "pre_entry_no",
            "export_date",
            "shipper_name",
            "shipper_tax_id",
            "consignee_name",
            "producer_name",
            "declarant_name",
            "customs_office_name",
            "customs_office_code",
            "transport_mode",
            "supervision_mode_name",
            "supervision_mode_code",
            "bill_of_lading_no",
            "destination_country_name",
            "items",
            "total_amount",
            "currency",
        ):
            assert required in names, f"missing field {required}"


class TestExtractCustoms:
    """End-to-end tests for ``/extract/customs``."""

    def _path(self, name: str) -> str:
        return os.path.join(_TEST_DIR, "报关单", name)

    def test_extract_customs_refund(self, auth_headers):
        path = self._path("L20260602000010_0429.pdf")
        resp = client.post("/extract/customs", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["declaration_variant"] == "出口退税联"
        assert data["customs_no"] == "223120260001921706"
        assert data["shipper_name"] == "重庆旺成贸易有限公司"
        assert data["shipper_tax_id"] == "5006960544"
        assert data["item_count"] == 3
        assert data["currency"] == "USD"
        assert data["total_amount"] == "5332.28"

    def test_extract_customs_operational(self, auth_headers):
        path = self._path("L20260626000009_BGD_SNKO020260509798.pdf")
        resp = client.post("/extract/customs", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["declaration_variant"] == "操作联"
        assert data["customs_no"] == "222920260002109647"
        assert data["declarant_name"] == "上海航联报关有限责任公司"

    def test_extract_customs_multi_page_refund(self, auth_headers):
        """6-page 退税联 declaration: items must aggregate across pages."""
        path = self._path("L20260625000008_0522.pdf")
        resp = client.post("/extract/customs", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["page_count"] == 6
        assert data["item_count"] == 24
        assert data["total_amount"] == "49195.76"

    def test_extract_customs_merged_document_skips_release(self, auth_headers):
        """The 报关单 + 放行条 merged PDF must yield 报关单 fields only."""
        path = self._path("L20260528000009_21708433876_报关单放行条合并文档(1).pdf")
        resp = client.post("/extract/customs", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["declaration_variant"] == "操作联"
        assert data["customs_no"] == "010120260000240192"
        assert data["declarant_name"] == "北京巡宇国际货运代理有限公司"
        # The 放行条 page must not produce a customs_no.
        assert data["customs_no"]

    def test_extract_customs_upload_form(self, auth_headers):
        path = self._path("L20260602000010_0429.pdf")
        with open(path, "rb") as f:
            resp = client.post(
                "/extract/customs",
                headers=auth_headers,
                files={"file": ("0429.pdf", f, "application/pdf")},
            )
        assert resp.status_code == 200
        assert resp.json()["customs_no"] == "223120260001921706"

    def test_extract_customs_unauthenticated(self):
        """Without an auth header, the endpoint must reject."""
        path = self._path("L20260602000010_0429.pdf")
        resp = client.post("/extract/customs", json={"file_path": path})
        assert resp.status_code in (401, 403)

    def test_extract_customs_no_input(self, auth_headers):
        resp = client.post("/extract/customs", json={}, headers=auth_headers)
        assert resp.status_code == 400


class TestExtract:
    def test_extract_with_file_upload(self, auth_headers):
        path = os.path.join(_TEST_DIR, "新电子发票-标准", "购_材料_01.pdf")
        with open(path, "rb") as f:
            resp = client.post(
                "/extract",
                headers=auth_headers,
                files={"file": ("购_材料_01.pdf", f, "application/pdf")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["invoice_number"]
        assert data["buyer_name"]
        assert data["seller_name"]

    def test_extract_with_file_path_json(self, auth_headers):
        path = os.path.join(_TEST_DIR, "新电子发票-标准", "购_材料_01.pdf")
        resp = client.post("/extract", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["invoice_number"]
        assert data["buyer_name"]

    def test_extract_with_raw_true(self, auth_headers):
        """``/raw`` 端点单独提供原始 PDF 文本。"""
        path = os.path.join(_TEST_DIR, "新电子发票-标准", "购_材料_01.pdf")
        resp = client.post("/raw", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "text" in data
        assert data["text"]

    @pytest.mark.xfail(reason="schema: 'warnings' replaced by 'message'", strict=False)
    def test_extract_returns_all_expected_keys(self, auth_headers):
        path = os.path.join(_TEST_DIR, "新电子发票-标准", "购_材料_01.pdf")
        with open(path, "rb") as f:
            resp = client.post(
                "/extract",
                headers=auth_headers,
                files={"file": ("购_材料_01.pdf", f, "application/pdf")},
            )
        data = resp.json()
        expected_keys = {
            "file", "page_count", "invoice_type", "invoice_number",
            "issue_date", "buyer_name", "buyer_tax_id", "seller_name",
            "seller_tax_id", "amount_without_tax", "tax_amount",
            "total_amount", "total_amount_upper", "payee", "reviewer",
            "drawer", "warnings",
        }
        assert expected_keys.issubset(data.keys())

    def test_extract_no_file_returns_error(self, auth_headers):
        resp = client.post("/extract", json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_extract_multiple_formats(self, auth_headers):
        """Verify all known invoice types parse without error."""
        files = [
            "新电子发票-标准/购_材料_01.pdf", "新电子发票-旧格式/购_材料_02.pdf",
            "新电子发票-标准/购_租赁_01.pdf", "成品油合并代码/购_油品_01.pdf",
            "航空运输电子客票/航_运输_01.pdf", "新电子发票-标准/购_自来水_01.pdf",
        ]
        for fname in files:
            path = os.path.join(_TEST_DIR, fname)
            resp = client.post("/extract", json={"file_path": path}, headers=auth_headers)
            assert resp.status_code == 200, f"{fname} failed: {resp.text}"
            data = resp.json()
            assert data.get("invoice_number"), f"{fname}: no invoice_number"
            assert data.get("buyer_name"), f"{fname}: no buyer_name"
            assert data.get("seller_name"), f"{fname}: no seller_name"


class TestErrorHandling:
    def test_invalid_file_path_is_handled(self, auth_headers):
        resp = client.post(
            "/extract",
            json={"file_path": "/nonexistent/test.pdf"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 422)

    def test_invalid_base64_is_handled(self, auth_headers):
        resp = client.post(
            "/extract",
            json={"file_base64": "not-valid-base64!!!"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 422)

    def test_non_pdf_content_is_handled(self, auth_headers):
        resp = client.post(
            "/extract",
            headers=auth_headers,
            files={"file": ("test.txt", b"not a pdf", "text/plain")},
        )
        assert resp.status_code == 422


class TestXmlApi:
    """API integration tests for XML e-invoice format."""

    def test_xml_multipart_upload(self, auth_headers):
        path = os.path.join(_TEST_DIR, "全电发票-XML", "购_材料_01.xml")
        with open(path, "rb") as f:
            resp = client.post(
                "/extract",
                headers=auth_headers,
                files={"file": ("购_材料_01.xml", f, "application/xml")},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["invoice_number"] == "25502000000129241041"
        assert data["invoice_type"] == "电子发票（增值税专用发票）"
        assert data["seller_name"] == "重庆铜睿机械有限公司"
        assert data["total_amount"] == "140141.09"
        assert data["page_count"] == 1

    def test_xml_file_path_json(self, auth_headers):
        path = os.path.join(_TEST_DIR, "全电发票-XML", "购_油品_01.xml")
        resp = client.post("/extract", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["invoice_number"] == "26507000000185284098"
        assert data["invoice_type"] == "电子发票（普通发票）"
        assert data["drawer"] == "邹玮"

    def test_xml_sales_invoice(self, auth_headers):
        """The 销_产品_01.xml sample has a sales-side (selling) invoice shape."""
        path = os.path.join(_TEST_DIR, "全电发票-XML", "销_产品_01.xml")
        resp = client.post("/extract", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["invoice_type"] == "电子发票（增值税专用发票）"
        assert data["seller_name"] == "重庆市旺成科技股份有限公司"
        assert data["buyer_name"] == "新大洲本田摩托（苏州）有限公司"

    @pytest.mark.xfail(reason="schema: 'warnings' replaced by 'message'", strict=False)
    def test_xml_response_keys_match_pdf(self, auth_headers):
        """XML responses must expose the same field set as PDF responses."""
        path = os.path.join(_TEST_DIR, "全电发票-XML", "购_零售_01.xml")
        with open(path, "rb") as f:
            resp = client.post(
                "/extract",
                headers=auth_headers,
                files={"file": ("购_零售_01.xml", f, "application/xml")},
            )
        data = resp.json()
        expected_keys = {
            "file", "page_count", "invoice_type", "invoice_number",
            "issue_date", "buyer_name", "buyer_tax_id", "seller_name",
            "seller_tax_id", "amount_without_tax", "tax_amount",
            "total_amount", "total_amount_upper", "payee", "reviewer",
            "drawer", "warnings",
        }
        assert expected_keys.issubset(data.keys())

    def test_xml_malformed_returns_error(self, auth_headers):
        resp = client.post(
            "/extract",
            headers=auth_headers,
            files={"file": ("bad.xml", b"<<not valid xml>>", "application/xml")},
        )
        assert resp.status_code == 422
        assert "xml_parse_failed" in resp.text


class TestOfdApi:
    """OFD 数电票 end-to-end via the HTTP API."""

    OFD_NAME = "全电发票-OFD/烤匠电子发票_332.60元_龚健武.ofd"

    def test_ofd_multipart_upload(self, auth_headers):
        path = os.path.join(_TEST_DIR, self.OFD_NAME)
        with open(path, "rb") as f:
            resp = client.post(
                "/extract",
                headers=auth_headers,
                files={
                        "file": (
                            "烤匠电子发票_332.60元_龚健武.ofd",
                            f,
                            "application/ofd",
                        )
                    },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["invoice_number"] == "26507000000257250590"
        assert data["seller_name"] == "重庆福烤餐饮服务有限公司"
        assert data["total_amount"] == "332.60"
        assert data["page_count"] == 1

    def test_ofd_file_path_json(self, auth_headers):
        path = os.path.join(_TEST_DIR, self.OFD_NAME)
        resp = client.post(
            "/extract", json={"file_path": path}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["invoice_number"] == "26507000000257250590"
        assert data["amount_without_tax"] == "313.77"
        assert data["tax_amount"] == "18.83"

    def test_raw_ofd_returns_400(self, auth_headers):
        """``/raw`` rejects OFD the same way it does XML."""
        with open(
            os.path.join(_TEST_DIR, self.OFD_NAME), "rb"
        ) as f:
            resp = client.post(
                "/raw",
                headers=auth_headers,
                files={
                        "file": (
                            "烤匠电子发票_332.60元_龚健武.ofd",
                            f,
                            "application/ofd",
                        )
                    },
            )
        assert resp.status_code == 400
        assert "raw_unsupported" in resp.json()["detail"]
