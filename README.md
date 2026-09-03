# evaTorrent ⚡

> High-performance, modern BitTorrent engine and Web UI written in Python 3.12+ with native `asyncio`, managed with `uv`.

evaTorrent is an asynchronous BitTorrent client implementing core BitTorrent specifications (BEP 0003, BEP 0015, BEP 0009), featuring parallel block pipelining, rarest-first piece scheduling, per-torrent download speed throttling, `.part` file preservation, and an interactive dark-mode Web Dashboard with live telemetry over WebSockets.

---

## Features

- ⚡ **Pure Asynchronous Architecture**: Built on modern Python `asyncio` with non-blocking networking.
- 🚀 **Parallel Downloading & Request Pipelining**: Pipelined block requests across connected peers with rarest-first piece selection for fast downloads.
- 🎛️ **Per-Torrent Speed Limits**: Adjust max download speed on the fly directly from the Web UI or REST API.
- 📁 **Incomplete File Safety (`.part`)**: Appends `.part` to files in progress, automatically finalizing and atomically renaming them once verified.
- 🛑 **Completion & Seeding Control**: Automatically announces completion to trackers and ceases seeding immediately once all pieces are verified.
- ⚠️ **Stall & Error Detection**: Automatically flags downloads stalled for over 3 minutes with clear error diagnostics and one-click retry.
- 📦 **Managed with `uv` & Docker Ready**: Single-command runner, official Docker Hub image, and `docker-compose.yml`.
- 🌐 **Modern Web UI**:
  - Dark mode dashboard with glassmorphism and real-time animations.
  - Live WebSocket telemetry (download/upload speed gauges, active peers, progress).
  - Interactive **Piece Map Visualizer** displaying real-time piece completion and in-flight blocks.
  - Peer Inspector showing swarm connection states, unchoked flags, and peer transfer speeds.
  - Drag-and-drop `.torrent` upload and Magnet URI support.
  - Torrent controls: Pause, Resume, Speed Limit, and Delete.
- 📡 **Dual Tracker Support**:
  - **HTTP/HTTPS Trackers (BEP 0003)**: standard announce protocol with compact peer decoding.
  - **UDP Trackers (BEP 0015)**: binary UDP protocol for modern public trackers.
- 🧩 **Robust Piece & Block Manager**:
  - 16 KiB block pipelining with request timeout recovery.
  - SHA-1 piece checksum verification before persisting to disk.
- 💾 **Single & Multi-File Storage**: Accurate piece-to-file offset mapping.
- 💻 **CLI & Web Modes**:
  - Run the full Web UI server: `evatorrent web`
  - Download torrents directly in the terminal: `evatorrent download <torrent>`

---

## Quickstart with Docker 🐳

### Using Docker Compose (Recommended)
Create or use the included `docker-compose.yml`:
```yaml
services:
  evatorrent:
    image: akashkece/evatorrent:0.2.0
    container_name: evatorrent
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./downloads:/downloads
    environment:
      - PYTHONUNBUFFERED=1
```

Run:
```bash
docker compose up -d
```
Open your browser at **[http://localhost:8080](http://localhost:8080)**.

### Using Docker CLI
```bash
docker run -d \
  --name evatorrent \
  -p 8080:8080 \
  -v $(pwd)/downloads:/downloads \
  akashkece/evatorrent:0.2.0
```

---

## Local Quickstart with `uv`

### 1. Requirements
- Python `>=3.12`
- [`uv`](https://github.com/astral-sh/uv) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### 2. Installation & Setup
```bash
git clone https://github.com/akash211/evaTorrent.git
cd evaTorrent

# Sync dependencies with uv
uv sync
```

### 3. Launching the Web UI
```bash
uv run evatorrent web --port 8080
```
Open **[http://127.0.0.1:8080](http://127.0.0.1:8080)** in your browser.

### 4. CLI Direct Download
```bash
uv run evatorrent download path/to/file.torrent --output downloads/
```

---

## Running Tests
Run the complete unit and integration test suite:
```bash
uv run pytest -v
```

---

## Building the Package
```bash
uv build
```
Build artifacts are placed in `dist/` (`.tar.gz` sdist and `.whl` wheel).

---

## License
MIT License.
