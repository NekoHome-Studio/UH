FROM python:3.12-slim-bookworm AS build
WORKDIR /build
COPY pyproject.toml ./
RUN python -c "import tomllib; from pathlib import Path; print(*tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']['optional-dependencies']['crewai'], sep=chr(10))" > requirements.txt \
    && python -m pip install --only-binary=:all: --no-cache-dir --no-compile --prefix=/install -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
COPY --from=build /install/ /usr/local/
COPY kernel/crewai_worker.py ./
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    CREWAI_STORAGE_DIR=/tmp/crewai \
    CREWAI_DISABLE_TELEMETRY=true \
    CREWAI_TRACING_ENABLED=false \
    OTEL_SDK_DISABLED=true
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "crewai_worker:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "5"]
