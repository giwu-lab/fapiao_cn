"""Tests for the /extract/batch endpoint."""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient

from fapiao_cn.server import app

client = TestClient(app)

TEST_DIR = Path(__file__).resolve().parent.parent / "samples"


def _load(name: str) -> bytes:
    """``name`` is a path relative to ``samples/`` (may include a subdir)."""

    return (TEST_DIR / name).read_bytes()


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
        resp = client.post("/extract/batch", headers=auth_headers, files=data)
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
        resp = client.post("/extract/batch", headers=auth_headers, files=data)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["message"] == "success"
        assert body[1]["message"].startswith("error:")

    def test_internal_error_logged(self, auth_headers, caplog, monkeypatch):
        """H4: an unexpected parse exception must be logged with a traceback."""
        import logging

        def _boom(contents, filename=""):
            raise RuntimeError("simulated parser crash")

        monkeypatch.setattr("fapiao_cn.server.parse_pdf", _boom)

        data = (
            ("files", (
                "火车票01.pdf",
                io.BytesIO(_load("铁路电子客票/火车票01.pdf")),
                "application/pdf",
            )),
        )
        with caplog.at_level(logging.ERROR, logger="fapiao_cn.server"):
            resp = client.post("/extract/batch", headers=auth_headers, files=data)
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
        resp = client.post("/extract/batch", headers=auth_headers, files=tuple(files))
        assert resp.status_code == 400
        assert "Too many files" in resp.json()["detail"]

    def test_single_file_too_large(self, auth_headers):
        """A single file > max_batch_single_mb should return 400."""
        big = b"x" * (11 * 1024 * 1024)  # 11 MB
        data = (
            ("files", ("big.pdf", io.BytesIO(big), "application/pdf")),
        )
        resp = client.post("/extract/batch", headers=auth_headers, files=data)
        assert resp.status_code == 400
        assert "File too large" in resp.json()["detail"]

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
        resp = client.post("/extract/batch", headers=auth_headers, files=data)
        assert resp.status_code == 400
        assert "Total size too large" in resp.json()["detail"]

    def test_no_files(self, auth_headers):
        """Missing 'files' field should return 400."""
        resp = client.post("/extract/batch", headers=auth_headers)
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
        resp = client.post("/extract/batch", headers=auth_headers, json=payload)
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
        resp = client.post("/extract/batch", headers=auth_headers, json=payload)
        assert resp.status_code == 400
        assert "Invalid base64" in resp.json()["detail"]

    def test_json_missing_base64(self, auth_headers):
        """JSON missing base64 field should return 400."""
        payload = {
            "files": [
                {"filename": "bad.pdf"},
            ]
        }
        resp = client.post("/extract/batch", headers=auth_headers, json=payload)
        assert resp.status_code == 400
        assert "Missing 'base64'" in resp.json()["detail"]
