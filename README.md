# AVL Media Indexer

AI-powered media search for Northwoods' 173 TB production vault. Runs on a local Intel Mac Pro (Ubuntu Server 24.04.4 LTS) — managed remotely via SSH from the dev Mac.

## Features

- **AI-powered descriptions**: Gemma 3 12B vision generates searchable descriptions for images and video keyframes
- **Speech transcription**: Whisper large-v3-turbo transcribes every video and audio file
- **Full-text keyword search**: SQLite FTS5 across descriptions, transcripts, filenames, and face names
- **Thumbnail serving**: On-demand JPEG thumbnails for video keyframes and images
- **Face recognition**: Detect, cluster, and name people — searchable by name
- **Multi-GPU inference**: llama.cpp with Vulkan across 3 AMD GPUs (24 GB total VRAM)
- **MCP integration**: Search the vault from VS Code via Continue

## Network Architecture

```
NAS (10.10.11.185)               Mac Pro (10.10.11.157)
  173TB Vault  ──SMB──>  ┌──────────────────────────────────┐
                         │  llama.cpp + Vulkan               │
                         │  RX 580 #1 → Gemma 3 12B :8090  │
                         │  RX 580 #2 → Gemma 3 12B :8091  │
                         │  Pro 580X  → Whisper      :8092  │
                         │                                   │
                         │  media-indexer.py (CPU)           │<──HTTP──> Vault Search App
                         │  - SQLite + FTS5 search           │
                         │  - Search API :8081               │
                         │  - Thumbnail serving              │
                         └──────────────────────────────────┘
                                                     ┌──────────────────┐
                                                     │ tool-proxy.py    │
                                                     │ (port 8083)      │
                                                     │ media-search-mcp │
                                                     │ (stdio MCP)      │
                                                     └──────────────────┘
```

| Port | Service | Host | Description |
|------|---------|------|-------------|
| 8090 | llama.cpp (Gemma) | Mac Pro | Vision descriptions — RX 580 #1 |
| 8091 | llama.cpp (Gemma) | Mac Pro | Vision descriptions — RX 580 #2 |
| 8092 | llama.cpp (Whisper) | Mac Pro | Audio transcription — Pro 580X |
| 8081 | media-indexer.py | Mac Pro | Search API, thumbnails, status |
| 8083 | tool-proxy.py | Dev Mac | Tool-calling proxy for Gemma |
| 49990 | dashboard-agent.py | Mac Pro | AVL Dashboard Agent (mDNS discovery) |

## Management

All management is done via SSH from the dev Mac:

```bash
ssh mediaadmin@10.10.11.157
```

### Starting Servers

Servers auto-start on boot via systemd. To manage manually:

```bash
# Check server health
ssh mediaadmin@10.10.11.157 "curl -s http://localhost:8090/health && curl -s http://localhost:8091/health && curl -s http://localhost:8092/health"

# Restart a service
ssh mediaadmin@10.10.11.157 "sudo systemctl restart gemma0"
```

### Indexing

The indexer runs as a systemd service (`media-indexer.service`):

```bash
# Check indexer status
ssh mediaadmin@10.10.11.157 "sudo systemctl status media-indexer"

# Check indexer logs
ssh mediaadmin@10.10.11.157 "sudo journalctl -u media-indexer -n 30"

# Check overall status
curl http://10.10.11.157:8081/status
```

### Starting Tool Proxy (Dev Mac)

```bash
python3 tool-proxy.py &
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
      "path": "\\\\Vault\\Videos Vault\\2024\\Easter\\sunset-wide.mp4",
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
    {"path": "\\\\Vault\\Videos Vault", "name": "Videos Vault", "count": 18000, "last_scan": "2026-02-15T12:00:00"}
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
LLM Server/
├── media-indexer.py          # Core indexer + search API + thumbnail server
├── media-search-mcp.py       # MCP server for VS Code / Continue
├── tool-proxy.py             # Tool-calling proxy for Gemma
├── PRD.md                    # Product requirements document
├── CLAUDE.md                 # Project context for Claude Code
├── HARDWARE.md               # Machine, OS, GPU layout, fan control
├── SERVERS.md                # Systemd services, management commands
├── MODELS.md                 # Model selection and VRAM guide
├── PIPELINE.md               # Indexing pipeline, scene detection, faces
├── ISSUES.md                 # Known issues and debugging log
├── OPERATIONS.md             # 24/7 reliability, monitoring, watchdog, recovery
├── HISTORY.md                # macOS → Windows → Ubuntu migration story
├── SECURITY.md               # Security considerations
└── CREDITS.md                # Third-party credits and licenses
```

## Security

This system is designed for **local network use only** with no authentication. See [SECURITY.md](SECURITY.md) for the full security review.

**Do not expose these services to the internet.**

## License

[MIT](LICENSE) - Copyright (c) 2026 Northwoods Community Church

## Credits

See [CREDITS.md](CREDITS.md) for full attribution of third-party libraries, models, and tools.
