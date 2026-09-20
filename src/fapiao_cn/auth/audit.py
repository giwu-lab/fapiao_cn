"""审计日志 —— 每条受保护请求一行 JSON。

通过专用 logger ``fapiao_cn.audit`` 输出，handler 在 ``deploy/log_config.yaml``
中路由到 ``logs/audit.log``。这样调用方可以单独轮转 / 入库 / 告警审计日志。
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("fapiao_cn.audit")


def audit_log(
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    client: str,
    key_id: Optional[str],
    label: Optional[str],
    bytes_in: int = 0,
    bytes_out: int = 0,
    reason: Optional[str] = None,
) -> None:
    """记录一条审计事件。

    失败（401/403/429）也记：``reason`` 字段说明原因，便于事后回溯。
    成功请求 ``key_id``/``label`` 非空；未鉴权透传时为空字符串 + reason="legacy"。
    """

    log.info(
        "%s %s -> %d (%dms) key=%s label=%s from=%s in=%dB out=%dB%s",
        method,
        path,
        status,
        duration_ms,
        key_id or "-",
        label or "-",
        client,
        bytes_in,
        bytes_out,
        f" reason={reason}" if reason else "",
    )
