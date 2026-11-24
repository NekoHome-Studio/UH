FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml ./
COPY kernel/ ./kernel/
COPY deploy/security/seccomp.json ./deploy/security/seccomp.json
ARG UH_INSTALL_EXTRAS=compose
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
# M3-8 供应链：镜像内携带 SBOM（构建阶段由 scripts/generate-sbom.mjs 产出 dist/sbom.cdx.json）
COPY scripts/generate-sbom.mjs ./scripts/generate-sbom.mjs
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
CMD ["python", "-m", "uvicorn", "kernel.main:app", "--host", "0.0.0.0", "--port", "8080"]
