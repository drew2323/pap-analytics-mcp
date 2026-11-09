# One image for both services (ingest, mcp). Config, data and secrets come from mounts; see docs/OPERATIONS.md.
FROM python:3.12-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" \
    PAP_CONFIG=/etc/pap-analytics/config.toml MPLCONFIGDIR=/tmp/mpl

RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync openssh-client curl \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin pap \
 && mkdir -p /data/store /data/source && chown -R pap /data

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra plot

USER 10001
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/healthz || exit 1
ENTRYPOINT ["pap"]
CMD ["--help"]
