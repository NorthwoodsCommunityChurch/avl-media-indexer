# LLM Server - Product Requirements Document

## Overview

Local LLM inference server and AI-powered media indexer running on a Mac Pro (Intel Xeon W-3245, 96GB RAM, 3x AMD GPUs with 24GB total VRAM) at Northwoods Community Church. The system indexes a 173TB NAS vault of production media (video, images, audio) using AI vision descriptions. It provides a full-text search API, thumbnail serving, and developer tool integrations via MCP and an OpenAI-compatible inference endpoint.

## Architecture

```
NAS (10.10.11.185)          Mac Pro (10.10.11.173)              Dev Mac
  173TB Vault  ──SMB──>  ┌─────────────────────────┐
                         │  llama.cpp (port 8080)   │
                         │  Vulkan multi-GPU        │
                         │                          │
                         │  media-indexer.py         │
                         │  - SQLite + FTS5         │<──HTTP──> Vault Search App
                         │  - ChromaDB (optional)   │
                         │  - Search API (8081)     │
                         │  - Thumbnail serving     │
                         └─────────────────────────┘
                                                        ┌──────────────────┐
                                                        │ tool-proxy.py    │
                                                        │ (port 8083)      │
                                                        │                  │
                                                        │ media-search-mcp │
                                                        │ (stdio MCP)      │
                                                        └──────────────────┘
```

## Components

| Component | File | Runs On | Port | Purpose |
|-----------|------|---------|------|---------|
| LLM Server | llama.cpp | Mac Pro | 8080 | OpenAI-compatible inference API |
| Media Indexer | media-indexer.py | Mac Pro | 8081 | Crawl, describe, search, serve thumbnails |
| Tool Proxy | tool-proxy.py | Dev Mac | 8083 | Translate OpenAI tool-calling for Gemma |
| MCP Server | media-search-mcp.py | Dev Mac | stdio | VS Code / Continue integration |
| Server Scripts | start/stop/status-llm-server.sh | Mac Pro | - | Server lifecycle management |

## Search API Endpoints (Port 8081)

| Method | Path | Parameters | Response |
|--------|------|------------|----------|
| GET | /search | q (required), limit (optional, max 200) | JSON: query, count, results[] |
| GET | /thumbnail | id (required) | JPEG image bytes |
| GET | /status | - | JSON: counts by status, folders list |
| GET | /health | - | JSON: {status: "ok"} |
| GET | /folders | - | JSON: indexed folders list |

## Indexing Pipeline

1. **Crawl**: Walk vault folders via `os.walk`, filter by media extensions
2. **Register**: Insert file records into SQLite (path, size, mtime hash as ID)
3. **Probe**: Extract metadata via ffprobe (duration, resolution, codec)
4. **Keyframes**: Extract 1-3 keyframes per video via ffmpeg (10%, 50%, 90% timestamps)
5. **Describe**: Send images/keyframes to Gemma 3 12B vision for AI descriptions
6. **Store**: Save descriptions in SQLite + FTS5 index, optionally embed in ChromaDB
7. **Serve**: HTTP API provides search, thumbnails, and status

## Data Model (SQLite)

**folders**: id, path, name, enabled, last_scan, file_count

**files**: id, path, filename, folder_id, file_type, size_bytes, modified_at, duration_seconds, width, height, codec, ai_description, tags, indexed_at, status, error_message

**keyframes**: id, file_id, timestamp_seconds, thumbnail_path, ai_description

**files_fts**: FTS5 virtual table on filename, ai_description, tags

## Supported Media Formats

- **Images**: .jpg, .jpeg, .png, .tif, .tiff, .bmp, .webp
- **Video**: .mp4, .mov, .mxf, .m4v, .avi, .mkv, .r3d, .braw
- **Audio**: .wav, .mp3, .aac, .m4a, .flac, .aif, .aiff

## CLI Commands

```
media-indexer.py index <folder>     Index folders (crawl + describe)
media-indexer.py search <query>     Search from command line
media-indexer.py status             Show indexing progress
media-indexer.py watch <folder>     Continuous monitoring (5-min rescan)
media-indexer.py serve [port]       HTTP search API (default: 8081)
media-indexer.py reembed            Re-embed all descriptions into ChromaDB
```

## Models

| Model | File Size | Speed | Vision | Notes |
|-------|-----------|-------|--------|-------|
| Qwen3-14B Q4_K_M | 8.4 GB | ~14 t/s | No | Default, text-only, recommended for general use |
| Gemma 3 12B Q4_K_M | 6.8 GB + 815 MB encoder | ~13 t/s | Yes | Used by media indexer for vision descriptions |
| Qwen3-VL-32B Q4_K_M | 19.8 GB | ~7 t/s | Yes | Large vision model, tight VRAM fit |

## Startup Sequence

1. Mount NAS: `open "smb://10.10.11.185/Vault"`
2. Start LLM: `~/start-llm-server.sh gemma`
3. Start search API: `~/start-media-services.sh`
4. Start tool proxy (dev Mac): `python3 tool-proxy.py &`

## Safe Reboot Procedure

Active SMB mounts can cause kernel panics during reboot. Always follow this order:

1. Stop indexer: `~/stop-media-services.sh`
2. Stop LLM: `~/stop-llm-server.sh`
3. Unmount NAS: `umount /Volumes/Vault`
4. Wait 10 seconds
5. Reboot: `sudo reboot`

## Non-Goals

- Cloud deployment (local network only)
- User authentication (trusted LAN environment)
- HTTPS (all traffic stays on the local network)
- Real-time streaming transcription
- Direct file modification or deletion of vault media

## Resolved Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| GPU backend | Vulkan over native Metal | Metal is ~30% slower on AMD GPUs; MoltenVK translation overhead is negligible (~1-2%); the speed limit is VRAM bandwidth, not software |
| Database | SQLite over PostgreSQL | Zero dependencies, WAL mode handles concurrent reads/writes, FTS5 built-in for full-text search |
| Semantic search | ChromaDB with all-MiniLM-L6-v2 | Optional layer for embedding-based search, runs on CPU, complements keyword search |
| Language | Python 3.9 stdlib only | No pip dependencies for core indexer; Mac Pro has Python 3.9 installed, must avoid newer syntax |
| LLM parallelism | 2 concurrent workers | Leaves 1 of 3 `--parallel` slots free for interactive user queries while indexer runs |
