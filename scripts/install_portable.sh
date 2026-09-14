#!/usr/bin/env bash
# scripts/install_portable.sh — extract and launch the portable package
# on an offline target machine.
#
# Usage:
#     bash scripts/install_portable.sh [DEST_DIR]    # interactive systemd prompt
#     bash scripts/install_portable.sh --systemd     # auto-register as a service
#     bash scripts/install_portable.sh --nohup       # skip systemd, keep nohup
#
# Prerequisites:
#   - The portable tarball ``fapiao_cn-portable.tar.gz`` lives in the SAME
#     directory as this script.
#   - The host runs Linux x86_64 + Python 3.10+.  The venv inside the
#     tarball was built for the same Python version, so a mismatched
#     interpreter will break the embedded ``.so`` files.

set -euo pipefail

DEST="${1:-/opt/fapiao_cn}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="fapiao_cn-portable.tar.gz"
TEMPLATE="fapiao_cn.service.template"
SERVICE_NAME="fapiao_cn.service"

# --- 0. Resolve mode --------------------------------------------------
MODE="interactive"
DEST_ARG=""
for arg in "$@"; do
    case "$arg" in
        --systemd) MODE="systemd" ;;
        --nohup)   MODE="nohup" ;;
        --help|-h)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)         DEST_ARG="$arg" ;;
    esac
done
if [ -n "$DEST_ARG" ]; then
    DEST="$DEST_ARG"
fi

if [ ! -f "$SCRIPT_DIR/$ARCHIVE" ]; then
    echo "错误：找不到 $ARCHIVE，请将其与本脚本放在同一目录"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/$TEMPLATE" ]; then
    echo "错误：找不到 $TEMPLATE（应与 $ARCHIVE 一起被打包）"
    exit 1
fi

# --- 1. Extract -------------------------------------------------------
echo ">>> 解压到 $DEST ..."
mkdir -p "$DEST"
tar xzf "$SCRIPT_DIR/$ARCHIVE" -C "$DEST"
cd "$DEST"

# --- 2. Self-check ----------------------------------------------------
echo ">>> 自检：venv 中是否可定位 fapiao_cn 包 ..."
./parse_vevn/bin/python -c "import fapiao_cn; print('fapiao_cn ok:', fapiao_cn.__file__)" || {
    echo "错误：venv 中找不到 fapiao_cn — tarball 不完整或源机器未执行 pip install -e ."
    exit 1
}

mkdir -p logs

# --- 3. Choose launcher ----------------------------------------------
ask_mode() {
    if [ ! -t 0 ]; then
        # Non-interactive shell (no TTY): default to nohup.
        echo ">>> 非交互式终端，默认采用 nohup 模式（传入 --systemd 强制注册）"
        MODE="nohup"
        return
    fi
    echo
    echo "请选择启动方式："
    echo "  1) systemd — 注册为系统服务，开机自启 + 崩溃自动重启 + journald 日志"
    echo "  2) nohup   — 后台进程，仅本次会话存活；需要 root 写 /etc/systemd/system"
    echo
    local choice
    read -r -p "选择 [1/2，默认 1]: " choice
    case "${choice:-1}" in
        1|systemd) MODE="systemd" ;;
        2|nohup)   MODE="nohup"   ;;
        *) echo "未识别，回落到 systemd"; MODE="systemd" ;;
    esac
}

launch_nohup() {
    echo ">>> 启动 nohup 后台进程 ..."
    nohup ./parse_vevn/bin/python -m uvicorn fapiao_cn.server:app \
        --host 0.0.0.0 --port 8091 > logs/server.log 2>&1 &
    echo $! > .fapiao_cn.pid
    echo ">>> 服务已启动，PID=$(cat .fapiao_cn.pid)"
    echo ">>> 验证：curl http://localhost:8091/health"
    echo ">>> 停止：cd $DEST && kill \$(cat .fapiao_cn.pid)"
}

launch_systemd() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "错误：注册 systemd 服务需要 root 权限；改用 --nohup 或 sudo 重试" >&2
        exit 2
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "错误：目标机无 systemctl（不是 systemd 发行版？）；改用 --nohup" >&2
        exit 2
    fi

    local user="fapiao_cn"
    if ! id -u "$user" >/dev/null 2>&1; then
        echo ">>> 创建系统用户 $user ..."
        useradd --system --home "$DEST" --shell /usr/sbin/nologin "$user" || {
            echo "警告：useradd 失败，回落到当前用户 $(id -un)" >&2
            user="$(id -u -n)"
        }
    fi
    chown -R "$user":"$user" "$DEST" 2>/dev/null || true

    # Default env file (only if absent) — lets operators tweak host/port
    # without re-rendering the unit.
    if [ ! -f "$DEST/fapiao_cn.env" ]; then
        cat > "$DEST/fapiao_cn.env" <<EOF
FC_HOST=0.0.0.0
FC_PORT=8091
EOF
    fi

    echo ">>> 渲染 $SERVICE_NAME 并安装 ..."
    sed \
        -e "s|__DEST_DIR__|$DEST|g" \
        -e "s|__FC_USER__|$user|g" \
        "$SCRIPT_DIR/$TEMPLATE" > "/etc/systemd/system/$SERVICE_NAME"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"

    sleep 1
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo ">>> $SERVICE_NAME 已 active"
        echo ">>> 验证：curl http://localhost:8091/health"
        echo ">>> 状态：systemctl status $SERVICE_NAME"
        echo ">>> 日志：journalctl -u $SERVICE_NAME -f"
    else
        echo "错误：$SERVICE_NAME 启动失败，详情：journalctl -u $SERVICE_NAME -n 50" >&2
        exit 1
    fi
}

case "$MODE" in
    systemd) launch_systemd ;;
    nohup)   launch_nohup   ;;
    *)       ask_mode
             case "$MODE" in
                 systemd) launch_systemd ;;
                 *)       launch_nohup   ;;
             esac
             ;;
esac