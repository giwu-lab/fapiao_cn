"""版本(edition)与能力(feature)模型。

三个互相正交的维度,任何一条不满足都拒绝：

====================== ====================================================
维度                   回答的问题 / 载体
====================== ====================================================
``edition``            装的是什么发行包 —— 由「发行包构成 + 许可证」决定
``feature``            这个版本能做哪些事 —— 许可证授权能力的子集
``scope``              哪个 API key 能用 —— ``auth/keys.py``，与版本无关
====================== ====================================================

**有效权限 = edition ∩ license ∩ key scope。**

没有 ``FC_EDITION`` 之类的环境变量开关：开源版用户改一个环境变量就能解锁
企业版能力，整套商业边界就作废。版本只能由「装了哪个发行包 + 许可证是否
有效」决定，两者都在本进程之外的控制之下。

企业版实现**不在本包内** —— CE ↔ EE 的接缝见 :mod:`fapiao_cn.plugins`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from fastapi import HTTPException, Request

log = logging.getLogger("fapiao_cn.editions")


class Edition(str, Enum):
    """发行版。"""

    CE = "ce"
    EE = "ee"


class Feature(str, Enum):
    """能力。与 key 的 scope 不同 —— scope 是用户自己配的，不是商业边界。"""

    EXTRACT = "extract"
    BATCH = "batch"
    CUSTOMS = "customs"
    ACCEPTANCE = "acceptance"
    VERIFY = "verify"


#: 开源版自带的能力。两个发行版都有。
CE_FEATURES = frozenset({Feature.EXTRACT, Feature.VERIFY})


@dataclass(frozen=True)
class Entitlements:
    """当前进程实际拥有的能力。

    ``reason`` 记录降级原因（``ee_package_absent`` / ``license_absent`` /
    ``license_expired`` / ``license_invalid`` / ``plugin_error`` …），
    供启动日志、``/capabilities`` 与审计日志使用。
    """

    edition: Edition
    features: frozenset[Feature]
    license_id: str = ""
    customer: str = ""
    expires_at: str = ""
    reason: str = ""

    def has(self, feature: Feature) -> bool:
        return feature in self.features

    def to_dict(self) -> dict:
        return {
            "edition": self.edition.value,
            "features": sorted(f.value for f in self.features),
            "license": {
                "status": "active" if self.license_id else "absent",
                "license_id": self.license_id,
                "customer": self.customer,
                "expires_at": self.expires_at,
            },
            "reason": self.reason,
        }


#: 降级到 CE 时的标准能力集。
def _ce_entitlements(reason: str) -> Entitlements:
    return Entitlements(
        edition=Edition.CE,
        features=CE_FEATURES,
        reason=reason,
    )


_ENTITLEMENTS: Optional[Entitlements] = None


def resolve_entitlements(plugin: object = None) -> Entitlements:
    """解析当前进程的能力。

    判定顺序是**保守优先**：任何一步不满足就降级成 CE，绝不因为异常
    而默认放行企业版能力。

    1. 没有企业版插件（发行包里没装 EE）→ CE；
    2. 插件在，但它算出来的授权无效（许可证缺失 / 过期 / 签名错）→ CE；
    3. 插件本身抛异常 → CE（并记日志），不让一个插件 bug 变成越权。
    """
    if plugin is None:
        return _ce_entitlements("ee_package_absent")
    try:
        ent = plugin.entitlements()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — 插件异常必须降级，不能放行
        log.error("edition plugin failed to resolve entitlements: %s", exc)
        return _ce_entitlements("plugin_error")
    if not isinstance(ent, Entitlements):
        log.error("edition plugin returned %r, expected Entitlements", type(ent))
        return _ce_entitlements("plugin_error")
    return ent


def set_entitlements(ent: Entitlements) -> None:
    """覆盖当前能力（SIGHUP 重载许可证时用）。"""
    global _ENTITLEMENTS
    _ENTITLEMENTS = ent


def get_entitlements() -> Entitlements:
    """返回当前能力。尚未初始化时按 CE 兜底。"""
    if _ENTITLEMENTS is None:
        return _ce_entitlements("not_initialized")
    return _ENTITLEMENTS


def reset_entitlements_for_tests() -> None:
    """清空缓存的能力（测试用）。"""
    global _ENTITLEMENTS
    _ENTITLEMENTS = None


def feature_not_available_payload(
    feature: Feature,
    ent: Optional[Entitlements] = None,
) -> dict:
    """企业版能力不可用时的 403 响应体。

    结构化字段让调用方能区分「没鉴权」（401）和「版本不够」（403），
    前端也能据此弹出「升级到企业版」的引导。
    """
    ent = ent or get_entitlements()
    return {
        "error": "feature_not_available",
        "feature": feature.value,
        "required_edition": Edition.EE.value,
        "current_edition": ent.edition.value,
        "reason": ent.reason,
        "upgrade": "https://example.invalid/upgrade",
    }


def require_feature(feature: Feature) -> Callable:
    """FastAPI 依赖：请求该能力，否则 403。

    与 ``auth_required(scope)`` 组合使用时**顺序很重要**：依赖按声明顺序
    求值，前一个抛错即短路。所以必须把鉴权依赖写在前面 —— 无 key 的请求
    先得到 401，不会先撞到 403（否则等于向未鉴权方泄漏「这是企业版
    功能」）。
    """

    async def _dependency(request: Request) -> None:
        ent = get_entitlements()
        if ent.has(feature):
            return
        raise HTTPException(
            status_code=403,
            detail=feature_not_available_payload(feature, ent),
        )

    return _dependency


def capabilities_payload() -> dict:
    """``GET /capabilities`` 的响应体。

    前端据此决定是否显示「批量上传」「报关单解析」入口 —— 这是企业版
    销售最需要的「可见的墙」：用户看得见能力边界，也知道该买什么。
    """
    from fapiao_cn.config import settings

    ent = get_entitlements()
    return {
        "edition": ent.edition.value,
        "features": {f.value: f in ent.features for f in Feature},
        "limits": {
            "max_file_mb": settings.max_file_mb,
            "max_batch_files": settings.max_batch_files,
            "max_batch_total_mb": settings.max_batch_total_mb,
        },
        "license": ent.to_dict()["license"],
        "reason": ent.reason,
    }


__all__ = [
    "CE_FEATURES",
    "Edition",
    "Entitlements",
    "Feature",
    "capabilities_payload",
    "feature_not_available_payload",
    "get_entitlements",
    "require_feature",
    "reset_entitlements_for_tests",
    "resolve_entitlements",
    "set_entitlements",
]
