#!/usr/bin/env bash
# scripts/install_portable.sh — extract and launch the portable package
# on an offline target machine.
#
# Usage:
#     bash scripts/install_portable.sh [DEST_DIR]    # interactive prompt
#     bash scripts/install_portable.sh --systemd     # auto-register as a systemd service
#     bash scripts/install_portable.sh --supervisor  # register under Supervisor
#     bash scripts/install_portable.sh --nohup       # skip daemon, keep nohup
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
SUPERVISOR_TEMPLATE="fapiao_cn.supervisor.conf"
SERVICE_NAME="fapiao_cn.service"
SUPERVISOR_CONF_NAME="fapiao_cn.conf"
SUPERVISOR_CONF_DIR="/etc/supervisor/conf.d"

# --- 0. Resolve mode --------------------------------------------------
MODE="interactive"
DEST_ARG=""
for arg in "$@"; do
    case "$arg" in
        --systemd)   MODE="systemd"   ;;
        --nohup)     MODE="nohup"     ;;
        --supervisor) MODE="supervisor" ;;
        --help|-h)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)           DEST_ARG="$arg"   ;;
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

if [ ! -f "$SCRIPT_DIR/$SUPERVISOR_TEMPLATE" ]; then
    echo "错误：找不到 $SUPERVISOR_TEMPLATE（应与 $ARCHIVE 一起被打包）"
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
        echo ">>> 非交互式终端，默认采用 nohup 模式（传入 --systemd / --supervisor 强制注册）"
        MODE="nohup"
        return
    fi
    echo
    echo "请选择启动方式："
    echo "  1) systemd    — 注册为系统服务，开机自启 + 崩溃自动重启 + journald 日志"
    echo "  2) nohup      — 后台进程，仅本次会话存活；需要 root 写 /etc/systemd/system"
    echo "  3) supervisor — Supervisor 守护（无 systemd 环境；写 /etc/supervisor/conf.d/）"
    echo
    local choice
    read -r -p "选择 [1/2/3，默认 1]: " choice
    case "${choice:-1}" in
        1|systemd)    MODE="systemd"    ;;
        2|nohup)      MODE="nohup"      ;;
        3|supervisor) MODE="supervisor" ;;
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
        echo "错误：目标机无 systemctl（不是 systemd 发行版？）；改用 --nohup 或 --supervisor" >&2
        exit 2
    fi

    local user
    user="$(ensure_user)"

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

launch_supervisor() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "错误：注册 Supervisor program 需要 root 权限（写 $SUPERVISOR_CONF_DIR）；改用 --nohup 或 sudo 重试" >&2
        exit 2
    fi
    if ! command -v supervisorctl >/dev/null 2>&1; then
        echo "错误：目标机无 supervisorctl；安装：apt install supervisor / yum install supervisor / pip install supervisor" >&2
        exit 2
    fi
    if [ ! -d "$SUPERVISOR_CONF_DIR" ]; then
        echo "错误：$SUPERVISOR_CONF_DIR 不存在；确认 /etc/supervisor/supervisord.conf 中 [include] files = $SUPERVISOR_CONF_DIR/*.conf" >&2
        exit 2
    fi

    local user
    user="$(ensure_user)"

    echo ">>> 渲染 $SUPERVISOR_CONF_NAME 并安装到 $SUPERVISOR_CONF_DIR ..."
    sed \
        -e "s|__DEST_DIR__|$DEST|g" \
        -e "s|__FC_USER__|$user|g" \
        "$SCRIPT_DIR/$SUPERVISOR_TEMPLATE" > "$SUPERVISOR_CONF_DIR/$SUPERVISOR_CONF_NAME"

    supervisorctl reread
    supervisorctl update

    supervisorctl restart fapiao_cn 2>/dev/null || supervisorctl start fapiao_cn

    sleep 1
    if supervisorctl status fapiao_cn | grep -q "RUNNING"; then
        echo ">>> fapiao_cn 已 RUNNING"
        echo ">>> 验证：curl http://localhost:8091/health"
        echo ">>> 状态：supervisorctl status fapiao_cn"
        echo ">>> 日志：supervisorctl tail -f fapiao_cn"
        echo ">>> stderr：supervisorctl tail -f fapiao_cn stderr"
    else
        echo "错误：fapiao_cn 启动失败，详情：supervisorctl tail fapiao_cn stderr" >&2
        exit 1
    fi
}

# ensure_user — create the fapiao_cn system user (idempotent) and chown the
# deployment dir.  Falls back to the current user on failure (e.g. busybox).
ensure_user() {
    local user="fapiao_cn"
    if ! id -u "$user" >/dev/null 2>&1; then
        echo ">>> 创建系统用户 $user ..."
        useradd --system --home "$DEST" --shell /usr/sbin/nologin "$user" || {
            echo "警告：useradd 失败，回落到当前用户 $(id -un)" >&2
            user="$(id -u -n)"
        }
    fi
    chown -R "$user":"$user" "$DEST" 2>/dev/null || true
    echo "$user"
}

case "$MODE" in
    systemd)    launch_systemd    ;;
    supervisor) launch_supervisor ;;
    nohup)      launch_nohup      ;;
    *)          ask_mode
                case "$MODE" in
                    systemd)    launch_systemd    ;;
                    supervisor) launch_supervisor ;;
                    *)          launch_nohup      ;;
                esac
                ;;
esac