# M3 独立复审 P1：LABEL 声明了 /app/dist/sbom.cdx.json，但原构建只 COPY 生成脚本
# 从未运行它——按该 Dockerfile 构建的镜像不含 SBOM，M3-8 镜像验收条件不成立。
# 用独立 node 阶段生成清单，再 COPY 进运行镜像。
FROM node:22-slim AS sbom
WORKDIR /src
COPY pyproject.toml ./
COPY scripts/generate-sbom.mjs ./scripts/
RUN node scripts/generate-sbom.mjs && test -s dist/sbom.cdx.json
FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml ./
COPY kernel/ ./kernel/
COPY deploy/security/seccomp.json ./deploy/security/seccomp.json
# M3 独立复审 P1：默认含 otel——K8s 清单默认开启 UH_OTEL_ENABLED，
# 只装 compose extra 会让内核导入失败并静默降级为无追踪，链路在发送端就断了。
ARG UH_INSTALL_EXTRAS=compose,otel
RUN python -m pip install --no-cache-dir --prefix=/install ".[${UH_INSTALL_EXTRAS}]"

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/local/bin/python3 /usr/bin/python3 \
    && groupadd --gid 10001 uh \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin uh \
    && install -d -o uh -g uh /app/.uh /app/.uh/plugins

WORKDIR /app
COPY --from=build /install/ /usr/local/
COPY --from=build /src/kernel/ ./kernel/
COPY ui/ ./ui/
COPY docs/contracts/ ./docs/contracts/
# M3-8 供应链：SBOM 产物随镜像落地（由上方 sbom 阶段生成），
# 与下方 LABEL org.opencontainers.image.sbom 声明的路径一致。
COPY --from=sbom /src/dist/sbom.cdx.json ./dist/sbom.cdx.json
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UH_PLUGIN_INSTALL_VIA_KERNEL=1

# SBOM 作为镜像元数据的一部分（OCI annotation），不含凭据
LABEL org.opencontainers.image.title="UH! Kernel" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.sbom="/app/dist/sbom.cdx.json"
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/health', timeout=3).close()"]
# M3 独立复审 P1：--no-access-log 防止查询串（可能是凭据）落入访问日志。
# 结构化审计由 kernel.logging 输出，无需 Uvicorn 内置 access log。
CMD ["python", "-m", "uvicorn", "kernel.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
