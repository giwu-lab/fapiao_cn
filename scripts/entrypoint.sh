#!/usr/bin/env bash
# scripts/entrypoint.sh — Docker 入口包装，把 uvicorn stdout/stderr
# 同时写到 /app/logs/server.log 和 docker log driver。
set -e
mkdir -p /app/logs
# 对抗 ./logs:/app/logs 这种 root-owned bind mount：尽力放宽权限；
# 仍然失败时（如 fapiao_cn 既非目录 owner 也无 chmod 权限）也不让脚本崩。
chmod 777 /app/logs 2>/dev/null || true

# 用进程替换镜像日志到文件。如果 tee 因为 bind mount 只读而失败，
# 进程替换能让 uvicorn 继续向 docker log driver 输出，不会被 SIGPIPE。
exec python -m uvicorn fapiao_cn.server:app \
    --host "${FC_HOST:-0.0.0.0}" \
    --port "${FC_PORT:-8091}" \
    --log-config /app/log_config.yaml \
    > >(tee -a /app/logs/server.log 2>/dev/null) 2>&1