FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies first for optimal caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source code and assets
COPY README.md ./
COPY src/ ./src/

# Install application
RUN uv sync --frozen --no-dev

# Create directories and non-root user
RUN mkdir -p /downloads /data \
    && useradd -m -s /bin/bash evatorrent \
    && chown -R evatorrent:evatorrent /app /downloads /data

ENV DOWNLOAD_DIR=/downloads
ENV EVA_DATA_DIR=/data

USER evatorrent

EXPOSE 8080
EXPOSE 6881/tcp
EXPOSE 6881/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/auth/status || exit 1

ENTRYPOINT ["uv", "run", "evatorrent", "web", "--host", "0.0.0.0", "--port", "8080"]
