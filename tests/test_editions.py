"""发行版 / 能力门禁的测试。

三条不变量：

1. **没有环境变量开关** —— 改 ``FC_EDITION`` 之类的环境变量不能解锁
   企业版能力；
2. **鉴权优先于版本判定** —— 无 key 的请求先得到 401，不会先撞到 403
   （否则等于向未鉴权方泄漏"这是企业版功能"）；
3. **未授权的能力一律 403 + 结构化 body**，而不是 404（两版路由表一致）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fapiao_cn.editions import (
    CE_FEATURES,
    Edition,
    Entitlements,
    Feature,
    feature_not_available_payload,
    get_entitlements,
    require_feature,
    resolve_entitlements,
    set_entitlements,
)
from fapiao_cn.server import app
from tests._helpers import ee_feature_enabled

client = TestClient(app)


class TestEntitlementsModel:
    def test_no_plugin_resolves_to_ce(self):
        """没有企业版发行包 → 开源版。"""
        ent = resolve_entitlements(None)
        assert ent.edition is Edition.CE
        assert ent.features == CE_FEATURES
        assert ent.reason == "ee_package_absent"

    def test_plugin_error_degrades_to_ce(self):
        """插件抛异常必须降级成 CE，不能因此放行企业版能力。"""

        class Broken:
            def entitlements(self):
                raise RuntimeError("boom")

        ent = resolve_entitlements(Broken())
        assert ent.edition is Edition.CE
        assert ent.features == CE_FEATURES
        assert ent.reason == "plugin_error"

    def test_plugin_returning_wrong_type_degrades(self):
        class Wrong:
            def entitlements(self):
                return {"edition": "ee"}

        ent = resolve_entitlements(Wrong())
        assert ent.edition is Edition.CE
        assert ent.reason == "plugin_error"

    def test_ce_features_exclude_enterprise_ones(self):
        assert Feature.BATCH not in CE_FEATURES
        assert Feature.CUSTOMS not in CE_FEATURES
        assert Feature.EXTRACT in CE_FEATURES

    def test_no_edition_env_switch(self, monkeypatch):
        """环境变量不能提升发行版 —— 商业边界不能靠改配置绕过。"""
        monkeypatch.setenv("FC_EDITION", "ee")
        monkeypatch.setenv("FC_LICENSE_FILE", "")
        ent = resolve_entitlements(None)
        assert ent.edition is Edition.CE
        assert not ent.has(Feature.BATCH)
        assert not ent.has(Feature.CUSTOMS)


class TestFeatureGate:
    def test_require_feature_raises_403_when_missing(self):
        """依赖在能力缺失时抛 403，并带结构化 body。"""
        from fastapi import HTTPException

        dep = require_feature(Feature.BATCH)
        original = get_entitlements()
        set_entitlements(
            Entitlements(edition=Edition.CE, features=CE_FEATURES, reason="test")
        )
        try:
            with pytest.raises(HTTPException) as excinfo:
                import asyncio

                asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                    dep(_FakeRequest())
                )
            assert excinfo.value.status_code == 403
            detail = excinfo.value.detail
            assert detail["error"] == "feature_not_available"
            assert detail["feature"] == "batch"
            assert detail["required_edition"] == "ee"
            assert detail["current_edition"] == "ce"
        finally:
            set_entitlements(original)

    def test_payload_shape(self):
        payload = feature_not_available_payload(
            Feature.CUSTOMS, Entitlements(edition=Edition.CE, features=CE_FEATURES)
        )
        assert payload["feature"] == "customs"
        assert payload["error"] == "feature_not_available"
        assert payload["required_edition"] == "ee"


class _FakeRequest:
    """最小 Request 替身 —— 依赖只用到 state。"""

    def __init__(self) -> None:
        self.state = type("S", (), {})()


class TestCapabilityEndpoint:
    def test_capabilities_shape(self):
        resp = client.get("/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["edition"] in ("ce", "ee")
        assert set(data["features"]) == {"extract", "batch", "customs", "verify"}
        assert data["features"]["extract"] is True
        assert "limits" in data
        assert "max_file_mb" in data["limits"]
        assert "license" in data

    def test_capabilities_matches_runtime_entitlement(self):
        ent = get_entitlements()
        data = client.get("/capabilities").json()
        assert data["edition"] == ent.edition.value
        for feature in Feature:
            assert data["features"][feature.value] is ent.has(feature)


class TestEnterpriseRoutes:
    """两个企业版端点的门禁行为（两个发行版都适用）。"""

    def test_unauthenticated_gets_401_not_403(self):
        """鉴权优先：没有 key 先 401，不泄漏版本信息。"""
        resp = client.post(
            "/extract/customs",
            files={"file": ("x.pdf", b"%PDF-1.4")},
        )
        assert resp.status_code == 401
        resp = client.post(
            "/extract/batch",
            files=[("files", ("x.pdf", b"%PDF-1.4"))],
        )
        assert resp.status_code == 401

    def test_customs_endpoint_gated(self, auth_headers):
        from pathlib import Path

        if ee_feature_enabled("customs"):
            sample = (
                Path(__file__).resolve().parent.parent
                / "samples"
                / "报关单"
                / "L20260602000010_0429.pdf"
            )
            if not sample.exists():
                pytest.skip("sample not available")
            resp = client.post(
                "/extract/customs",
                headers=auth_headers,
                files={"file": ("bgd.pdf", sample.read_bytes())},
            )
            assert resp.status_code == 200
            assert resp.json()["customs_no"] == "223120260001921706"
        else:
            resp = client.post(
                "/extract/customs",
                headers=auth_headers,
                files={"file": ("x.pdf", b"%PDF-1.4")},
            )
            assert resp.status_code == 403
            detail = resp.json()["detail"]
            assert detail["error"] == "feature_not_available"
            assert detail["feature"] == "customs"
            assert detail["required_edition"] == "ee"

    def test_batch_endpoint_gated(self, auth_headers):
        resp = client.post(
            "/extract/batch",
            headers=auth_headers,
            files=[("files", ("x.pdf", b"%PDF-1.4"))],
        )
        if ee_feature_enabled("batch"):
            assert resp.status_code == 200
        else:
            assert resp.status_code == 403
            detail = resp.json()["detail"]
            assert detail["error"] == "feature_not_available"
            assert detail["feature"] == "batch"

    def test_routes_exist_in_both_editions(self):
        """两版路由表一致 —— 客户端不会因为版本差异拿到 404。"""
        paths = {route.path for route in app.routes}
        assert "/extract/batch" in paths
        assert "/extract/customs" in paths

    def test_openapi_marks_enterprise_paths(self):
        schema = app.openapi()
        for path in ("/extract/batch", "/extract/customs"):
            assert path in schema["paths"]
            assert schema["paths"][path]["post"]["x-required-edition"] == "ee"


class TestParserRegistry:
    def test_customs_rule_follows_entitlement(self):
        """报关单规则只在被授权时注册 —— 否则 /formats 会泄漏能力。"""
        from fapiao_cn.parsers import _RULES, get_format_descriptions

        names = {name for name, _, _ in _RULES}
        if ee_feature_enabled("customs"):
            assert "customs_declaration" in names
            assert "customs_declaration" in get_format_descriptions()
        else:
            assert "customs_declaration" not in names
            assert "customs_declaration" not in get_format_descriptions()

    def test_register_external_is_idempotent(self):
        from fapiao_cn.parsers import register_external, unregister_external

        before = len(__import__("fapiao_cn.parsers", fromlist=["_RULES"])._RULES)
        try:
            register_external("probe", ("zzz-probe",), lambda text, ctx: None, "probe")
            register_external("probe", ("zzz-probe",), lambda text, ctx: None, "probe")
            from fapiao_cn.parsers import _RULES

            assert len([r for r in _RULES if r[0] == "probe"]) == 1
        finally:
            unregister_external("probe")
        from fapiao_cn.parsers import _RULES

        assert len(_RULES) == before
