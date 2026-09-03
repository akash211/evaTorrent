# evaTorrent ⚡

A modern BitTorrent client built in Python with `asyncio`, packaged with `uv`, featuring a full BitTorrent engine and a sleek Web UI.

## Features (Planned)
- Bencoding / Bdecoding parser
- Metainfo `.torrent` file handling & Magnet link support
- HTTP & UDP Tracker communication
- BitTorrent Peer Wire Protocol (Handshake, Bitfield, Interested, Choke/Unchoke, Piece/Block requests, Have)
- Piece Manager & multi-block downloader with SHA-1 hash verification
- Disk writer for single & multi-file torrents
- Modern Web UI with real-time download status, peer stats, and torrent controls
- Packaged cleanly with `uv`

## License
MIT
