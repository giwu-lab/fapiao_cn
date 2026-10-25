#!/usr/bin/env bash
# scripts/entrypoint.sh — Docker 入口：修正日志目录属主 → 降权 → 启动 uvicorn。
#
# 为什么入口必须是 root：
#   宿主把 ./logs 挂到 /app/logs 时，那个目录在 Linux 上通常是 root:root 0755
#   （docker compose 用 root 创建），而服务是以非 root 用户 fapiao_cn 跑的。
#   deploy/log_config.yaml 里的 audit_file 是 RotatingFileHandler，它在构造时
#   （delay 默认 False）就 open() 文件；uvicorn 在 Config.__init__ →
#   configure_logging() → dictConfig() 阶段创建 handler，此时打不开文件会抛
#   PermissionError → ValueError: Unable to configure handler 'audit_file'
#   → 容器在启动阶段直接退出，compose 表现为无限重启。
#
#   旧写法只 chmod 777 目录，而且容器以非 root 运行，对 root 拥有的目录
#   chmod 会直接 EPERM 被 || true 吞掉；就算目录 chmod 成功，已存在的
#   root:root 0644 的 audit.log 本身依然打不开。两条路都救不了。
#
#   所以入口以 root 进来：先把日志目录和里面已存在的日志文件一起 chown 给
#   应用用户，再降权回 fapiao_cn 启动 uvicorn。exec 让 uvicorn 取代 shell
#   成为 PID 1，docker stop 的 SIGTERM 语义不变。
#
# LOG_DIR 必须与 deploy/log_config.yaml 的 `filename: logs/audit.log` 和
# Dockerfile 的 `WORKDIR /app` 保持一致：uvicorn 把相对路径按进程 CWD 解析。
set -e

APP_USER="${FC_APP_USER:-fapiao_cn}"
LOG_DIR=/app/logs
HOST="${FC_HOST:-0.0.0.0}"
PORT="${FC_PORT:-8091}"

app_uid="$(id -u "$APP_USER" 2>/dev/null || true)"
app_gid="$(id -g "$APP_USER" 2>/dev/null || true)"

die_not_writable() {
    echo "[entrypoint] FATAL: '$APP_USER' cannot write '$LOG_DIR' (or its audit.log)." >&2
    echo "[entrypoint] FATAL: the server refuses to start without a usable audit log." >&2
    echo "[entrypoint] FATAL: fix it on the host, e.g." >&2
    echo "[entrypoint] FATAL:   sudo chown -R ${app_uid:-999}:${app_gid:-999} logs" >&2
    echo "[entrypoint] FATAL:   sudo chmod 755 logs" >&2
    exit 1
}

# ── 1. 修正日志目录属主（需要 root） ────────────────────────────────
if [ "$(id -u)" = "0" ]; then
    if [ -z "$app_uid" ]; then
        echo "[entrypoint] FATAL: user '$APP_USER' does not exist in this image" >&2
        exit 1
    fi
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    # 目录可写 ≠ 文件可写：已存在的 root:root 0644 的 audit.log 依然会被
    # open(..., 'a') 拒绝，所以目录和里面的文件必须一起 chown。
    if ! chown -R "$app_uid:$app_gid" "$LOG_DIR" 2>/dev/null; then
        echo "[entrypoint] WARNING: cannot chown '$LOG_DIR' to '$APP_USER'." >&2
        echo "[entrypoint] WARNING: the host mount is probably read-only or owned" >&2
        echo "[entrypoint] WARNING: by another uid; see the FATAL message below." >&2
    fi
    chmod 755 "$LOG_DIR" 2>/dev/null || true
    find "$LOG_DIR" -maxdepth 1 -type f \( -name '*.log' -o -name '*.log.*' \) \
        -exec chmod 644 {} + 2>/dev/null || true

    # 许可证文件同理：服务以非 root 运行，文件权限太紧（0600 root-only）
    # 会读不到 → 静默降级成 CE。这里放宽到应用用户可读；实在改不动
    # （只读挂载）就明确告警，别让客户以为买了企业版却没生效。
    LICENSE_FILE="${FC_LICENSE_FILE:-}"
    if [ -n "$LICENSE_FILE" ] && [ -f "$LICENSE_FILE" ]; then
        chmod 644 "$LICENSE_FILE" 2>/dev/null || true
    fi

    # 预检：以应用用户的身份试写一次。失败就直接给出可执行的修复提示，
    # 而不是让 uvicorn 在 dictConfig 阶段抛一句难懂的 PermissionError。
    if command -v setpriv >/dev/null 2>&1; then
        setpriv --reuid="$app_uid" --regid="$app_gid" --init-groups -- \
            /bin/sh -c "test -w '$LOG_DIR' && { test ! -e '$LOG_DIR/audit.log' || test -w '$LOG_DIR/audit.log'; }" \
            2>/dev/null || die_not_writable
        if [ -n "$LICENSE_FILE" ] && [ -f "$LICENSE_FILE" ]; then
            setpriv --reuid="$app_uid" --regid="$app_gid" --init-groups -- \
                /bin/sh -c "test -r '$LICENSE_FILE'" 2>/dev/null \
                || echo "[entrypoint] WARNING: license '$LICENSE_FILE' is not readable by '$APP_USER'; the service will run in CE mode." >&2
        fi
    fi
else
    # 已经被外部指定成非 root（例如 docker run --user 999）：没有 root 就
    # 修不了属主，只能如实检查当前身份能不能写。
    /bin/sh -c "test -w '$LOG_DIR' && { test ! -e '$LOG_DIR/audit.log' || test -w '$LOG_DIR/audit.log'; }" \
        || die_not_writable
fi

export FC_HOST="$HOST" FC_PORT="$PORT"

# ── 2. 降权启动 uvicorn（它就是 PID 1） ─────────────────────────────
SERVER_CMD='exec python -m uvicorn fapiao_cn.server:app --host "$FC_HOST" --port "$FC_PORT" --log-config /app/log_config.yaml > >(tee -a /app/logs/server.log 2>/dev/null) 2>&1'

if [ "$(id -u)" != "0" ]; then
    exec /bin/bash -c "$SERVER_CMD"
fi

# setpriv 来自 util-linux，python:3.12-slim 自带，不需要额外装包，
# 离线构建不受影响。
if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid="$app_uid" --regid="$app_gid" --init-groups -- \
        /bin/bash -c "$SERVER_CMD"
fi

if command -v su >/dev/null 2>&1; then
    exec su -s /bin/bash "$APP_USER" -c "$SERVER_CMD"
fi

# 绝不静默地以 root 跑服务：降权不可用就明确失败。
echo "[entrypoint] FATAL: neither setpriv nor su is available to drop privileges." >&2
echo "[entrypoint] FATAL: refusing to run the server as root." >&2
exit 1
