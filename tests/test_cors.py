"""Tests for CORS middleware configuration."""
from __future__ import annotations

from fastapi.testclient import TestClient

from fapiao_cn.server import app

client = TestClient(app)


class TestCors:
    def test_options_preflight_returns_allow_origin(self):
        """Preflight OPTIONS /invoice must include Access-Control-Allow-Origin."""
        resp = client.options(
            "/invoice",
            headers={
                "Origin": "http://192.168.3.160:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type,Authorization",
            },
        )
        assert resp.status_code in (200, 204)
        keys = {k.lower() for k in resp.headers.keys()}
        assert "access-control-allow-origin" in keys

    def test_post_includes_allow_origin_header(self, auth_headers):
        """POST response must include CORS header regardless of parse outcome."""
        resp = client.post(
            "/invoice",
            headers={
                **auth_headers,
                "Origin": "http://192.168.3.160:3000",
            },
            files={"file": ("t.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code in (200, 400, 422, 500)
        keys = {k.lower() for k in resp.headers.keys()}
        assert "access-control-allow-origin" in keys
