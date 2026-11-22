FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml ./
COPY kernel/ ./kernel/
RUN python -m pip install --no-cache-dir --prefix=/install '.[compose]'

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
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
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UH_PLUGIN_INSTALL_VIA_KERNEL=1

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/health', timeout=3).close()"]
CMD ["python", "-m", "uvicorn", "kernel.main:app", "--host", "0.0.0.0", "--port", "8080"]
