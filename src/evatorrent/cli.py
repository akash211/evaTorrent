"""evaTorrent Command Line Interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("evaTorrent")


def cmd_web(args):
    """Starts the evaTorrent Web UI server."""
    logger.info(f"Starting evaTorrent Web UI on http://{args.host}:{args.port}")
    uvicorn.run("evatorrent.web.app:app", host=args.host, port=args.port, reload=args.reload)


async def run_download(torrent_path: str, output_dir: str):
    from evatorrent.engine.session import TorrentSession
    from evatorrent.torrent import Torrent

    path = Path(torrent_path)
    if not path.exists():
        logger.error(f"File not found: {torrent_path}")
        sys.exit(1)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading torrent from {torrent_path}...")
    torrent = Torrent.from_file(path)
    logger.info(f"Torrent Name: {torrent.name}")
    logger.info(f"Total Size: {torrent.total_length / 1024 / 1024:.2f} MB")
    logger.info(f"Pieces: {torrent.piece_count} ({torrent.piece_length} bytes each)")
    logger.info(f"Trackers: {', '.join(torrent.trackers)}")

    session = TorrentSession(torrent, download_dir=out)
    session.start()

    logger.info("Session started. Downloading pieces from swarm...")
    try:
        while not session.piece_manager.is_complete:
            await asyncio.sleep(1.0)
            pct = session.piece_manager.progress_percentage
            dl_mb = session.download_speed / 1024 / 1024
            active = sum(1 for c in session.active_peers.values() if c.is_connected)
            sys.stdout.write(
                f"\rProgress: {pct:6.2f}% | Speed: {dl_mb:5.2f} MB/s | Peers: {active}/{len(session.seen_peers)} | ETA: {session.eta_seconds or 0}s  "
            )
            sys.stdout.flush()

        sys.stdout.write("\n")
        logger.info("Download completed successfully!")
    finally:
        await session.stop()


def cmd_download(args):
    """Downloads a torrent directly from the CLI."""
    asyncio.run(run_download(args.torrent, args.output))


def main():
    parser = argparse.ArgumentParser(
        prog="evatorrent",
        description="evaTorrent ⚡ - High-performance modern BitTorrent engine and Web UI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Web UI command
    web_parser = subparsers.add_parser("web", help="Start the evaTorrent Web UI server")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    web_parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    web_parser.add_argument("--reload", action="store_true", help="Enable live auto-reload for dev")

    # CLI download command
    dl_parser = subparsers.add_parser("download", help="Download a .torrent directly in CLI")
    dl_parser.add_argument("torrent", help="Path to .torrent file")
    dl_parser.add_argument("-o", "--output", default="downloads", help="Output directory (default: downloads)")

    args = parser.parse_args()
    if args.command == "web":
        cmd_web(args)
    elif args.command == "download":
        cmd_download(args)
    else:
        # Default to starting the web UI
        parser.print_help()


if __name__ == "__main__":
    main()
