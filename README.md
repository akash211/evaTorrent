# evaTorrent ⚡

> High-performance, modern BitTorrent engine and Web UI written in Python 3.12+ with native `asyncio`, managed with `uv`.

evaTorrent is an asynchronous BitTorrent client implementing core BitTorrent specifications (BEP 0003, BEP 0015, BEP 0009), featuring Email OTP & Google OAuth authentication, parallel block pipelining, per-torrent download speed throttling, `.part` file preservation, and an interactive dark-mode Web Dashboard with live telemetry over WebSockets.

---

## Features

- 🔐 **Authentication & Security (New in v0.3.0)**:
  - **Email OTP Sign-In**: Passwordless login with 6-digit cryptographic verification codes sent via SMTP or printed to server/docker logs.
  - **Google OAuth Login**: Direct one-click login via Google Identity Services (GIS) for authorized administrators.
  - **Initial Setup Wizard**: Easily configure your administrator email on first launch or directly via environment variables.
  - **Session & Telemetry Protection**: All REST endpoints and WebSockets are guarded by HMAC-signed session cookies/tokens.
- ⚡ **Pure Asynchronous Architecture**: Built on modern Python `asyncio` with non-blocking networking.
- 🚀 **Parallel Downloading & Request Pipelining**: Pipelined block requests across connected peers with rarest-first piece selection for fast downloads.
- 🎛️ **Per-Torrent Speed Limits**: Adjust max download speed on the fly directly from the Web UI or REST API.
- 📁 **Incomplete File Safety (`.part`)**: Appends `.part` to files in progress, automatically finalizing and atomically renaming them once verified.
- 🛑 **Completion & Seeding Control**: Automatically announces completion to trackers and ceases seeding immediately once all pieces are verified.
- ⚠️ **Stall & Error Detection**: Automatically flags stalled downloads with clear diagnostics and one-click resume.
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

---

## Quickstart with Docker 🐳

### Using Docker Compose (Recommended)
Create or use the included `docker-compose.yml`:
```yaml
services:
  evatorrent:
    image: akashkece/evatorrent:0.3.0
    container_name: evatorrent
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./downloads:/downloads
      - ./data:/root/.evatorrent
    environment:
      - PYTHONUNBUFFERED=1
      # Initial Admin Email (or set via Web UI on first launch)
      - ADMIN_EMAIL=your-email@gmail.com
      # Optional Google OAuth 2.0 Client ID for Google Sign-In
      - GOOGLE_CLIENT_ID=
      # Optional Outbound SMTP for Email OTP delivery (if empty, OTP logs to docker console)
      - SMTP_HOST=
      - SMTP_PORT=587
      - SMTP_USER=
      - SMTP_PASSWORD=
      - SMTP_FROM=
```

Run:
```bash
docker compose up -d
```
Open your browser at **[http://localhost:8080](http://localhost:8080)**.
If SMTP is not configured, view your login OTP anytime via:
```bash
docker compose logs -f evatorrent
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
