# One image, two services. `docker compose` runs it twice with a different
# --strategy, so CARRY and TREND share the build but never share a database
# (PRD Section 6: one process per strategy, each with its own SQLite file).
# The dashboard is built here and copied into the runtime image. Without this
# stage the React app exists only under `vite dev`: the container runs uvicorn
# alone, so every US-T18 page would 404 on the deployed host while the API
# answered perfectly.
FROM node:22-slim AS dashboard
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build


FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Litestream streams the SQLite WAL to the free object store. It is optional:
# the engine runs without it and reports the backup as unavailable.
ARG LITESTREAM_VERSION=0.3.13
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) ls_arch=amd64 ;; arm64) ls_arch=arm64 ;; *) ls_arch="" ;; esac; \
    if [ -n "$ls_arch" ]; then \
      curl -fsSL -o /tmp/ls.tgz \
        "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-${ls_arch}.tar.gz" \
      && tar -C /usr/local/bin -xzf /tmp/ls.tgz litestream \
      && rm /tmp/ls.tgz; \
    fi

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config
COPY --from=dashboard /ui/dist ./ui
COPY ops/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && useradd --create-home --uid 10001 aegis \
 && mkdir -p /app/data /app/backups \
 && chown -R aegis:aegis /app
USER aegis

VOLUME ["/app/data", "/app/backups"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -m engine --strategy "${AEGIS_STRATEGY:-trend}" --health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--strategy", "trend", "--mode", "paper"]
