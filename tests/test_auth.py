"""API 鉴权专项测试。

覆盖：
- KeyStore 加载（simple / structured / disabled）
- FastAPI 依赖的 401 / 403 / 429 / WWW-Authenticate
- 审计日志
- 限速
- 双轨兼容
- dev fallback（未配 key 时全放行）
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.auth import (
    SCOPE_ADMIN,
    SCOPE_EXTRACT,
    KeyRecord,
    KeyStore,
    KeyStoreError,
    reset_key_store_for_tests,
    reset_rate_limiter_for_tests,
)
from fapiao_cn.config import settings
from fapiao_cn.server import app

# ──────────────────────────────────────────────────────────────
# KeyStore 单元测试（不依赖 FastAPI）
# ──────────────────────────────────────────────────────────────


class TestKeyRecord:
    def test_key_id_is_stable_short_sha(self):
        r = KeyRecord(key="abc", label="x", scopes=frozenset({SCOPE_EXTRACT}), rpm=60, burst=10)
        assert len(r.key_id) == 8
        same = KeyRecord(
            key="abc", label="x", scopes=frozenset({SCOPE_EXTRACT}), rpm=60, burst=10
        )
        assert r.key_id == same.key_id

    def test_key_id_different_for_different_keys(self):
        r1 = KeyRecord(key="abc", label="x", scopes=frozenset({SCOPE_EXTRACT}), rpm=60, burst=10)
        r2 = KeyRecord(key="abd", label="x", scopes=frozenset({SCOPE_EXTRACT}), rpm=60, burst=10)
        assert r1.key_id != r2.key_id


class TestKeyStoreSimple:
    def test_simple_mode_loads_all_scopes(self):
        settings.api_keys = "k1,k2,k3"
        settings.api_keys_file = ""
        reset_key_store_for_tests()
        store = KeyStore.from_env()
        assert not store.disabled
        assert len(store) == 3
        for r in [store.verify("k1"), store.verify("k2"), store.verify("k3")]:
            assert r is not None
            assert SCOPE_ADMIN in r.scopes

    def test_unknown_key_returns_none(self):
        settings.api_keys = "real-key"
        reset_key_store_for_tests()
        store = KeyStore.from_env()
        assert store.verify("wrong-key") is None
        assert store.verify("") is None


class TestKeyStoreStructured:
    def test_loads_json_with_scopes_and_rpm(self, tmp_path):
        payload = [
            {"key": "ext", "label": "ext-svc", "scopes": ["extract"]},
            {
                "key": "adm",
                "label": "ops",
                "scopes": ["extract", "verify", "admin"],
                "rpm": 5,
                "burst": 2,
            },
        ]
        p = tmp_path / "keys.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        settings.api_keys = ""
        settings.api_keys_file = str(p)
        reset_key_store_for_tests()

        store = KeyStore.from_env()
        assert not store.disabled
        assert len(store) == 2
        ext = store.verify("ext")
        assert ext is not None and ext.label == "ext-svc"
        assert SCOPE_EXTRACT in ext.scopes and SCOPE_ADMIN not in ext.scopes
        adm = store.verify("adm")
        assert adm is not None and adm.rpm == 5 and adm.burst == 2

    def test_env_keys_takes_priority_over_file(self, tmp_path):
        p = tmp_path / "keys.json"
        p.write_text(json.dumps([{"key": "from-file"}]), encoding="utf-8")
        settings.api_keys_file = str(p)
        settings.api_keys = "from-env"
        reset_key_store_for_tests()
        store = KeyStore.from_env()
        assert store.source.startswith("env:")
        assert store.verify("from-env") is not None
        assert store.verify("from-file") is None

    def test_invalid_json_raises(self, tmp_path):
        """H2: 配置了 JSON 但加载失败必须 fail-fast，绝不静默 disabled。"""
        p = tmp_path / "keys.json"
        p.write_text("{not json}", encoding="utf-8")
        settings.api_keys_file = str(p)
        settings.api_keys = ""
        reset_key_store_for_tests()
        with pytest.raises(KeyStoreError, match="invalid JSON"):
            KeyStore.from_env()

    def test_missing_file_raises(self, tmp_path):
        """H2: 文件不可读必须 raise KeyStoreError。"""
        settings.api_keys_file = str(tmp_path / "nope.json")
        settings.api_keys = ""
        reset_key_store_for_tests()
        with pytest.raises(KeyStoreError, match="unreadable"):
            KeyStore.from_env()

    def test_zero_valid_keys_raises(self, tmp_path):
        """H2: JSON 结构合法但 0 有效 key 也必须 fail-fast（与 disabled 严格区分）。"""
        p = tmp_path / "keys.json"
        p.write_text(json.dumps([{"label": "no-key-field"}]), encoding="utf-8")
        settings.api_keys_file = str(p)
        settings.api_keys = ""
        reset_key_store_for_tests()
        with pytest.raises(KeyStoreError, match="0 valid keys"):
            KeyStore.from_env()

    def test_invalid_rpm_type_raises(self, tmp_path):
        """H2: rpm 字段类型错必须 raise（保护服务不被一条坏 JSON 弄 500）。"""
        p = tmp_path / "keys.json"
        p.write_text(
            json.dumps([{"key": "k", "rpm": "not-an-int"}]),
            encoding="utf-8",
        )
        settings.api_keys_file = str(p)
        settings.api_keys = ""
        reset_key_store_for_tests()
        with pytest.raises(KeyStoreError, match="rpm invalid"):
            KeyStore.from_env()


class TestKeyStoreDisabled:
    def test_no_keys_no_flag_yields_disabled(self):
        settings.api_keys = ""
        settings.api_keys_file = ""
        settings.api_auth_enabled = True
        reset_key_store_for_tests()
        store = KeyStore.from_env()
        assert store.disabled

    def test_explicit_disable(self):
        settings.api_auth_enabled = False
        reset_key_store_for_tests()
        store = KeyStore.from_env()
        assert store.disabled


# ──────────────────────────────────────────────────────────────
# FastAPI 集成测试
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def unauth_client():
    """未配置 key 的 client → dev fallback 模式。"""

    settings.api_auth_enabled = True
    settings.api_keys = ""
    settings.api_keys_file = ""
    settings.api_legacy_allow = False
    reset_key_store_for_tests()
    reset_rate_limiter_for_tests()
    return TestClient(app)


class TestDevFallback:
    """未配置 key 时所有端点（含 verify）应正常 200/422。"""

    def test_extract_works_without_keys(self, unauth_client):
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "samples", "购_材料_01.pdf"
        )
        resp = unauth_client.post("/extract", json={"file_path": path})
        assert resp.status_code == 200
        assert resp.json()["invoice_number"]

    def test_health_full_works_without_keys(self, unauth_client):
        resp = unauth_client.get("/health/full")
        assert resp.status_code in (200, 503)


class TestAuthEnforced:
    """simple mode 有 key 时，所有受保护端点需要 token。"""

    def test_extract_without_token_returns_401(self):
        settings.api_keys = "good-key"
        reset_key_store_for_tests()
        reset_rate_limiter_for_tests()
        client = TestClient(app)
        resp = client.post("/extract", json={"file_path": "/nonexistent"})
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer")
        assert 'realm="fapiao_cn"' in resp.headers["WWW-Authenticate"]

    def test_extract_with_wrong_token_returns_401_invalid(self):
        settings.api_keys = "good-key"
        reset_key_store_for_tests()
        reset_rate_limiter_for_tests()
        client = TestClient(app)
        resp = client.post(
            "/extract",
            headers={"Authorization": "Bearer wrong-key"},
            json={"file_path": "/nonexistent"},
        )
        assert resp.status_code == 401
        assert 'error="invalid_token"' in resp.headers.get("WWW-Authenticate", "")

    def test_extract_with_x_api_key_works(self):
        settings.api_keys = "alt-key"
        reset_key_store_for_tests()
        reset_rate_limiter_for_tests()
        client = TestClient(app)
        resp = client.post(
            "/extract",
            headers={"X-API-Key": "alt-key"},
            json={"file_path": "/nonexistent"},
        )
        # 鉴权通过，但文件不存在 → 4xx 而非 401
        assert resp.status_code != 401

    def test_health_public_no_token_needed(self):
        settings.api_keys = "good-key"
        reset_key_store_for_tests()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200


class TestScopeEnforcement:
    """JSON 模式可配不同 scope，验证 403 + insufficient_scope。"""

    def test_extract_only_cannot_call_health_full(self, keys_file_env):
        client = TestClient(app)
        resp = client.get("/health/full", headers=keys_file_env["extract_only"])
        assert resp.status_code == 403
        wa = resp.headers.get("WWW-Authenticate", "")
        assert 'error="insufficient_scope"' in wa
        assert "scope=" in wa

    def test_extract_only_cannot_call_verify(self, keys_file_env):
        client = TestClient(app)
        resp = client.post(
            "/verify",
            headers=keys_file_env["extract_only"],
            json={"invoice_number": "1"},
        )
        assert resp.status_code == 403

    def test_verify_only_cannot_call_extract(self, keys_file_env):
        import os

        client = TestClient(app)
        path = os.path.join(
            os.path.dirname(__file__), "..", "samples", "购_材料_01.pdf"
        )
        resp = client.post(
            "/extract",
            headers=keys_file_env["verify_only"],
            json={"file_path": path},
        )
        assert resp.status_code == 403

    def test_admin_can_call_everything(self, keys_file_env):

        client = TestClient(app)
        resp = client.get("/health/full", headers=keys_file_env["admin"])
        assert resp.status_code in (200, 503)
        resp = client.get("/formats", headers=keys_file_env["admin"])
        assert resp.status_code == 200
        resp = client.post(
            "/verify",
            headers=keys_file_env["admin"],
            json={"invoice_number": "1"},
        )
        # /verify 当 disabled 时是 503，不是 403
        assert resp.status_code in (200, 503)


class TestRateLimit:
    def test_burst_exhaustion_returns_429(self, keys_file_env, monkeypatch):
        monkeypatch.setattr(settings, "verify_enabled", False)
        client = TestClient(app)
        headers = keys_file_env["rate1"]
        # rate1: rpm=2, burst=1 → 2 次请求后第 3 次应 429
        for _ in range(2):
            r = client.get("/formats", headers=headers)
            assert r.status_code == 200
        r = client.get("/formats", headers=headers)
        assert r.status_code == 429
        wa = r.headers.get("WWW-Authenticate", "")
        assert 'error="rate_limited"' in wa
        assert "Retry-After" in r.headers

    def test_rate_limit_per_key_independent(self, keys_file_env, monkeypatch):
        monkeypatch.setattr(settings, "verify_enabled", False)
        client = TestClient(app)
        # 用完 rate1 的桶
        for _ in range(2):
            client.get("/formats", headers=keys_file_env["rate1"])
        # rate1 触发限速
        assert client.get("/formats", headers=keys_file_env["rate1"]).status_code == 429
        # admin 不受影响
        assert client.get("/formats", headers=keys_file_env["admin"]).status_code == 200


class TestLegacyCompat:
    def test_legacy_allow_lets_unauthenticated_through(self):
        settings.api_keys = "real-key"
        settings.api_legacy_allow = True
        reset_key_store_for_tests()
        reset_rate_limiter_for_tests()
        client = TestClient(app)
        # 无 token 也放行
        r = client.get("/formats")
        assert r.status_code == 200

    def test_legacy_allow_does_not_skip_health(self):
        settings.api_keys = "real-key"
        settings.api_legacy_allow = True
        reset_key_store_for_tests()
        client = TestClient(app)
        assert client.get("/health").status_code == 200


class TestAuditLog:
    def test_auth_event_emits_log(self, keys_file_env, caplog):
        caplog.set_level(logging.INFO, logger="fapiao_cn.audit")
        client = TestClient(app)
        # 401
        client.get("/formats")
        # 200
        client.get("/formats", headers=keys_file_env["admin"])
        # 403
        client.get("/health/full", headers=keys_file_env["extract_only"])

        audit_records = [r for r in caplog.records if r.name == "fapiao_cn.audit"]
        assert len(audit_records) >= 3
        messages = [r.getMessage() for r in audit_records]
        # 至少有一条 401/一条 200/一条 403
        assert any("-> 401" in m for m in messages)
        assert any("-> 200" in m for m in messages)
        assert any("-> 403" in m for m in messages)
        # 401 应带 reason=missing_token
        assert any("reason=missing_token" in m for m in messages)
