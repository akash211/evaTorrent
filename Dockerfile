FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install dependencies first for optimal caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source code and assets
COPY README.md ./
COPY src/ ./src/

# Install application
RUN uv sync --frozen --no-dev

# Create downloads volume
RUN mkdir -p /downloads
ENV DOWNLOAD_DIR=/downloads

EXPOSE 8080

ENTRYPOINT ["uv", "run", "evatorrent", "web", "--host", "0.0.0.0", "--port", "8080"]
