"""Tests for the /invoice/batch endpoint."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.server import app
from tests._helpers import ee_feature_enabled

client = TestClient(app)

TEST_DIR = Path(__file__).resolve().parent.parent / "samples"


def _load(name: str) -> bytes:
    """``name`` is a path relative to ``samples/`` (may include a subdir)."""

    return (TEST_DIR / name).read_bytes()


@pytest.mark.skipif(
    not ee_feature_enabled("batch"),
    reason="企业版能力：需要安装 fapiao_cn_ee 并提供有效许可证",
)
class TestBatchExtract:
    def test_two_valid_pdfs(self, auth_headers):
        """Two valid invoices should return a list of two results."""
        data = (
            ("files", (
                "火车票01.pdf",
                io.BytesIO(_load("铁路电子客票/火车票01.pdf")),
                "application/pdf",
            )),
            ("files", (
                "火车票02.pdf",
                io.BytesIO(_load("铁路电子客票/火车票02.pdf")),
                "application/pdf",
            )),
        )
        resp = client.post("/invoice/batch", headers=auth_headers, files=data)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert body[0]["file"] == "火车票01.pdf"
        assert body[0]["invoice_type"] == "电子发票（铁路电子客票）"
        assert body[0]["message"] == "success"
        assert body[1]["file"] == "火车票02.pdf"
        assert body[1]["message"] == "success"

    def test_one_valid_one_broken(self, auth_headers):
        """A valid PDF and a non-PDF should each yield a result."""
        data = (
            ("files", (
                "火车票01.pdf",
                io.BytesIO(_load("铁路电子客票/火车票01.pdf")),
                "application/pdf",
            )),
            ("files", ("bad.txt", io.BytesIO(b"not a pdf"), "text/plain")),
        )
        resp = client.post("/invoice/batch", headers=auth_headers, files=data)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["message"] == "success"
        assert body[1]["message"].startswith("error:")

    def test_internal_error_logged(self, auth_headers, caplog, monkeypatch):
        """H4: an unexpected parse exception must be logged with a traceback."""
        import logging

        import fapiao_cn_ee.batch as ee_batch

        def _boom(contents, filename=""):
            raise RuntimeError("simulated parser crash")

        # 批量实现在企业版发行包里，所以要 patch 它自己引用的 parse_pdf
        # （patch fapiao_cn.server.parse_pdf 对它没有影响）。
        monkeypatch.setattr(ee_batch, "parse_pdf", _boom)

        data = (
            ("files", (
                "火车票01.pdf",
                io.BytesIO(_load("铁路电子客票/火车票01.pdf")),
                "application/pdf",
            )),
        )
        with caplog.at_level(logging.ERROR, logger="fapiao_cn.ee.batch"):
            resp = client.post("/invoice/batch", headers=auth_headers, files=data)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        # The batch swallows the exception, so we still get a response
        # item, but caplog should now carry the traceback.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("batch worker failed" in m for m in msgs), msgs

    def test_too_many_files(self, auth_headers):
        """Exceeding max_batch_files should return 400."""
        files = []
        for i in range(51):
            files.append(
                ("files", (f"dummy{i}.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf"))
            )
        resp = client.post("/invoice/batch", headers=auth_headers, files=tuple(files))
        assert resp.status_code == 400
        assert "Too many files" in resp.json()["detail"]

    def test_single_file_too_large(self, monkeypatch, auth_headers):
        """A single file > FC_MAX_FILE_MB should return 400."""
        from fapiao_cn.config import settings
        # 临时下调单文件上限，避免为测试真分配整份 FC_MAX_FILE_MB
        monkeypatch.setattr(settings, "max_file_mb", 1)
        big = b"x" * (2 * 1024 * 1024)  # 2 MB
        data = (
            ("files", ("big.pdf", io.BytesIO(big), "application/pdf")),
        )
        resp = client.post("/invoice/batch", headers=auth_headers, files=data)
        assert resp.status_code == 400
        assert "File too large" in resp.json()["detail"]

    def test_single_file_limit_shared_with_extract(self, monkeypatch, auth_headers):
        """/invoice 与 /invoice/batch 必须共用同一个单文件上限 FC_MAX_FILE_MB。

        同一个超限文件送两个端点都要被拒，只是状态码语义不同：
        批量入口在闸门处整批 400，单文件端点由 parse_pdf 兜底返回 422。
        """
        from fapiao_cn.config import settings

        monkeypatch.setattr(settings, "max_file_mb", 1)
        big = b"x" * (2 * 1024 * 1024)  # 2 MB > 1 MB

        single = client.post(
            "/invoice",
            headers=auth_headers,
            files={"file": ("big.pdf", io.BytesIO(big), "application/pdf")},
        )
        assert single.status_code == 422
        assert "file_too_large" in single.json()["message"]

        batch = client.post(
            "/invoice/batch",
            headers=auth_headers,
            files=(("files", ("big.pdf", io.BytesIO(big), "application/pdf")),),
        )
        assert batch.status_code == 400
        assert "File too large" in batch.json()["detail"]

    def test_file_within_single_limit_passes_gate(self, monkeypatch, auth_headers):
        """闸门只拦超限文件：未超过 FC_MAX_FILE_MB 的文件必须能进解析流程。

        与 test_single_file_limit_shared_with_extract 互为反向断言，
        防止把上限写死或写错（例如误用 max_batch_total_mb 之类）。
        """
        from fapiao_cn.config import settings

        monkeypatch.setattr(settings, "max_file_mb", 1)
        payload = b"x" * (512 * 1024)  # 0.5 MB < 1 MB
        data = (
            ("files", ("ok.pdf", io.BytesIO(payload), "application/pdf")),
        )
        resp = client.post("/invoice/batch", headers=auth_headers, files=data)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        # 内容不是 PDF，解析失败只体现在这一项，整批未被闸门拒绝
        assert body[0]["message"].startswith("error:")
        assert "file_too_large" not in body[0]["message"]

    def test_total_size_too_large(self, monkeypatch, auth_headers):
        """Total size > max_batch_total_mb should return 400."""
        from fapiao_cn.config import settings
        # Temporarily lower total limit so small files can trigger it
        monkeypatch.setattr(settings, "max_batch_total_mb", 1)
        chunk = b"x" * (512 * 1024)  # 0.5 MB each
        data = tuple(
            ("files", (f"{i}.pdf", io.BytesIO(chunk), "application/pdf"))
            for i in range(3)  # 3 * 0.5 = 1.5 MB > 1 MB
        )
        resp = client.post("/invoice/batch", headers=auth_headers, files=data)
        assert resp.status_code == 400
        assert "Total size too large" in resp.json()["detail"]

    def test_no_files(self, auth_headers):
        """Missing 'files' field should return 400."""
        resp = client.post("/invoice/batch", headers=auth_headers)
        assert resp.status_code == 400
        assert "multipart" in resp.json()["detail"].lower() or "No files" in resp.json()["detail"]

    def test_json_two_valid(self, auth_headers):
        """JSON with two valid base64 files."""
        import base64
        a = base64.b64encode(_load("铁路电子客票/火车票01.pdf")).decode()
        b = base64.b64encode(_load("铁路电子客票/火车票02.pdf")).decode()
        payload = {
            "files": [
                {"filename": "火车票01.pdf", "base64": a},
                {"filename": "火车票02.pdf", "base64": b},
            ]
        }
        resp = client.post("/invoice/batch", headers=auth_headers, json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["file"] == "火车票01.pdf"
        assert body[0]["invoice_type"] == "电子发票（铁路电子客票）"
        assert body[0]["message"] == "success"
        assert body[1]["file"] == "火车票02.pdf"
        assert body[1]["message"] == "success"

    def test_json_invalid_base64(self, auth_headers):
        """JSON with invalid base64 should return 400."""
        payload = {
            "files": [
                {"filename": "bad.pdf", "base64": "not-valid-base64!!!"},
            ]
        }
        resp = client.post("/invoice/batch", headers=auth_headers, json=payload)
        assert resp.status_code == 400
        assert "Invalid base64" in resp.json()["detail"]

    def test_json_missing_base64(self, auth_headers):
        """JSON 里一个来源都没给 → 400，并列出全部可用来源。"""
        payload = {
            "files": [
                {"filename": "bad.pdf"},
            ]
        }
        resp = client.post("/invoice/batch", headers=auth_headers, json=payload)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Missing" in detail
        assert "url" in detail and "file_path" in detail and "base64" in detail

    def test_json_with_url_input(self, auth_headers):
        """JSON 的 ``files[]`` 里直接给 URL —— 与 /invoice 的 url 入参对齐。"""
        from urllib.parse import quote

        from tests._helpers import serve_samples

        with serve_samples() as base:
            url = f"{base}/{quote('铁路电子客票/火车票01.pdf')}"
            payload = {"files": [{"filename": "火车票01.pdf", "url": url}]}
            resp = client.post("/invoice/batch", headers=auth_headers, json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["file"] == "火车票01.pdf"
        assert body[0]["invoice_type"] == "电子发票（铁路电子客票）"
        assert body[0]["message"] == "success"

    def test_multipart_urls_field(self, auth_headers):
        """multipart 的 ``urls`` 字段 —— 远端文件地址。"""
        from urllib.parse import quote

        from tests._helpers import serve_samples

        with serve_samples() as base:
            url = f"{base}/{quote('铁路电子客票/火车票01.pdf')}"
            resp = client.post(
                "/invoice/batch",
                headers=auth_headers,
                files=[("urls", (None, url))],
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["invoice_type"] == "电子发票（铁路电子客票）"
        assert body[0]["message"] == "success"

    def test_mixed_upload_and_url_keep_order(self, auth_headers):
        """上传文件与远端 URL 混传 —— 结果顺序与输入顺序一致。"""
        from urllib.parse import quote

        from tests._helpers import serve_samples

        with serve_samples() as base:
            url = f"{base}/{quote('铁路电子客票/火车票02.pdf')}"
            resp = client.post(
                "/invoice/batch",
                headers=auth_headers,
                files=[
                    ("files", ("火车票01.pdf", _load("铁路电子客票/火车票01.pdf"),
                               "application/pdf")),
                    ("urls", (None, url)),
                ],
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [item["file"] for item in body] == ["火车票01.pdf", "火车票02.pdf"]
        assert all(item["message"] == "success" for item in body)

    def test_json_top_level_url_and_files_conflict(self, auth_headers):
        """顶层 url 与 files 数组同时给 → 400，不再静默忽略顶层 url。"""
        import base64
        from urllib.parse import quote

        from tests._helpers import serve_samples

        payload_b64 = base64.b64encode(_load("铁路电子客票/火车票01.pdf")).decode()
        with serve_samples() as base:
            url = f"{base}/{quote('铁路电子客票/火车票02.pdf')}"
            resp = client.post(
                "/invoice/batch",
                headers=auth_headers,
                json={
                    "files": [{"filename": "火车票01.pdf", "base64": payload_b64}],
                    "url": url,
                },
            )
        assert resp.status_code == 400
        assert "not both" in resp.json()["detail"]

    def test_unreachable_url(self, auth_headers):
        """远端抓不到 → 400。"""
        from urllib.parse import quote

        from tests._helpers import serve_samples

        with serve_samples() as base:
            url = f"{base}/{quote('铁路电子客票/不存在.pdf')}"
            resp = client.post(
                "/invoice/batch",
                headers=auth_headers,
                json={"files": [{"filename": "x.pdf", "url": url}]},
            )
        assert resp.status_code == 400
        assert "Failed to fetch URL" in resp.json()["detail"]
