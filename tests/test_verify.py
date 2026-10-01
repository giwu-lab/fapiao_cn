"""Tests for the optional 国家税务总局 verification proxy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.config import settings
from fapiao_cn.server import app

client = TestClient(app)

def _fake_response(body: dict) -> MagicMock:
    """Build a mock httpx.Response.

    ``verify_invoice`` streams the body via ``resp.aiter_bytes()`` to
    enforce a size cap (H11).  We mimic that by exposing an async
    iterator that yields the JSON-encoded bytes plus a sync ``headers``
    dict that carries the ``content-length`` hint.
    """

    import json as _json

    raw = _json.dumps(body).encode("utf-8")

    async def _aiter(_chunk_size: int = 65536):
        yield raw

    m = MagicMock()
    m.status_code = 200
    m.raise_for_status.return_value = None
    m.headers = {"content-length": str(len(raw))}
    m.aiter_bytes = _aiter
    return m


@pytest.fixture(autouse=True)
def _restore_verify_settings():
    """Snapshot verify settings before each test, restore after."""
    saved = {
        "enabled": settings.verify_enabled,
        "endpoint": settings.verify_endpoint,
        "api_key": settings.verify_api_key,
        "timeout": settings.verify_timeout,
    }
    yield
    settings.verify_enabled = saved["enabled"]
    settings.verify_endpoint = saved["endpoint"]
    settings.verify_api_key = saved["api_key"]
    settings.verify_timeout = saved["timeout"]


class TestVerifyDisabled:
    def test_default_disabled_returns_503(self, auth_headers):
        settings.verify_enabled = False
        settings.verify_endpoint = ""
        resp = client.post(
            "/verify",
            headers=auth_headers,
            json={"invoice_number": "12345", "issue_date": "2025-01-01"},
        )
        assert resp.status_code == 503
        assert "verify_disabled" in resp.text

    def test_enabled_but_no_endpoint_returns_503(self, auth_headers):
        settings.verify_enabled = True
        settings.verify_endpoint = ""
        resp = client.post(
            "/verify",
            headers=auth_headers,
            json={"invoice_number": "12345"},
        )
        assert resp.status_code == 503
        assert "verify_unconfigured" in resp.text


class TestVerifyProxiesToEndpoint:
    def test_forwards_request_and_returns_response(self, auth_headers):
        settings.verify_enabled = True
        settings.verify_endpoint = "http://mock-proxy/verify"
        settings.verify_api_key = ""

        mock_response = _fake_response(
            {"valid": True, "source": "official", "detail": {"foo": "bar"}}
        )

        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=mock_response),
        ) as mock_post:
            resp = client.post(
                "/verify",
                headers=auth_headers,
                json={
                    "invoice_number": "25502000000129241041",
                    "issue_date": "2025-11-27",
                    "total_amount": "140141.09",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["source"] == "official"
        assert body["detail"] == {"foo": "bar"}

        mock_post.assert_awaited_once()
        kwargs = mock_post.await_args.kwargs
        assert kwargs["json"]["invoice_number"] == "25502000000129241041"
        assert "Authorization" not in kwargs.get("headers", {})

    def test_sends_bearer_when_api_key_set(self, auth_headers):
        settings.verify_enabled = True
        settings.verify_endpoint = "http://mock-proxy/verify"
        settings.verify_api_key = "secret-token-xyz"

        mock_response = _fake_response(
            {"valid": False, "source": "official", "detail": {}}
        )

        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=mock_response),
        ) as mock_post:
            resp = client.post("/verify", headers=auth_headers, json={"invoice_number": "1"})

        assert resp.status_code == 200
        headers = mock_post.await_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer secret-token-xyz"

    def test_proxy_error_returns_502(self, auth_headers):
        settings.verify_enabled = True
        settings.verify_endpoint = "http://mock-proxy/verify"

        import httpx

        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=httpx.HTTPError("upstream down")),
        ):
            resp = client.post("/verify", headers=auth_headers, json={"invoice_number": "1"})

        assert resp.status_code == 502
        assert "verify_proxy_error" in resp.text


class TestExtractAndVerify:
    def test_extract_works_without_verify(self, auth_headers):
        """When verify is disabled, /extract-and-verify returns the
        plain extraction result."""
        import os

        settings.verify_enabled = False
        sample = os.path.join(
            os.path.dirname(__file__), "..", "samples", "购_材料_01.pdf"
        )
        with open(sample, "rb") as f:
            resp = client.post(
                "/extract-and-verify",
                headers=auth_headers,
                files={"file": ("购_材料_01.pdf", f, "application/pdf")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["invoice_number"] == "25502000000129241041"
        assert "verify_result" not in body

    def test_extract_attaches_verify_when_enabled(self, auth_headers):
        import os

        settings.verify_enabled = True
        settings.verify_endpoint = "http://mock-proxy/verify"
        settings.verify_api_key = ""

        mock_response = _fake_response(
            {"valid": True, "source": "official", "detail": {}}
        )

        sample = os.path.join(
            os.path.dirname(__file__), "..", "samples", "购_材料_01.pdf"
        )
        with open(sample, "rb") as f, patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=mock_response),
        ):
            resp = client.post(
                "/extract-and-verify",
                headers=auth_headers,
                files={"file": ("购_材料_01.pdf", f, "application/pdf")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["invoice_number"] == "25502000000129241041"
        assert body["verify_result"]["valid"] is True

    def test_extract_records_verify_error_when_proxy_fails(self, auth_headers):
        import os

        import httpx

        settings.verify_enabled = True
        settings.verify_endpoint = "http://mock-proxy/verify"

        sample = os.path.join(
            os.path.dirname(__file__), "..", "samples", "购_材料_01.pdf"
        )
        with open(sample, "rb") as f, patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=httpx.HTTPError("down")),
        ):
            resp = client.post(
                "/extract-and-verify",
                headers=auth_headers,
                files={"file": ("购_材料_01.pdf", f, "application/pdf")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["invoice_number"] == "25502000000129241041"
        assert "verify_error" in body
        assert "down" in body["verify_error"]
