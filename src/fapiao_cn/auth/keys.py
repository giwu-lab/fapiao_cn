"""API key 加载与校验。

优先级：
1. ``FC_API_KEYS``（env，逗号分隔）—— 简单模式，所有 key 为 extract 作用域、用默认 rpm/burst
2. ``FC_API_KEYS_FILE``（env，文件路径）—— 结构化模式，JSON 数组
3. 两者都空 → KeyStore 处于 ``disabled`` 状态；启动 WARN + 全量放行（dev 兜底）

关键策略：
- 「未配置」与「配置了但坏」严格区分。前者保 dev 兜底，后者必须 ``KeyStoreError``。
  避免「挂载错误 / JSON typo / 0 有效 key」时服务**静默全放行** —— 这就是 H2。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fapiao_cn.config import settings

log = logging.getLogger("fapiao_cn.auth")

# 已知作用域；与 server.py 路由一一对应
SCOPE_EXTRACT = "extract"
SCOPE_VERIFY = "verify"
SCOPE_ADMIN = "admin"
ALL_SCOPES = frozenset({SCOPE_EXTRACT, SCOPE_VERIFY, SCOPE_ADMIN})


class KeyStoreError(RuntimeError):
    """配置了但加载失败时抛出。

    与「dev 兜底 disabled」严格区分 —— 误以为静默放行会让鉴权在生产失效。
    """


@dataclass(frozen=True)
class KeyRecord:
    """单把 key 的元数据。"""

    key: str
    label: str
    scopes: frozenset[str]
    rpm: int
    burst: int

    @property
    def key_id(self) -> str:
        """短、不可逆、用于日志的 key 标识（sha256 前 8 位）。"""

        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:8]


class KeyStore:
    """内存中的 key 仓库；启动期一次性构建，运行时只读。"""

    def __init__(
        self,
        records: list[KeyRecord],
        *,
        source: str = "",
        disabled: bool = False,
    ):
        self._records: dict[str, KeyRecord] = {r.key: r for r in records}
        self._source = source
        self._disabled = disabled

    @property
    def disabled(self) -> bool:
        """True 表示未配置任何 key —— 鉴权全放行（WARN 已记）。"""

        return self._disabled

    @property
    def source(self) -> str:
        return self._source

    def __len__(self) -> int:
        return len(self._records)

    def verify(self, presented_key: str) -> Optional[KeyRecord]:
        """恒定时间比对，匹配返回 ``KeyRecord``，否则 None。"""

        if not presented_key:
            return None
        if self._disabled:
            return None
        for rec in self._records.values():
            if hmac.compare_digest(rec.key, presented_key):
                return rec
        return None

    @classmethod
    def from_env(cls) -> "KeyStore":
        """根据当前 ``settings`` 构造。"""

        if not settings.api_auth_enabled:
            log.warning(
                "api auth explicitly disabled via FC_API_AUTH_ENABLED=false; "
                "all endpoints are public"
            )
            return cls(records=[], source="disabled", disabled=True)

        # 1) FC_API_KEYS 优先
        raw = (settings.api_keys or "").strip()
        if raw:
            keys = [k.strip() for k in raw.split(",") if k.strip()]
            records = [
                KeyRecord(
                    key=k,
                    label=f"env#{i}",
                    # simple mode 默认给全部 scope（含 admin）；用 JSON 文件模式做精细控制
                    scopes=frozenset(ALL_SCOPES),
                    rpm=settings.api_key_default_rpm,
                    burst=settings.api_key_default_burst,
                )
                for i, k in enumerate(keys)
            ]
            log.info(
                "loaded %d API keys from FC_API_KEYS (simple mode; all scopes)",
                len(records),
            )
            return cls(records=records, source="env:FC_API_KEYS", disabled=False)

        # 2) FC_API_KEYS_FILE 回退
        path = (settings.api_keys_file or "").strip()
        if path:
            # 用户**配置了** JSON 文件路径 —— 加载失败必须 fail-fast。
            # 这与下方「都空 → dev 兜底」是两条完全不同的路径。
            return cls._from_file(path)

        # 3) 都空 → dev 模式
        if settings.require_configured_keys:
            raise KeyStoreError(
                "FC_API_KEYS and FC_API_KEYS_FILE are both empty but "
                "FC_REQUIRE_CONFIGURED_KEYS=true; refusing dev-fallback. "
                "Set FC_API_KEYS / FC_API_KEYS_FILE, or explicitly opt "
                "out via FC_REQUIRE_CONFIGURED_KEYS=false for local dev."
            )
        log.warning(
            "FC_API_KEYS and FC_API_KEYS_FILE are both empty; "
            "API auth is in dev-fallback mode (all endpoints public). "
            "Set FC_API_KEYS or FC_API_KEYS_FILE before production."
        )
        return cls(records=[], source="disabled", disabled=True)

    @classmethod
    def _from_file(cls, path: str) -> "KeyStore":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise KeyStoreError(
                f"FC_API_KEYS_FILE={path} unreadable: {exc}"
            ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KeyStoreError(
                f"FC_API_KEYS_FILE={path} invalid JSON: {exc}"
            ) from exc

        if not isinstance(data, list):
            raise KeyStoreError(
                f"FC_API_KEYS_FILE={path} must be a JSON array; "
                f"got {type(data).__name__}"
            )

        records: list[KeyRecord] = []
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                log.warning("keys[%d] is not an object; skipped", idx)
                continue
            key = str(item.get("key", "")).strip()
            if not key:
                log.warning("keys[%d] missing 'key' field; skipped", idx)
                continue
            label = str(item.get("label", f"file#{idx}")).strip()
            raw_scopes = item.get("scopes") or [SCOPE_EXTRACT]
            if isinstance(raw_scopes, str):
                raw_scopes = [s.strip() for s in raw_scopes.split(",") if s.strip()]
            scopes = frozenset(s for s in raw_scopes if s in ALL_SCOPES)
            if not scopes:
                scopes = frozenset({SCOPE_EXTRACT})
            try:
                rpm = int(item.get("rpm", settings.api_key_default_rpm))
            except (TypeError, ValueError) as exc:
                raise KeyStoreError(
                    f"FC_API_KEYS_FILE={path} keys[{idx}].rpm invalid: {exc}"
                ) from exc
            try:
                burst = int(item.get("burst", settings.api_key_default_burst))
            except (TypeError, ValueError) as exc:
                raise KeyStoreError(
                    f"FC_API_KEYS_FILE={path} keys[{idx}].burst invalid: {exc}"
                ) from exc
            records.append(
                KeyRecord(
                    key=key,
                    label=label,
                    scopes=scopes,
                    rpm=max(1, rpm),
                    burst=max(1, burst),
                )
            )

        if not records:
            raise KeyStoreError(
                f"FC_API_KEYS_FILE={path} yielded 0 valid keys"
            )

        log.info("loaded %d API keys from %s (structured mode)", len(records), path)
        return cls(records=records, source=f"file:{path}", disabled=False)


_KEY_STORE: Optional[KeyStore] = None


def get_key_store() -> KeyStore:
    """进程内单例；首次访问时构造。"""

    global _KEY_STORE
    if _KEY_STORE is None:
        _KEY_STORE = KeyStore.from_env()
    return _KEY_STORE


def reload_key_store() -> KeyStore:
    """Force the singleton to re-read env / JSON file (H6).

    Invoked on SIGHUP by ``fapiao_cn.server``.  Returns the freshly
    constructed ``KeyStore`` so callers can log how many keys were
    loaded.
    """

    global _KEY_STORE
    fresh = KeyStore.from_env()
    _KEY_STORE = fresh
    return fresh


def reset_key_store_for_tests() -> None:
    """测试钩子：清空单例让下一次 ``get_key_store()`` 重建。"""

    global _KEY_STORE
    _KEY_STORE = None
