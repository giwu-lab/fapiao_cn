# syntax=docker/dockerfile:1.7
# ── Stage 1: builder ────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# 一次性构建本项目 wheel
COPY pyproject.toml README.md ./
COPY src ./src

# 用 build-system 要求的 hatchling 自举（PEP 517），把产物放到 /wheels
RUN pip wheel --no-cache-dir --no-deps --wheel-dir=/wheels .

# ── Stage 2: runtime ────────────────────────────────────────────────
FROM python:3.12-slim

# curl 用于 HEALTHCHECK；ca-certificates 用于 HTTPS 调用（国税代理等）
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 非 root 系统用户
RUN groupadd --system fapiao_cn \
 && useradd  --system --gid fapiao_cn --home /app --shell /usr/sbin/nologin fapiao_cn \
 && mkdir -p /app/logs \
 && chown -R fapiao_cn:fapiao_cn /app

WORKDIR /app

# 拷贝源码，由 wheel 安装
COPY --from=builder /wheels /wheels
COPY --chown=fapiao_cn:fapiao_cn pyproject.toml README.md ./
COPY --chown=fapiao_cn:fapiao_cn src ./src

# 装运行时依赖 + 项目本体（本地 wheel，不联网）
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
        pdfplumber fastapi 'uvicorn>=0.30' pydantic pydantic-settings python-multipart httpx \
 && pip install --no-cache-dir --no-index --no-deps --find-links=/wheels fapiao_cn \
 && rm -rf /wheels

ENV FC_HOST=0.0.0.0 \
    FC_PORT=8091 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER fapiao_cn
EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8091/health || exit 1

ENTRYPOINT ["python", "-m", "uvicorn", "fapiao_cn.server:app", \
            "--host", "0.0.0.0", "--port", "8091"]