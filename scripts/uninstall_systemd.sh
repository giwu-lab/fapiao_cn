#!/usr/bin/env bash
# scripts/uninstall_systemd.sh — undo the systemd registration made by
# install_portable.sh.  Does NOT remove the deployment directory.
#
# Usage:
#     sudo bash scripts/uninstall_systemd.sh              # default DEST=/opt/fapiao_cn
#     sudo bash scripts/uninstall_systemd.sh /srv/fapiao_cn  # custom path

set -euo pipefail

DEST="${1:-/opt/fapiao_cn}"
SERVICE_NAME="fapiao_cn.service"

if [ "$(id -u)" -ne 0 ]; then
    echo "错误：需要 root 权限" >&2
    exit 2
fi

if [ ! -f "/etc/systemd/system/$SERVICE_NAME" ]; then
    echo "$SERVICE_NAME 未安装，nothing to do"
    exit 0
fi

echo ">>> 停止并禁用 $SERVICE_NAME ..."
systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true

echo ">>> 删除 unit 文件 ..."
rm -f "/etc/systemd/system/$SERVICE_NAME"
systemctl daemon-reload

# Reset ownership so the deploy dir is not orphaned under a deleted user.
echo ">>> 归还 $DEST 所有权到 root ..."
chown -R root:root "$DEST" 2>/dev/null || true

echo ">>> 完成。deployment 仍在 $DEST，可手动 rm -rf 清理。"