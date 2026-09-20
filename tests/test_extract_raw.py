"""Tests for the dedicated ``POST /raw`` endpoint."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from fapiao_cn.server import app

client = TestClient(app)

_TEST_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


def _load(name: str) -> bytes:
    with open(os.path.join(_TEST_DIR, name), "rb") as f:
        return f.read()


class TestExtractRaw:
    def test_raw_multipart_pdf(self, auth_headers):
        contents = _load("购_材料_01.pdf")
        resp = client.post(
            "/raw",
            headers=auth_headers,
            files={"file": ("购_材料_01.pdf", contents, "application/pdf")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "text" in body and "lines" in body and "page_count" in body
        assert body["page_count"] >= 1
        assert body["text"]  # non-empty
        assert isinstance(body["lines"], list)

    def test_raw_json_file_path(self, auth_headers):
        path = os.path.join(_TEST_DIR, "购_材料_01.pdf")
        resp = client.post("/raw", headers=auth_headers, json={"file_path": path})
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"]
        assert body["page_count"] >= 1

    def test_raw_json_base64(self, auth_headers):
        import base64

        b64 = base64.b64encode(_load("购_材料_01.pdf")).decode()
        resp = client.post(
            "/raw",
            headers=auth_headers,
            json={"filename": "购_材料_01.pdf", "file_base64": b64},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"]

    def test_raw_no_input_returns_400(self, auth_headers):
        resp = client.post("/raw", headers=auth_headers, json={})
        assert resp.status_code == 400
        assert "Provide file" in resp.json()["detail"]

    def test_raw_empty_file_returns_422(self, auth_headers):
        resp = client.post(
            "/raw",
            headers=auth_headers,
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["message"].startswith("error: parse_failed:")

    def test_raw_xml_returns_400(self, auth_headers):
        """``/raw`` is PDF-only — XML inputs must be rejected."""
        xml_bytes = _load("购_材料_01.xml")
        resp = client.post(
            "/raw",
            headers=auth_headers,
            files={"file": ("购_材料_01.xml", xml_bytes, "application/xml")},
        )
        assert resp.status_code == 400
        assert "raw_unsupported" in resp.json()["detail"]

    def test_raw_xml_via_json_returns_400(self, auth_headers):
        """XML detection must also work when routed via JSON ``file_path``."""
        resp = client.post(
            "/raw",
            headers=auth_headers,
            json={"file_path": os.path.join(_TEST_DIR, "购_材料_01.xml")},
        )
        assert resp.status_code == 400
        assert "raw_unsupported" in resp.json()["detail"]

    def test_raw_response_shape_only_has_four_fields(self, auth_headers):
        """``/raw`` should emit ONLY ``text`` / ``lines`` / ``page_count`` / ``message``."""
        resp = client.post(
            "/raw",
            headers=auth_headers,
            files={"file": ("购_材料_01.pdf", _load("购_材料_01.pdf"), "application/pdf")},
        )
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"text", "lines", "page_count", "message"}
