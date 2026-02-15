# AVL Media Indexer

AI-powered media search for Northwoods' 173TB production vault, running on a Mac Pro with local LLM inference.

## Features

- **AI-powered descriptions**: Gemma 3 12B vision generates searchable descriptions for images and video keyframes
- **Hybrid search**: Semantic search (ChromaDB) combined with full-text keyword search (SQLite FTS5)
- **Thumbnail serving**: On-demand JPEG thumbnails for video keyframes and images
- **Multi-GPU inference**: llama.cpp with Vulkan across 3 AMD GPUs (24GB total VRAM)
- **Zero external dependencies**: Core indexer uses only Python 3.9 standard library
- **Concurrent indexing**: 2-worker pipeline processes files while keeping a GPU slot free for queries
- **MCP integration**: Search the vault from VS Code via Continue

## Requirements

- macOS (tested on macOS 12+ Intel)
- Mac Pro or similar with AMD GPUs (Vulkan via MoltenVK)
- Python 3.9+
- ffmpeg and ffprobe (static builds at `/usr/local/bin/`)
- llama.cpp built with Vulkan backend
- NAS mounted via SMB

## Network Architecture

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

| Port | Service | Host | Description |
|------|---------|------|-------------|
| 8080 | llama.cpp | Mac Pro | OpenAI-compatible LLM inference API |
| 8081 | media-indexer.py | Mac Pro | Search API, thumbnails, status |
| 8083 | tool-proxy.py | Dev Mac | Tool-calling proxy for Gemma |

## Installation

### Mac Pro Setup

1. Build llama.cpp with Vulkan:
   ```bash
   cd ~/llama.cpp
   cmake -B build -DGGML_VULKAN=ON -DGGML_METAL=OFF -DCMAKE_BUILD_TYPE=Release
   cmake --build build --config Release -j$(sysctl -n hw.ncpu)
   ```

2. Install static ffmpeg builds to `/usr/local/bin/`

3. Copy scripts and indexer to the Mac Pro:
   ```bash
   scp media-indexer.py mediaadmin@10.10.11.173:~/
   scp start-llm-server.sh stop-llm-server.sh status-llm-server.sh mediaadmin@10.10.11.173:~/
   scp start-media-services.sh stop-media-services.sh mediaadmin@10.10.11.173:~/
   ```

4. (Optional) Install ChromaDB for semantic search:
   ```bash
   ssh mediaadmin@10.10.11.173 "pip3 install chromadb"
   ```

### Dev Mac Setup

No installation required. Run `tool-proxy.py` and `media-search-mcp.py` directly with Python 3.

## Usage

### Startup

```bash
# 1. Mount the NAS vault
open "smb://10.10.11.185/Vault"

# 2. Start LLM server (on Mac Pro)
ssh mediaadmin@10.10.11.173 "~/start-llm-server.sh gemma"

# 3. Start search API (on Mac Pro)
ssh mediaadmin@10.10.11.173 "nohup python3 ~/media-indexer.py serve 8081 > ~/media-index/serve.log 2>&1 &"

# 4. Start tool proxy (on dev Mac)
python3 tool-proxy.py &
```

### Indexing

```bash
# Index a specific folder
ssh mediaadmin@10.10.11.173 'nohup python3 ~/media-indexer.py index "/Volumes/Vault/Videos Vault/2024" > ~/media-index/indexer.log 2>&1 &'

# Watch for new files continuously
ssh mediaadmin@10.10.11.173 'nohup python3 ~/media-indexer.py watch "/Volumes/Vault/Videos Vault" > ~/media-index/indexer.log 2>&1 &'

# Check indexing progress
ssh mediaadmin@10.10.11.173 "python3 ~/media-indexer.py status"

# Re-embed all descriptions into ChromaDB
ssh mediaadmin@10.10.11.173 "python3 ~/media-indexer.py reembed"
```

### Safe Reboot

Active SMB mounts can cause kernel panics. Always follow this order:

```bash
ssh mediaadmin@10.10.11.173 "~/stop-media-services.sh"
ssh mediaadmin@10.10.11.173 "~/stop-llm-server.sh"
ssh mediaadmin@10.10.11.173 "umount /Volumes/Vault"
sleep 10
ssh mediaadmin@10.10.11.173 "sudo reboot"
```

## API Reference

### Search

```
GET /search?q=sunset&limit=20
```

```json
{
  "query": "sunset",
  "count": 3,
  "results": [
    {
      "id": "a1b2c3d4e5f67890",
      "path": "/Volumes/Vault/Videos Vault/2024/Easter/sunset-wide.mp4",
      "filename": "sunset-wide.mp4",
      "type": "video",
      "description": "Wide shot of sunset over hills, warm golden light...",
      "tags": "Videos Vault, 2024, Easter",
      "duration": 45.2,
      "width": 1920,
      "height": 1080
    }
  ]
}
```

### Thumbnail

```
GET /thumbnail?id=a1b2c3d4e5f67890
```

Returns JPEG image bytes with `Cache-Control: public, max-age=86400`.

### Status

```
GET /status
```

```json
{
  "counts": {"pending": 17900, "indexed": 67, "error": 0, "offline": 0},
  "folders": [["path", 1200, "2026-02-15T12:00:00"]]
}
```

### Health

```
GET /health
```

```json
{"status": "ok"}
```

### Folders

```
GET /folders
```

```json
{
  "folders": [
    {"path": "/Volumes/Vault/Videos Vault", "name": "Videos Vault", "count": 18000, "last_scan": "2026-02-15T12:00:00"}
  ]
}
```

## Supported Media Formats

| Type | Extensions |
|------|-----------|
| Images | .jpg, .jpeg, .png, .tif, .tiff, .bmp, .webp |
| Video | .mp4, .mov, .mxf, .m4v, .avi, .mkv, .r3d, .braw |
| Audio | .wav, .mp3, .aac, .m4a, .flac, .aif, .aiff |

## Project Structure

```
avl-media-indexer/
├── media-indexer.py          # Core indexer + search API + thumbnail server
├── media-search-mcp.py       # MCP server for VS Code / Continue
├── tool-proxy.py             # Tool-calling proxy for Gemma
├── start-llm-server.sh       # Start llama.cpp server
├── stop-llm-server.sh        # Stop llama.cpp server
├── status-llm-server.sh      # Check server status
├── start-media-services.sh   # Start search API + optional watcher
├── stop-media-services.sh    # Stop search API and watcher
├── PRD.md                    # Product requirements document
├── SECURITY.md               # Security review and network exposure
├── CREDITS.md                # Third-party credits and licenses
├── LICENSE                   # MIT License
└── CLAUDE.md                 # Project context for Claude Code
```

## Security

This system is designed for **local network use only** with no authentication. See [SECURITY.md](SECURITY.md) for the full security review including network exposure, input validation, and recommendations.

**Do not expose these services to the internet.**

## License

[MIT](LICENSE) - Copyright (c) 2026 Northwoods Community Church

## Credits

See [CREDITS.md](CREDITS.md) for full attribution of third-party libraries, models, and tools.
