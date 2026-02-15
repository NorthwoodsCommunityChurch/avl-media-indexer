# Security

## Intended Use

This project is designed for **local network use only** within the Northwoods church production infrastructure. It is NOT intended to be exposed to the internet.

## Network Exposure

| Port | Service | Binding | Description |
|------|---------|---------|-------------|
| 8080 | llama.cpp | 0.0.0.0 | LLM inference API (OpenAI-compatible) |
| 8081 | media-indexer | 0.0.0.0 | Search API, thumbnail serving, status |
| 8083 | tool-proxy | 0.0.0.0 | Tool-calling proxy (runs on dev Mac) |

All services bind to `0.0.0.0` (all interfaces) to allow access from other machines on the local network.

## What Is Exposed

- Search results (file paths, AI descriptions, folder tags)
- Thumbnail images (resized keyframes and image previews)
- Indexing status (file counts, folder paths, processing stats)
- System health check

## What Is NOT Exposed

- NAS credentials (stored in macOS Keychain, not in code)
- Full file contents (only metadata and thumbnails)
- Write access (all endpoints are read-only)
- No ability to modify, delete, or upload files

## Authentication

**None.** This is intentional for a trusted local network environment. Adding authentication would add complexity without meaningful security benefit on an isolated production network.

## Data Storage

- **SQLite database** (`~/media-index/index.db`): Contains file paths, AI-generated descriptions, metadata, and indexing status. Uses WAL mode for concurrent access.
- **Thumbnails** (`~/media-index/thumbnails/`): Cached JPEG thumbnails generated from source media via ffmpeg.
- **ChromaDB** (`~/media-index/chroma/`): Optional vector embeddings for semantic search. Contains text embeddings of AI descriptions.
- **Logs** (`~/media-index/indexer.log`): Processing logs with file paths and status messages.

No credentials, tokens, or secrets are stored in any of these locations.

## Subprocess Execution

The indexer calls `ffmpeg` and `ffprobe` for media processing:
- Arguments are constructed as Python lists (not shell strings), preventing shell injection
- File paths come from filesystem crawling (`os.walk`), not from HTTP request parameters
- All subprocess calls have timeouts (30-60 seconds) to prevent hangs
- Binary paths are hardcoded to `/usr/local/bin/` (static ffmpeg builds)

## Security Review Summary

| Category | Status | Notes |
|----------|--------|-------|
| Hardcoded secrets | Pass | No credentials in source code |
| SQL injection | Pass | FTS5 queries properly escaped with quote wrapping |
| Path traversal | Pass | Thumbnail IDs are SHA-256 hash prefixes, not user paths |
| Command injection | Pass | Subprocess uses list args, no user input in commands |
| Input validation | Fixed | Query limit parameter capped at 200 |
| CORS | Documented | Wildcard (`*`) - acceptable for local network |
| HTTPS | Not implemented | HTTP only - acceptable for trusted LAN |
| Rate limiting | Not implemented | Acceptable for local single-user environment |
| Authentication | Not implemented | Intentional for trusted local network |

## Recommendations

1. **Network isolation**: Keep the Mac Pro on a separate VLAN or behind a firewall if possible
2. **Do not expose to internet**: These services have no authentication and should never be port-forwarded or made publicly accessible
3. **Firewall rules**: Consider restricting ports 8080-8083 to known production machines only

## Reporting a Vulnerability

If you discover a security issue, please report it via [GitHub private vulnerability reporting](https://github.com/NorthwoodsCommunityChurch/avl-media-indexer/security/advisories/new).
