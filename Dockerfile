# Stage 1: Build the high-performance Rust binary
FROM rust:bookworm AS builder

WORKDIR /usr/src/evatorrent

# Cache dependencies (Cargo.toml + lockfile first for better layer caching)
# Compile ONLY the dependency graph against throwaway stub sources.
#
# CRITICAL (stub-binary leak guard): the stub's bin/lib outputs and their cargo
# fingerprints are deleted afterwards. BuildKit's later `COPY src/` preserves
# the *older* checkout mtimes, and cargo only invalidates a unit when a source
# is NEWER than its dep-info anchor (backward mtime = "fresh"). Without this
# deletion the real `cargo build --release --bin evatorrent` below considers
# everything fresh, relinks nothing, and a 324 KB `fn main() {}` stub binary
# ships in the image (silently broke akashkece/evatorrent:1.0.0/latest on
# 2026-10-16; the container then exits in milliseconds with no logs).
COPY Cargo.toml Cargo.lock* ./
RUN mkdir -p src && echo "fn main() {}" > src/main.rs && printf "// lib\n" > src/lib.rs \
    && cargo build --release \
    && rm -rf src \
    && rm -rf target/release/evatorrent target/release/libevatorrent.rlib \
       target/release/deps/evatorrent-* target/release/deps/libevatorrent-* \
       target/release/.fingerprint/evatorrent-*

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
