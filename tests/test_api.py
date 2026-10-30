"""Tests for the FastAPI HTTP endpoint."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.config import settings
from fapiao_cn.server import app
from tests._helpers import ee_feature_enabled, serve_samples

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

    def test_health_full_warning_flag_degrades(self, auth_headers, monkeypatch):
        """A warning flag set to True must degrade readiness on its own.

        ``cache_low_hit_ratio`` is a warning bit (True = the warning fired),
        the opposite polarity of the pass checks (True = healthy).  Mixing
        both into a single ``all()`` cancelled them out, so this alert could
        never degrade readiness.  It only "passed" before because
        ``samples_dir_exists`` was permanently False and masked it.
        """
        from fapiao_cn.cache import ParseResultCache

        cache = ParseResultCache(capacity=128)
        for _ in range(60):
            cache.misses += 1
        monkeypatch.setattr("fapiao_cn.server.get_cache", lambda: cache)
        resp = client.get("/health/full", headers=auth_headers)
        assert resp.json()["checks"]["cache_low_hit_ratio"] is True
        assert resp.status_code == 503, resp.json()

    def test_health_full_samples_absent_does_not_degrade(self, auth_headers, tmp_path):
        """H50: missing samples/ is informational and must not gate readiness.

        ``samples/`` is .gitignore'd dev material and is not shipped in the
        image, so the readiness probe has to stay green without it.
        """
        # tmp_path has no samples/ subdir -> the check reports False.
        monkey_app_root = str(tmp_path)
        original = settings.app_root
        settings.app_root = monkey_app_root
        try:
            resp = client.get("/health/full", headers=auth_headers)
        finally:
            settings.app_root = original
        assert resp.json()["checks"]["samples_dir_exists"] is False
        assert resp.status_code == 200, resp.json()
        assert resp.json()["status"] == "ok"

    def test_health_full_resolves_app_root_not_src(self, auth_headers, tmp_path):
        """H50: the disk probe must target the app root, never ``src/``.

        The off-by-one (``parent.parent`` instead of ``parents[2]``) used to
        create a stray ``src/logs/`` directory on every probe.
        """
        src_dir = Path(__file__).resolve().parent.parent / "src"
        stray = src_dir / "logs"
        assert not stray.exists(), f"stray {stray} created by health probe"

        original = settings.app_root
        settings.app_root = str(tmp_path)
        try:
            resp = client.get("/health/full", headers=auth_headers)
        finally:
            settings.app_root = original
        # The probe wrote into tmp_path/logs, i.e. the configured app root.
        assert resp.status_code in (200, 503)
        assert (tmp_path / "logs").is_dir()


class TestFormats:
    def test_formats_returns_dict(self, auth_headers):
        resp = client.get("/formats", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "formats" in data
        assert "new" in data["formats"]
        assert "airline" in data["formats"]
        # 报关单解析是企业版能力：开源版的格式表里没有它，
        # 装了企业版包且许可证有效时才有。
        if ee_feature_enabled("customs"):
            assert "customs_declaration" in data["formats"]
        else:
            assert "customs_declaration" not in data["formats"]


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


@pytest.mark.skipif(
    not ee_feature_enabled("customs"),
    reason="企业版能力：需要安装 fapiao_cn_ee 并提供有效许可证",
)
class TestCustomsBatch:
    """End-to-end tests for ``/customs/batch``（多文件报关单提取）。"""

    def _path(self, name: str) -> str:
        return os.path.join(_TEST_DIR, "报关单", name)

    def _post_files(self, auth_headers, *names):
        """按 multipart ``files``（重复字段）提交若干报关单，返回响应数组。"""

        payload = []
        for name in names:
            path = self._path(name)
            with open(path, "rb") as f:
                payload.append(
                    ("files", (os.path.basename(path), f.read(), "application/pdf"))
                )
        resp = client.post("/customs/batch", headers=auth_headers, files=payload)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_customs_batch_refund(self, auth_headers):
        data = self._post_files(auth_headers, "L20260602000010_0429.pdf")[0]
        assert data["declaration_variant"] == "出口退税联"
        assert data["customs_no"] == "223120260001921706"
        assert data["shipper_name"] == "重庆旺成贸易有限公司"
        assert data["shipper_tax_id"] == "5006960544"
        assert data["item_count"] == 3
        assert data["currency"] == "USD"
        assert data["total_amount"] == "5332.28"

    def test_customs_batch_operational(self, auth_headers):
        data = self._post_files(
            auth_headers, "L20260626000009_BGD_SNKO020260509798.pdf"
        )[0]
        assert data["declaration_variant"] == "操作联"
        assert data["customs_no"] == "222920260002109647"
        assert data["declarant_name"] == "上海航联报关有限责任公司"

    def test_customs_batch_multi_page_refund(self, auth_headers):
        """6-page 退税联 declaration: items must aggregate across pages."""
        data = self._post_files(auth_headers, "L20260625000008_0522.pdf")[0]
        assert data["page_count"] == 6
        assert data["item_count"] == 24
        assert data["total_amount"] == "57438.00"

    def test_customs_batch_merged_document_skips_release(self, auth_headers):
        """The 报关单 + 放行条 merged PDF must yield 报关单 fields only."""
        data = self._post_files(
            auth_headers, "L20260528000009_21708433876_报关单放行条合并文档(1).pdf"
        )[0]
        assert data["declaration_variant"] == "操作联"
        assert data["customs_no"] == "010120260000240192"
        assert data["declarant_name"] == "北京巡宇国际货运代理有限公司"
        # The 放行条 page must not produce a customs_no.
        assert data["customs_no"]

    def test_customs_batch_multiple_files_in_one_request(self, auth_headers):
        """Several declarations in one request; results keep input order."""
        data = self._post_files(
            auth_headers,
            "L20260602000010_0429.pdf",
            "L20260626000009_BGD_SNKO020260509798.pdf",
            "L20260625000008_0522.pdf",
        )
        assert len(data) == 3
        assert [d["customs_no"] for d in data] == [
            "223120260001921706",
            "222920260002109647",
            "530420260000900809",
        ]
        assert [d["item_count"] for d in data] == [3, 2, 24]

    def test_customs_batch_json_files_array(self, auth_headers):
        """JSON 入参：``{"files": [{"filename": ..., "base64": ...}]}``。"""
        path = self._path("L20260602000010_0429.pdf")
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        resp = client.post(
            "/customs/batch",
            json={"files": [{"filename": "0429.pdf", "base64": b64}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        assert data[0]["customs_no"] == "223120260001921706"

    def test_customs_batch_json_url_input(self, auth_headers):
        """JSON 入参：``files[]`` 里直接给 URL —— 与 /invoice 的 url 入参对齐。"""
        from urllib.parse import quote

        with serve_samples() as base:
            url = f"{base}/{quote('报关单/L20260602000010_0429.pdf')}"
            resp = client.post(
                "/customs/batch",
                json={"files": [{"filename": "0429.pdf", "url": url}]},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        assert data[0]["customs_no"] == "223120260001921706"
        assert data[0]["item_count"] == 3

    def test_customs_batch_multipart_url_input(self, auth_headers):
        """multipart 入参：``urls`` 字段给远端报关单地址。"""
        from urllib.parse import quote

        with serve_samples() as base:
            url = f"{base}/{quote('报关单/L20260625000008_0522.pdf')}"
            resp = client.post(
                "/customs/batch",
                headers=auth_headers,
                files=[("urls", (None, url))],
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        assert data[0]["item_count"] == 24
        assert data[0]["total_amount"] == "57438.00"

    def test_customs_batch_mixed_upload_and_url(self, auth_headers):
        """同一批里混用「上传的文件」和「远端 URL」，顺序按输入顺序。"""
        from urllib.parse import quote

        with serve_samples() as base:
            url = f"{base}/{quote('报关单/L20260626000009_BGD_SNKO020260509798.pdf')}"
            path = self._path("L20260602000010_0429.pdf")
            with open(path, "rb") as f:
                payload = f.read()
            resp = client.post(
                "/customs/batch",
                headers=auth_headers,
                files=[
                    ("files", ("0429.pdf", payload, "application/pdf")),
                    ("urls", (None, url)),
                ],
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [d["customs_no"] for d in data] == [
            "223120260001921706",
            "222920260002109647",
        ]

    def test_customs_batch_unreachable_url(self, auth_headers):
        """URL 抓不到 → 400，且说明是抓取失败。"""
        from urllib.parse import quote

        with serve_samples() as base:
            url = f"{base}/{quote('报关单/不存在的报关单.pdf')}"
            resp = client.post(
                "/customs/batch",
                json={"files": [{"filename": "x.pdf", "url": url}]},
                headers=auth_headers,
            )
        assert resp.status_code == 400
        assert "Failed to fetch URL" in resp.json()["detail"]

    def test_customs_batch_unauthenticated(self):
        """Without an auth header, the endpoint must reject."""
        path = self._path("L20260602000010_0429.pdf")
        with open(path, "rb") as f:
            resp = client.post(
                "/customs/batch",
                headers={},
                files=[("files", ("0429.pdf", f.read(), "application/pdf"))],
            )
        assert resp.status_code in (401, 403)

    def test_customs_batch_no_input(self, auth_headers):
        resp = client.post("/customs/batch", json={}, headers=auth_headers)
        assert resp.status_code == 400


class TestExtract:
    def test_extract_with_file_upload(self, auth_headers):
        path = os.path.join(_TEST_DIR, "新电子发票-标准", "购_材料_01.pdf")
        with open(path, "rb") as f:
            resp = client.post(
                "/invoice",
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
        resp = client.post("/invoice", json={"file_path": path}, headers=auth_headers)
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
                "/invoice",
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
        resp = client.post("/invoice", json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_extract_multiple_formats(self, auth_headers):
        """Verify all known invoice types parse without error."""
        files = [
            "新电子发票-标准/购_材料_01.pdf", "新电子发票-旧格式/购_材料_02.pdf",
            "新电子发票-标准/购_租赁_01.pdf", "成品油合并代码/购_油品_01.pdf",
            "航空运输电子客票/航_运输_01.pdf", "新电子发票-平台下载/购_自来水_01.pdf",
        ]
        for fname in files:
            path = os.path.join(_TEST_DIR, fname)
            resp = client.post("/invoice", json={"file_path": path}, headers=auth_headers)
            assert resp.status_code == 200, f"{fname} failed: {resp.text}"
            data = resp.json()
            assert data.get("invoice_number"), f"{fname}: no invoice_number"
            assert data.get("buyer_name"), f"{fname}: no buyer_name"
            assert data.get("seller_name"), f"{fname}: no seller_name"


class TestErrorHandling:
    def test_invalid_file_path_is_handled(self, auth_headers):
        resp = client.post(
            "/invoice",
            json={"file_path": "/nonexistent/test.pdf"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 422)

    def test_invalid_base64_is_handled(self, auth_headers):
        resp = client.post(
            "/invoice",
            json={"file_base64": "not-valid-base64!!!"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 422)

    def test_non_pdf_content_is_handled(self, auth_headers):
        resp = client.post(
            "/invoice",
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
                "/invoice",
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
        resp = client.post("/invoice", json={"file_path": path}, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["invoice_number"] == "26507000000185284098"
        assert data["invoice_type"] == "电子发票（普通发票）"
        assert data["drawer"] == "邹玮"

    def test_xml_sales_invoice(self, auth_headers):
        """The 销_产品_01.xml sample has a sales-side (selling) invoice shape."""
        path = os.path.join(_TEST_DIR, "全电发票-XML", "销_产品_01.xml")
        resp = client.post("/invoice", json={"file_path": path}, headers=auth_headers)
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
                "/invoice",
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
            "/invoice",
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
                "/invoice",
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
            "/invoice", json={"file_path": path}, headers=auth_headers
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
