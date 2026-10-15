# Stage 1: Build the high-performance Rust binary
FROM rust:bookworm AS builder

WORKDIR /usr/src/evatorrent

# Cache dependencies (Cargo.toml + lockfile first for better layer caching)
COPY Cargo.toml Cargo.lock* ./
RUN mkdir -p src && echo "fn main() {}" > src/main.rs && printf "// lib\n" > src/lib.rs \
    && cargo build --release \
    && rm -rf src

# Copy actual source code and build release binary
COPY src/ ./src/
COPY static/ ./static/
COPY tests/ ./tests/
RUN cargo build --release --bin evatorrent

# Stage 2: Minimal runtime image (~25 MB overhead vs ~350 MB Python image)
FROM debian:bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled Rust binary and static Web UI assets
COPY --from=builder /usr/src/evatorrent/target/release/evatorrent /usr/local/bin/evatorrent
COPY static/ ./static/

# Create directories and non-root user
RUN mkdir -p /downloads /data \
    && useradd -m -s /bin/bash evatorrent \
    && chown -R evatorrent:evatorrent /app /downloads /data

ENV DOWNLOAD_DIR=/downloads
ENV EVA_DATA_DIR=/data
ENV RUST_LOG=info

USER evatorrent

EXPOSE 8080
EXPOSE 6881/tcp
EXPOSE 6881/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/auth/status || exit 1

ENTRYPOINT ["evatorrent", "web", "--host", "0.0.0.0", "--port", "8080"]
