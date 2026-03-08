> ⚠️ ARCHIVE — This document describes a previous phase of the project and is kept for historical reference only. Do NOT use for current operations. Current doc: `PIPELINE.md`

# Media Indexer

AI-powered media search for the Northwoods vault (173 TB NAS at 10.10.11.185).

## How It Works
1. **Crawl**: Walks vault folders, registers media files (images, video, audio)
2. **Describe**: Sends images/keyframes to Gemma 3 12B vision for AI descriptions
3. **Transcribe**: Sends every video/audio file to Whisper for speech-to-text
4. **Store**: SQLite with FTS5 full-text search on descriptions, transcripts, filenames, and folder tags
5. **Search**: HTTP API on port 8081 + MCP server for Continue in VS Code

## Architecture

```
RX 580                 RX 580                 Pro 580X
  Gemma Q3_K_S           Gemma Q3_K_S           Whisper large-v3-turbo
  port 8090              port 8091              port 8092
       │                      │                      │
       └──────────┬───────────┘                      │
                  │                           WhisperWorker thread
       media-indexer.py (CPU)                 (pre-queued transcription)
       2 GPU workers + prep threads                  │
                  │                                  │
                  └──────────────┬───────────────────┘
                                 │
                      Search API :8081 (CPU)
                      FTS5 keyword search
```

- 2× RX 580 run Gemma 3 12B Q3_K_S independently (vision descriptions)
- Pro 580X runs Whisper large-v3-turbo (speech-to-text)
- All 3 servers auto-start on boot via Windows scheduled tasks (`Start-All-Servers` logon task, runs `start-all.py`)
- Vulkan device indices are non-deterministic — `start-all.py` handles assignment via crash-and-retry (see SERVERS.md)
- Native Vulkan drivers — true parallel GPU execution for text inference; vision inference serializes due to WDDM kernel behavior (see PRD.md §9)

**Pipeline components:**
- `media-indexer.py` — single Python file, no external pip dependencies for core indexer
- Uses `ffmpeg`/`ffprobe` for metadata and keyframes
- SQLite database with WAL mode for concurrent access
- 2 `PipelineWorker` instances (one per Gemma GPU), each with its own prep + inference threads
- `WhisperWorker` — dedicated thread for audio transcription (never blocks Gemma GPUs)

### Indexing Pipeline Detail

#### Gemma Workers (vision descriptions)
Each `PipelineWorker` manages one RX 580:
1. **Prep thread** — pulls files from the prepped queue (scene detection already done by SceneDetectPool), extracts keyframes via ffmpeg, feeds per-GPU task queue (prefetch buffer, maxsize=12)
2. **Inference thread** — pulls from per-GPU task queue, sends images to llama-server, writes descriptions to DB

The prep thread stays ahead of inference so the GPU is never waiting for CPU work.

#### Scene Detection (keyframe extraction)
Instead of fixed keyframes at 10%/50%/90%, the indexer detects actual scene changes using ffmpeg's built-in scene filter:
1. ffmpeg runs `select='gt(scene,threshold)',showinfo` on the video with VAAPI GPU decode
2. Scene change scores above the threshold trigger a cut detection
3. One keyframe extracted per detected scene (at 30% into each scene segment)
4. No artificial cap — keyframe count driven entirely by content (a 2-hour worship service with 200 cuts gets 200 keyframes)
5. Short videos (<5s) skip detection and get a single mid-frame
6. If detection fails, falls back to 1 keyframe at 50%
7. SceneDetectPool runs 3 workers (one per GPU decode ASIC) for parallel video decode

**Constants**: `SCENE_CHANGE_THRESHOLD = 0.3`

**Performance**: Fast — uses GPU hardware decode (VAAPI) across all 3 GPUs in parallel. Runs between the global queue and prep threads so it doesn't block inference.

**Logs to watch for**:
- `Scene detect <file>: X cuts → Y keyframes` — normal operation
- `Scene detect <file>: no cuts found, fallback to 1 keyframe` — single-shot/static video
- `Scene detection failed for <file>` — WARNING, ffmpeg couldn't extract frames

#### WhisperWorker (audio transcription)
Whisper pre-queuing ensures the Pro 580X stays busy throughout indexing:
1. At startup, ALL video/audio files in the pending queue are immediately added to the Whisper queue (not just the current batch)
2. WhisperWorker extracts audio with ffmpeg, sends to Whisper server (port 8092)
3. Writes transcripts directly to the database via shared write lock
4. Gemma GPUs are never starved waiting for Whisper (Issue #7 + Issue #19 fix)

### Startup

Servers auto-start via the `Start-All-Servers` Windows logon task which runs `start-all.py`. To manually trigger:

```bash
# Start all servers
ssh mediaadmin@10.10.11.157 "python3 C:\Users\mediaadmin\start-all.py"
```

### Running the Indexer
The indexer runs as a Windows scheduled task (`Run-Indexer`) via `run-indexer.bat`:
```bash
# Start indexer (mounts NAS share, runs media-indexer.py)
ssh mediaadmin@10.10.11.157 "schtasks /run /tn \"Run-Indexer\""

# Check indexer progress
ssh mediaadmin@10.10.11.157 "powershell -Command \"Get-Content 'C:\Users\mediaadmin\media-index\indexer-run.log' -Tail 30\""
```

The batch file embeds `net use` with NAS credentials to mount the share (required because SSH sessions can't access SMB shares — Issue #9).

## Commands
```bash
python3 ~/media-indexer.py index "/Volumes/Vault/Videos Vault/2024/Easter 2024"
python3 ~/media-indexer.py search "sunset landscape"
python3 ~/media-indexer.py status
python3 ~/media-indexer.py watch "/Volumes/Vault/Videos Vault"  # continuous
python3 ~/media-indexer.py serve 8081                           # HTTP search API
```

## Search API (port 8081)
- `GET /search?q=sunset&limit=20` — keyword search (FTS5, CPU-only, no LLM)
- `GET /status` — indexing progress
- `GET /health` — health check
- `GET /folders` — list tracked folders

## MCP Server for Continue
- File: `media-search-mcp.py` (in LLM Server project)
- Tools: `search_media`, `media_status`, `list_indexed_folders`, `search_person_media`, `list_known_persons`, `face_recognition_status`
- Configured in `~/.continue/config.yaml` under `mcpServers`
- Calls the HTTP search API on the Mac Pro

## Face Recognition

Detects, clusters, and identifies faces in images and video keyframes. Uses the `face_recognition` library (dlib, CPU-based) so it doesn't compete with the LLM GPUs.

**How it works:**
1. During indexing, faces are automatically detected in images and video keyframes
2. Run `faces cluster` to group same-person faces together
3. Name clusters via web UI at `http://10.10.11.157:8081/faces/ui` or CLI
4. Person names get added to the FTS search index, so "Jon red shirt snow" just works

### CLI Commands
```bash
python3 ~/media-indexer.py faces detect              # Scan indexed files for faces
python3 ~/media-indexer.py faces cluster [tolerance]  # Cluster faces (default tolerance: 0.5)
python3 ~/media-indexer.py faces assign               # Match new faces to known persons
python3 ~/media-indexer.py faces name <id> <name>     # Name a cluster
python3 ~/media-indexer.py faces merge <src> <dst>    # Merge two clusters
python3 ~/media-indexer.py faces persons              # List named persons
python3 ~/media-indexer.py faces status               # Face stats
python3 ~/media-indexer.py faces reset                # Clear all face data
```

### Face Management API (port 8081)
- `GET /faces/ui` — web management page
- `GET /faces/clusters` — list all clusters with sample thumbnails
- `GET /faces/persons` — list named persons
- `GET /faces/thumbnail?id=X` — face crop thumbnail
- `GET /faces/status` — face stats
- `POST /faces/name` — `{"cluster_id": 0, "name": "Jon"}` name a cluster
- `POST /faces/merge` — `{"source_cluster_id": 3, "target_cluster_id": 0}` merge clusters
- `POST /faces/cluster` — `{"tolerance": 0.5}` trigger re-clustering
- `POST /faces/assign` — assign new faces to known persons

### Database Tables
- `persons` — named people (id, name, face_count)
- `faces` — every detected face (id, file_id, keyframe_id, person_id, cluster_id, embedding BLOB, bbox, thumbnail_path)
- `files.face_names` — comma-separated person names (synced to FTS index for search)

### Key Paths on Mac Pro
- `C:\Users\mediaadmin\media-index\` — SQLite database, logs
- `C:\Users\mediaadmin\media-index\thumbnails\` — keyframe thumbnails
- `C:\Users\mediaadmin\media-index\face-thumbnails\` — cropped face images
- Face embeddings: 128-D float64 vectors stored as 1024-byte BLOBs in SQLite
