# evaTorrent ⚡

> High-performance, modern BitTorrent engine and Web UI written in Python 3.12+ with native `asyncio`, managed with `uv`.

evaTorrent is an asynchronous BitTorrent client implementing core BitTorrent specifications (BEP 0003, BEP 0015, BEP 0009), featuring an interactive dark-mode Web Dashboard with live telemetry over WebSockets.

---

## Features

- ⚡ **Pure Asynchronous Architecture**: Built on modern Python `asyncio` with no blocking networking calls.
- 📦 **Managed with `uv`**: Lightning fast installation and virtual environment management.
- 🌐 **Modern Web UI**:
  - Dark mode dashboard with glassmorphism and real-time animations.
  - Live WebSocket telemetry (download/upload speed gauges, active peers, progress).
  - Interactive **Piece Map Visualizer** displaying real-time piece completion and in-flight blocks.
  - Peer Inspector showing swarm connection states, unchoked flags, and peer transfer speeds.
  - Drag-and-drop `.torrent` upload and Magnet URI support.
  - Torrent controls: Pause, Resume, and Delete.
- 📡 **Dual Tracker Support**:
  - **HTTP/HTTPS Trackers (BEP 0003)**: standard announce protocol with compact peer decoding.
  - **UDP Trackers (BEP 0015)**: binary UDP protocol for modern public trackers.
- 🧩 **Robust Piece & Block Manager**:
  - 16 KiB block pipelining with request timeout recovery.
  - SHA-1 piece checksum verification before persisting to disk.
- 💾 **Single & Multi-File Storage**:
  - Accurate piece-to-file offset mapping.
- 💻 **CLI & Web Modes**:
  - Run the full Web UI server: `evatorrent web`
  - Download torrents directly in the terminal: `evatorrent download <torrent>`

---

## Quickstart

### 1. Requirements
- Python `>=3.12`
- [`uv`](https://github.com/astral-sh/uv) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### 2. Installation & Setup
```bash
# Clone the repository
git clone https://github.com/akash211/evaTorrent.git
cd evaTorrent

# Sync dependencies with uv
uv sync
```

### 3. Launching the Web UI
```bash
uv run evatorrent web --port 8080
```
Open your browser at **[http://127.0.0.1:8080](http://127.0.0.1:8080)**.

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

## Project Structure

```
evaTorrent/
├── pyproject.toml              # uv project metadata & dependencies
├── src/
│   └── evatorrent/
│       ├── bencoding.py        # Bencode encoder & decoder
│       ├── torrent.py          # Metainfo (.torrent) & Magnet link parser
│       ├── tracker/            # HTTP and UDP (BEP 15) tracker clients
│       ├── peer/               # Peer wire protocol (BEP 0003) & async stream reader
│       ├── storage/            # Piece/Block manager & async disk writer
│       ├── engine/             # Swarm session coordinator & multi-torrent engine
│       ├── web/                # FastAPI server, WebSockets, and Web UI static dashboard
│       └── cli.py              # CLI entry point
└── tests/                      # Pytest test suite (23 unit & integration tests)
```

## License
MIT License.
