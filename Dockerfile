# syntax=docker/dockerfile:1.7
# ── Stage 1: builder ────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# H45: split dependency layer from source layer so changes to src/
# don't invalidate the (large) pip download cache.
COPY pyproject.toml README.md ./
RUN pip wheel --no-cache-dir --no-deps --wheel-dir=/wheels .

# Now copy src/ separately and rebuild the project wheel.
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir=/wheels .

# 预先下载所有运行时依赖 wheel（国内 PyPI 镜像；传 --build-arg PIP_INDEX_URL= 关闭）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip download --no-cache-dir \
        --index-url "$PIP_INDEX_URL" \
        --dest /wheels \
        pdfplumber fastapi 'uvicorn>=0.30' pydantic pydantic-settings python-multipart httpx pyyaml defusedxml; \
    else \
      pip download --no-cache-dir \
        --dest /wheels \
        pdfplumber fastapi 'uvicorn>=0.30' pydantic pydantic-settings python-multipart httpx pyyaml defusedxml; \
    fi

# ── Stage 2: runtime ────────────────────────────────────────────────
FROM python:3.12-slim

# 可选：切换国内 apt 源（默认阿里云）。传 --build-arg APT_MIRROR= 关闭。
ARG APT_MIRROR=mirrors.aliyun.com
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i \
        -e "s|deb.debian.org/debian|${APT_MIRROR}/debian|g" \
        -e "s|security.debian.org/debian-security|${APT_MIRROR}/debian-security|g" \
        /etc/apt/sources.list.d/debian.sources; \
    fi

# curl 用于 HEALTHCHECK；ca-certificates 用于 HTTPS 调用（国税代理等）；
# tzdata 提供 Asia/Shanghai 时区数据库
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# 非 root 系统用户
RUN groupadd --system fapiao_cn \
 && useradd  --system --gid fapiao_cn --home /app --shell /usr/sbin/nologin fapiao_cn \
 && mkdir -p /app/logs \
 && chown -R fapiao_cn:fapiao_cn /app

WORKDIR /app

# 拷贝源码，由 wheel 安装
COPY --from=builder /wheels /wheels

# 装运行时依赖 + 项目本体（本地 wheel，不联网）
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
        pdfplumber fastapi 'uvicorn>=0.30' pydantic pydantic-settings python-multipart httpx pyyaml defusedxml \
 && pip install --no-cache-dir --no-index --no-deps --find-links=/wheels fapiao_cn \
 && rm -rf /wheels

ENV FC_HOST=0.0.0.0 \
    FC_PORT=8091 \
    FC_APP_ROOT=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

USER fapiao_cn
EXPOSE 8091

COPY --chown=fapiao_cn:fapiao_cn deploy/log_config.yaml /app/log_config.yaml
COPY --chown=fapiao_cn:fapiao_cn scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8091/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]