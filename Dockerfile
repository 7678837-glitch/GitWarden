# syntax=docker/dockerfile:1
FROM python:3.14-slim AS builder
RUN pip install --no-cache-dir uv==0.12.17
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version LICENSE ./
RUN uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=UTC PYTHONUNBUFFERED=1 GW_DATA_DIR=/data
ARG GIT_COMMIT=unknown
ENV GW_GIT_COMMIT=$GIT_COMMIT
RUN groupadd --system --gid 10001 gitwarden \
    && useradd --system --uid 10001 --gid 10001 --home-dir /data --shell /usr/sbin/nologin gitwarden \
    && mkdir -p /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY gitwarden/ /app/gitwarden/
COPY VERSION LICENSE /app/
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('GW_PORT', '8080')}/health\", timeout=3).read()"]
CMD ["/app/.venv/bin/python", "-m", "gitwarden"]
