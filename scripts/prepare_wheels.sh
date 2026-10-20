#!/usr/bin/env bash
# scripts/prepare_wheels.sh — 主机侧预下载 pip wheels 到 ./wheels/
#
# 用法：在项目根目录跑一次（重生成时再跑一次）。
#   bash scripts/prepare_wheels.sh
#
# Docker 构建会 COPY ./wheels/ 进镜像，所有依赖（构建依赖 + 运行时依赖）
# 全部离线安装，不依赖 buildx sandbox 网络。

set -euo pipefail

WHEELS_DIR="${1:-./wheels}"

# 项目运行时依赖 + 构建依赖（hatchling 是 pyproject.toml [build-system] 的 requires）
DEPS=(
    hatchling
    pdfplumber
    fastapi
    'uvicorn>=0.30'
    pydantic
    pydantic-settings
    python-multipart
    httpx
    pyyaml
    defusedxml
)

mkdir -p "$WHEELS_DIR"

# 优先用项目本地 venv 的 python；找不到再退回系统 python3
if [[ -x ./.venv/bin/python3 ]]; then
    PY=./.venv/bin/python3
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "no python3 found" >&2
    exit 1
fi

# PIP_INDEX_URL 镜像源：国内可设 https://mirrors.aliyun.com/pypi/simple/ 提速
PIP_ARGS=()
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
    PIP_ARGS+=(--index-url "$PIP_INDEX_URL")
fi

echo "==> using $PY to download wheels to $WHEELS_DIR"
"$PY" -m pip wheel \
    --no-cache-dir \
    --wheel-dir "$WHEELS_DIR" \
    "${PIP_ARGS[@]}" \
    "${DEPS[@]}"

echo "==> done. $(ls "$WHEELS_DIR"/*.whl | wc -l) wheels in $WHEELS_DIR"