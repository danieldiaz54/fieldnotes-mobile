# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY pyproject.toml requirements.txt ./
COPY src/ ./src/
COPY ui/ ./ui/
COPY migrations/ ./migrations/

RUN python -m compileall -q src

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FIELDNOTES_ENV=production \
    FIELDNOTES_HOST=0.0.0.0 \
    FIELDNOTES_PORT=8097 \
    LOCAL_SQLITE_PATH=/app/.data/fieldnotes_device.sqlite3 \
    REMOTE_SQLITE_PATH=/app/.data/fieldnotes_remote.sqlite3

RUN groupadd --system fieldnotes && \
    useradd --system --gid fieldnotes --create-home --home-dir /app fieldnotes && \
    mkdir -p /app/.data && \
    chown -R fieldnotes:fieldnotes /app

WORKDIR /app
COPY --from=builder --chown=fieldnotes:fieldnotes /build/pyproject.toml /build/requirements.txt ./
COPY --from=builder --chown=fieldnotes:fieldnotes /build/src ./src
COPY --from=builder --chown=fieldnotes:fieldnotes /build/ui ./ui
COPY --from=builder --chown=fieldnotes:fieldnotes /build/migrations ./migrations

USER fieldnotes

EXPOSE 8097

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/api/v1/health', timeout=3)" || exit 1

CMD ["python", "-m", "src.main"]
