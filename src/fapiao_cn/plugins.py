"""企业版插件契约（CE ↔ EE 的接缝）。

开源版 ``fapiao_cn`` 的 wheel 里**不含任何企业版实现**。企业版以
**独立的 distribution** ``fapiao_cn_ee`` 发布，依赖 ``fapiao_cn==<同版本>``；
装上之后由本模块发现并加载。

CE 侧只依赖下面这几个方法，EE 侧怎么实现都行：

``name``
    插件名，用于日志。

``entitlements() -> Entitlements``
    结合许可证给出**实际**能力。许可证缺失 / 过期 / 签名不合法时必须
    返回 ``edition=ce`` 的 Entitlements（降级），不能"放行"。

``register_customs_route(app)`` / ``register_batch_route(app)``
    注册企业版路由。插件缺席时 CE 注册 403 占位路由，保证两版路由表
    一致 —— 客户端不会因为版本差异拿到 404。

``sync_parsers(entitled: bool)``
    按授权状态同步 dispatch 规则（幂等）。企业版的报关单规则必须**只在
    被授权时**注册，否则未授权的安装会通过 ``/formats`` 泄漏报关单能力。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from typing import Optional, Protocol

from fastapi import FastAPI

from fapiao_cn.editions import Entitlements

log = logging.getLogger("fapiao_cn.plugins")

#: 企业版发行包名。CE 的 wheel 里不存在这个模块。
EE_PACKAGE = "fapiao_cn_ee"


class FapiaoPlugin(Protocol):
    """企业版插件需要实现的接口。"""

    name: str

    def entitlements(self) -> Entitlements:
        """返回当前实际能力（已结合许可证）。"""
        ...

    def register_customs_route(self, app: FastAPI) -> None:
        """注册 ``POST /extract/customs`` 的真实实现。"""
        ...

    def register_batch_route(self, app: FastAPI) -> None:
        """注册 ``POST /extract/batch`` 的真实实现。"""
        ...

    def sync_parsers(self, entitled: bool) -> None:
        """按授权状态同步 dispatch 规则（幂等）。"""
        ...


def load_plugin() -> Optional[FapiaoPlugin]:
    """发现并加载企业版插件；没安装则返回 ``None``。

    找不到模块是正常情况（开源版），不算错误；装了但导入失败则是安装
    出了问题，要记日志 —— 但同样只降级为 CE，不因此放行任何企业版能力。
    """
    try:
        spec = importlib.util.find_spec(EE_PACKAGE)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None
    try:
        module = importlib.import_module(EE_PACKAGE)
    except Exception as exc:  # noqa: BLE001 — 插件坏了只能降级
        log.error("failed to import %s: %s", EE_PACKAGE, exc)
        return None
    plugin = getattr(module, "PLUGIN", None)
    if plugin is None:
        log.error("%s does not export PLUGIN", EE_PACKAGE)
        return None
    return plugin


__all__ = ["EE_PACKAGE", "FapiaoPlugin", "load_plugin"]
