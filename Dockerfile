# syntax=docker/dockerfile:1.7
# ── Stage 1: builder ────────────────────────────────────────────────
# 依赖有两条来路，产出同样的镜像：
#   离线 —— 主机侧 `bash scripts/prepare_wheels.sh` 把 wheel 预置到 ./wheels/，
#           构建期 `pip --no-index --find-links=/wheels` 全程不联网，绕开
#           BuildKit sandbox 在受限网络里的 DNS 隔离问题；
#   在线 —— 没有预置 wheel 时，构建期从镜像源下载（CI / 全新克隆走这条，
#           不需要任何前置步骤）。
# 是否离线由 /wheels 里有没有 .whl 决定；wheels/.gitkeep 是受版本控制的
# 占位文件，保证全新克隆里 `COPY wheels /wheels` 也成立。
FROM python:3.12-slim AS builder

WORKDIR /build

COPY wheels /wheels

# 发行版选择：ce = 开源版，ee = 企业版。
# ce 构建时把企业版 wheel 从 /wheels 里剔掉 —— runtime 阶段从 /wheels
# 安装依赖，剔掉之后 CE 镜像里连 fapiao_cn_ee 的字节码都没有，
# 商业边界落在**发行包**上，而不是运行时的 if。
ARG EDITION=ce
RUN if [ "$EDITION" != "ee" ]; then rm -f /wheels/fapiao_cn_ee-*.whl; fi

# 可选：切换 pip 镜像源（默认阿里云）。传 --build-arg PIP_INDEX_URL= 走官方源。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 一次完成：判定离线/在线、装好构建依赖（hatchling）、并把运行时依赖 wheel
# 备齐到 /wheels。
#
# 模式必须在这里判定一次并记进 /etc/pip-mode —— 后续步骤不能重新判断
# 「/wheels 里有没有 .whl」：在线分支会把依赖下载进 /wheels，之后这个条件
# 就不再代表原始模式了。
RUN set -eu; \
    if ls /wheels/*.whl >/dev/null 2>&1; then \
        echo offline > /etc/pip-mode; \
        PIP_ARGS="--no-index --find-links=/wheels"; \
        echo ">>> offline mode: using prebuilt wheels in /wheels"; \
    else \
        echo online > /etc/pip-mode; \
        PIP_ARGS="--index-url ${PIP_INDEX_URL}"; \
        echo ">>> online mode: downloading runtime deps from ${PIP_INDEX_URL}"; \
    fi; \
    pip install --no-cache-dir ${PIP_ARGS} hatchling; \
    if [ "$(cat /etc/pip-mode)" = online ]; then \
        pip download --no-cache-dir ${PIP_ARGS} --dest /wheels \
            pdfplumber fastapi 'uvicorn>=0.30' pydantic pydantic-settings \
            python-multipart httpx pyyaml defusedxml; \
    fi

# H45: split dependency layer from source layer so changes to src/
# don't invalidate the (large) pip download cache.
COPY pyproject.toml README.md ./
RUN set -eu; \
    if [ "$(cat /etc/pip-mode)" = offline ]; then \
        pip wheel --no-cache-dir --no-deps --no-index --find-links=/wheels --wheel-dir=/wheels .; \
    else \
        pip wheel --no-cache-dir --no-deps --index-url "${PIP_INDEX_URL}" --wheel-dir=/wheels .; \
    fi

# Now copy src/ separately and rebuild the project wheel.
COPY src ./src
RUN set -eu; \
    if [ "$(cat /etc/pip-mode)" = offline ]; then \
        pip wheel --no-cache-dir --no-deps --no-index --find-links=/wheels --wheel-dir=/wheels .; \
    else \
        pip wheel --no-cache-dir --no-deps --index-url "${PIP_INDEX_URL}" --wheel-dir=/wheels .; \
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
# /wheels 里的依赖在离线模式来自主机预置、在在线模式来自 builder 的
# pip download —— 两条路到这里都已经是 wheel，所以这里恒为离线安装。
#
# EDITION=ee 时额外安装企业版发行包 fapiao_cn_ee（报关单解析 / 批量提取）。
# 注意：企业版**光装了包还不够** —— 运行时还要用 FC_LICENSE_FILE 挂进
# 一份有效许可证，否则服务自动降级成开源版行为（企业版端点返回 403，
# 其余功能照常）。
ARG EDITION=ce
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
        pdfplumber fastapi 'uvicorn>=0.30' pydantic pydantic-settings python-multipart httpx pyyaml defusedxml \
    && pip install --no-cache-dir --no-index --no-deps --find-links=/wheels fapiao_cn \
    && if [ "$EDITION" = "ee" ]; then \
         pip install --no-cache-dir --no-index --no-deps --find-links=/wheels fapiao_cn_ee; \
       fi \
    && rm -rf /wheels

ENV FC_HOST=0.0.0.0 \
    FC_PORT=8091 \
    FC_APP_ROOT=/app \
    FC_APP_USER=fapiao_cn \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 入口需要 root：宿主 bind mount 过来的 /app/logs 在 Linux 上通常是
# root:root 0755，必须先把日志目录和已存在的日志文件一起 chown 给应用
# 用户，否则 audit 日志 handler 在 uvicorn 启动阶段就打不开文件
# （PermissionError → ValueError: Unable to configure handler 'audit_file'），
# 容器直接退出。scripts/entrypoint.sh 随后用 setpriv 降权回 fapiao_cn 再
# 启动 uvicorn；降权手段不可用时它会明确失败，而不是以 root 跑服务。
USER root
EXPOSE 8091

COPY --chown=fapiao_cn:fapiao_cn deploy/log_config.yaml /app/log_config.yaml
COPY --chown=fapiao_cn:fapiao_cn scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8091/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
