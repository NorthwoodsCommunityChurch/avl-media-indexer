# LLM Server — Product Requirements Document

## 1. Project Overview

**What it is**: Local AI-powered media search server for Northwoods Community Church production team.

**Who it's for**: Editors searching the 173 TB NAS vault for usable footage, photos, and audio.

**Why it exists**: The NAS is unsearchable without AI — editors waste time hunting for clips manually. This system crawls the vault, generates AI descriptions of every file, and provides a natural-language search API so editors can find what they need instantly.

---

## 2. System Architecture

### Hardware

| Component | Details |
|-----------|---------|
| Machine | Intel Mac Pro 2019 (cheese grater tower, Mac Pro 7,1), hostname `dc-macpro`, Xeon W-3245, 96 GB RAM |
| GPUs | 2× AMD Radeon RX 580 (8 GB each), 1× AMD Radeon Pro 580X (8 GB) = 24 GB VRAM |
| OS | Ubuntu Server 24.04.4 LTS (kernel `6.12.74-1-t2-noble`) |
| IP | 10.10.11.157 (DHCP reservation) |
| NAS | 10.10.11.185 — 173 TB Vault, mounted via CIFS at `/mnt/vault/` |

### Inference Stack

| Service | Model | GPU | Port | Role |
|---------|-------|-----|------|------|
| llama.cpp | Gemma 3 12B Q3_K_S | RX 580 #1 | 8090 | Vision descriptions |
| llama.cpp | Gemma 3 12B Q3_K_S | RX 580 #2 | 8091 | Vision descriptions |
| llama.cpp | Whisper large-v3-turbo | Pro 580X | 8092 | Audio transcription |

Backend: Vulkan, Mesa native Linux drivers (not MoltenVK). One model per GPU. `--parallel 1` only.

### Software Components

| Component | File | Runs On | Port | Purpose |
|-----------|------|---------|------|---------|
| Media Indexer | `media-indexer.py` | Mac Pro | 8081 | Crawl, describe, search, serve thumbnails |
| Tool Proxy | `tool-proxy.py` | Dev Mac | 8083 | Translate OpenAI tool-calling for Gemma |
| MCP Server | `media-search-mcp.py` | Dev Mac | stdio | VS Code / Continue integration |

### Management

- **SSH**: `ssh mediaadmin@10.10.11.157` from dev Mac
- **Server startup**: systemd services (`gemma0`, `gemma1`, `whisper`, `media-indexer`, `media-search`, `dashboard-agent`)
- **Key paths**: `/home/mediaadmin/` (scripts), `/home/mediaadmin/media-index/` (DB, thumbnails, logs), `/home/mediaadmin/models/` (GGUF files), `/mnt/vault/` (NAS)

### Architecture Diagram

```
NAS (10.10.11.185)               Mac Pro dc-macpro (10.10.11.157) — Ubuntu
  173TB Vault  ──CIFS──> ┌──────────────────────────────────┐
  /mnt/vault/            │  llama.cpp + Vulkan (Mesa)        │
                         │  RX 580 #1 → Gemma 3 12B :8090  │ (127.0.0.1 only)
                         │  RX 580 #2 → Gemma 3 12B :8091  │ (127.0.0.1 only)
                         │  Pro 580X  → Whisper      :8092  │ (127.0.0.1 only)
                         │                                   │
                         │  media-indexer.py (CPU)           │
                         │  - SQLite + FTS5 keyword search   │<──HTTP──> Vault Search App
                         │  - Search API :8081 (0.0.0.0)    │
                         │  - Thumbnail serving              │
                         └──────────────────────────────────┘
                                                     ┌──────────────────┐
                                                     │ tool-proxy.py    │
                                                     │ (port 8083)      │
                                                     │ media-search-mcp │
                                                     │ (stdio MCP)      │
                                                     └──────────────────┘
```

---

## 3. Goals

| Priority | Goal |
|----------|------|
| Primary | Editors can find any clip, photo, or audio by describing it in plain language |
| Accuracy | Detailed, accurate AI descriptions matter more than indexing speed |
| Utilization | When files are queued, GPUs should rarely be idle |
| Coverage | Eventually index everything on the Vault |

---

## 4. In Scope / Out of Scope

**In scope:**
- Crawling and indexing images, video, and audio from the NAS vault
- AI-generated descriptions via Gemma 3 12B vision
- Speech transcription via Whisper
- Face detection, clustering, and name assignment
- Natural language keyword search (FTS5) across descriptions, transcripts, filenames, face names
- Thumbnail serving
- MCP integration for VS Code / Continue

**Out of scope:**
- File editing, downloading, or exporting vault media
- Real-time live capture or streaming
- Authentication or access control
- Cloud deployment

---

## 5. Indexing Requirements

### Supported Formats

| Type | Extensions |
|------|-----------|
| Images | `.jpg`, `.png`, `.tif`, `.webp` |
| Video | `.mp4`, `.mov`, `.mxf`, `.braw`, `.r3d` |
| Audio | `.wav`, `.mp3`, `.flac`, `.aif` |

### Per-File Processing

1. **Probe**: Extract metadata via ffprobe (duration, resolution, codec)
2. **Keyframes**: Detect scene changes via ffmpeg's built-in scene filter (`select='gt(scene,0.3)'`) with VAAPI GPU decode, extract one keyframe per scene
3. **Describe**: Send images/keyframes to Gemma 3 12B vision for AI description
4. **Transcribe**: Send every video/audio file with an audio track to Whisper
5. **Faces**: Detect, cluster, and name faces in images and keyframes
6. **Store**: Write to SQLite + FTS5; thumbnails saved to disk

### Quality Targets (current implementation)

- Variable keyframes per video (1 per detected scene change; short videos get 1 mid-frame)
- AI descriptions cover subject, basic setting
- Transcription covers video/audio with audio tracks
- Face names searchable via FTS5

---

## 6. Search Requirements

- Natural language keyword search across: filename, AI description, transcript, face names, folder tags
- FTS5 keyword matching (no ChromaDB, no embeddings — not needed)
- Returns: file path, description excerpt, duration/resolution, thumbnail URL
- Face-based search: find clips by person name
- Read-only API — no file modification

### API Endpoints (Port 8081)

| Method | Path | Parameters | Response |
|--------|------|------------|----------|
| GET | `/search` | `q` (required), `limit` (optional, max 200) | JSON: query, count, results[] |
| GET | `/thumbnail` | `id` (required) | JPEG image bytes |
| GET | `/status` | — | JSON: counts by status, folders list |
| GET | `/health` | — | JSON: `{status: "ok"}` |
| GET | `/folders` | — | JSON: indexed folders list |

---

## 7. Hardware Utilization Requirements

The indexing pipeline must keep GPUs fed continuously:

- **Whisper pre-queuing**: WhisperWorker receives ALL video/audio files upfront (not just the current batch). This lets the Pro 580X work ahead while Gemma GPUs process vision. (Implemented — Issue #19)
- **Gemma prefetch**: Each GPU worker has a prep thread that pre-extracts keyframes into a queue so the GPU never waits for CPU work
- **No GPU should sit idle** while pending files exist in the queue

---

## 8. Data Model (SQLite)

| Table | Key Columns |
|-------|------------|
| `folders` | id, path, name, enabled, last_scan, file_count |
| `files` | id, path, filename, folder_id, file_type, size_bytes, modified_at, duration_seconds, width, height, codec, ai_description, transcript, face_names, tags, indexed_at, status, error_message |
| `keyframes` | id, file_id, timestamp_seconds, thumbnail_path, ai_description |
| `files_fts` | FTS5 virtual table on filename, ai_description, transcript, face_names, tags |
| `persons` | id, name, face_count |
| `faces` | id, file_id, keyframe_id, person_id, cluster_id, embedding BLOB, bbox, thumbnail_path |

---

## 9. Known Limitations (Hardware — Won't Fix)

### GPU Serialization (AMD Vulkan / WDDM)

**What happens**: All 3 GPUs take turns — the two RX 580s running Gemma vision inference AND the Pro 580X running Whisper all serialize against each other. When any GPU is doing heavy inference, the others pause. (~67s wall clock for 2 Gemma GPUs vs ~37s for a single GPU.) Text inference is perfectly parallel.

**Root cause**: AMD WDDM kernel driver (`amdkmdag.sys`) serializes large sustained compute dispatches across ALL Polaris GPUs in the system, regardless of model or role. SigLIP vision encoder triggers this; per-token text generation does not. The serialization is system-wide, not just between cards running the same model.

> **Important**: All benchmark data below and the `amdkmdag.sys` root cause were identified while the machine ran Windows/Atlas OS. The machine now runs Ubuntu Server with Mesa radeonsi drivers — a completely different driver stack. Serialization has **not been re-tested on Linux**. Results may differ significantly. See Issue #24.

**Benchmark data (collected on Windows — unconfirmed on Linux):**

| Test | Time |
|------|------|
| Single GPU vision | ~37s |
| Dual GPU vision (wall clock) | ~67s |
| Single GPU text | ~8.5s |
| Dual GPU text (wall clock) | ~8.6s (perfect parallel) |

> **Note (corrected Feb 2026):** Earlier data showed "Both vision + Whisper simultaneously | 66.95s (Whisper unaffected at 11.59s)." This conclusion was wrong — Whisper serializes with Gemma vision just like the two Gemma GPUs serialize with each other. The 11.59s figure reflected a short test clip that may have completed before the serialization bottleneck was observable. Use `python3 gpu-monitor.py --benchmark` to confirm the full 3-way serialization pattern.

**Dead ends confirmed:**

| Approach | Why It Failed |
|----------|--------------|
| HAGS | Requires RDNA (RX 5000+). Polaris has no on-chip scheduler hardware. |
| llama.cpp `--split-mode tensor` PR #19378 | Merged to mainline, Vulkan crashes with driver errors. CUDA-only. |
| ik_llama.cpp split mode graph | 3-4x on CUDA, crashes on Vulkan. Not available for AMD. |
| ROCm/HIP | AMD dropped Polaris (gfx803) support after ROCm 4.x. |
| Ollama / KoboldCpp | Both use ggml-vulkan internally. Same serialization problem. |
| AMD Compute Mode registry keys | Doubled mining throughput on RX 580s, but no effect on vision serialization. Tested Feb 2026. |
| `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1` | 5x prompt improvement on some systems, no effect on vision serialization. Left enabled — may help other workloads. Tested Feb 2026. |
| Native Metal backend | GPU timeout crash after 72s on CLIP encoding. Apple Silicon-optimized shaders too slow on AMD. |
| MoltenVK (macOS) | Serializes Vulkan→Metal at system level. Moved to Windows to fix. |
| DirectML via ONNX Runtime | Windows-only (DirectX 12); machine runs Ubuntu. Also requires ONNX models (not GGUF); DirectML in maintenance mode; limited VLM support in ONNX Runtime. |

**Remaining options (not yet tested):**
- **Stagger vision requests**: Offset GPU work by 1-2s so SigLIP encoder phases don't overlap. Would get partial parallelism during text generation. Low effort, worth trying.
- **Switch to Gemma 3 4B**: Same SigLIP encoder (~35s/image), but smaller language model = more VRAM headroom and faster text generation. Better quality-per-bit than current 12B Q3_K_S.

### Other Known Limits

- **Context limited to 1024**: `-c 2048` OOMs on 8 GB RX 580 during vision inference (Issue #11)
- **DJI drone files may have no audio**: Skipped by Whisper (Issue #17)

---

## 10. Ubuntu/systemd Operational Notes

- **Systemd replaces schtasks**: All services run as systemd units. `sudo systemctl start/stop/restart SERVICE` replaces the old batch file + schtasks approach. Processes survive SSH disconnect automatically.
- **Vulkan indices non-deterministic**: Still true on Linux. The systemd service files use `--device VulkanN` values that were set at configuration time. Verify with `llama-server --list-devices` if behavior seems wrong after hardware changes or reboots.
- **NAS via fstab**: CIFS mount at `/mnt/vault/` is configured in `/etc/fstab` with `_netdev,nofail`. Media indexer runs as a systemd service and has full access to `/mnt/vault/` at startup (no SSH session restriction).
- **LLM servers bind to 127.0.0.1**: Unlike Windows, the Ubuntu services are configured to only accept connections from localhost. This is intentional — `media-indexer.py` runs on the same machine.

> **Historical**: For the Windows/Atlas OS operational findings that originally occupied this section, see `HISTORY.md`.

---

## 11. Quality Roadmap

These are future improvements, not part of the current implementation:

| Item | Description |
|------|-------------|
| Richer Gemma prompts | Describe scene in detail — subjects, lighting, mood, colors, text, time of day, actions |
| ~~More keyframes~~ | ~~Scale keyframe count by clip duration~~ — replaced by scene detection (shipped Feb 2026) |
| Face auto-detection | Run face detection as part of the indexing pipeline (not a manual trigger) |
| Full Vault coverage | Add all NAS folders to the indexer (currently only 2 folders indexed) |
| Stagger vision requests | Only relevant if Issue #24 confirms serialization is present on Linux |

---

## 12. Resolved Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| GPU backend | Vulkan (native Linux/Mesa) | MoltenVK serialized on macOS; native Vulkan on Windows solved that; machine now runs Ubuntu with Mesa radeonsi |
| Database | SQLite + FTS5 | Zero dependencies, WAL mode, FTS5 built-in keyword search |
| Semantic search | FTS5 only (no ChromaDB) | Keyword search across detailed AI descriptions works well; embeddings not needed |
| Parallelism | `--parallel 1` per GPU | `--parallel 2` pushed VRAM to ~7.3 GB on 8 GB cards, crashing AMD driver |
| Language | Python 3.x stdlib | No pip dependencies for core indexer |
| Scheduling | One model per GPU | Sharing a model across GPUs via layer-split serializes on Windows/WDDM; also avoids VRAM contention on 8 GB cards |
