# Media Indexing Pipeline

**CURRENT — Ubuntu Server 24.04.4 LTS.** AI-powered media search for the Northwoods vault (173 TB NAS at 10.10.11.185).

---

## Architecture: Perpetual Task Pipeline (PTP)

The indexer uses a task-based architecture called the **Perpetual Task Pipeline**. Every file is a set of tasks. Every worker is independent. The system never stops.

```
NAS (/mnt/vault/)
      │
      ▼
 CrawlerWorker          ← polls every 5 min, walks vault folders
      │
      │  VIDEO  → transcribe task (+ scene_detect task in Phase 2+)
      │  AUDIO  → transcribe task
      │  IMAGE  → visual_analysis task + face_detect task (Phase 3+)
      │
      ├─────────────────────────────────────┐
      ▼                                     ▼
[ transcribe_q ]                   [ scene_detect_q ]  (Phase 2)
      │                                     │
 PerpetualWhisperWorker              SceneWorker ×3
   Pro 580X port 8092               VAAPI decode ASICs
      │                                     │
      │                              emits visual_analysis
      │                              + face_detect tasks
      │                                     │
      ▼                         ┌───────────┴──────────┐
   SQLite: transcript       [ visual_analysis_q ]  [ face_detect_q ]
                             GemmaWorker ×2 (Phase 3)   FaceWorker ×N (Phase 3)
                             RX 580 ports 8090/8091       CPU (dlib)

                    ┌──────────────────────┐
                    │   TaskCoordinator    │  ← always running
                    │  Watches task table  │
                    │  All tasks complete  │
                    │  → marks file indexed│
                    └──────────────────────┘
```

**Core rules:**
- The DB (`tasks` table) is the queue — no in-memory queues
- Workers claim tasks atomically: `UPDATE tasks SET status='assigned' WHERE status='pending' LIMIT 1`
- Workers loop forever — idle workers sleep 2s and poll
- TaskCoordinator owns file completion — workers never mark files indexed directly
- On startup: all `assigned` tasks reset to `pending` (handles prior crashes)

---

## Current Status: Phase 3 Running

**Enabled task types:** `transcribe`, `scene_detect`, `visual_analysis`

| Component | Status | Notes |
|---|---|---|
| CrawlerWorker | ✅ Running | 5-min crawl cycle; backfills 1,000 pending + 200 scene_detect + 500 visual_analysis/cycle |
| PerpetualWhisperWorker | ✅ Running | Prefetch pipeline active; ~0.28s gap between jobs |
| SceneWorker ×3 | ✅ Running | VAAPI decode; renderD128/129/130; watchdog+fallback confirmed working |
| GemmaWorker ×2 | ✅ Running | RX 580s ports 8090/8091; describes keyframes + assembles file-level descriptions |
| TaskCoordinator | ✅ Running | Marks files indexed within ~2–3s of all tasks completing |
| FaceWorker | ✅ Running | CPU-only dlib detection; claims face_detect tasks; single instance |

Backlog (Feb 25 2026): ~75,000 files with `file_type=NULL` pending transcription backfill; 15,751 keyframes without `ai_description`. Per crawl cycle: 500 transcribe tasks + 200 scene_detect tasks + 500 visual_analysis tasks + 500 face_detect tasks queued.

**Phase 3 confirmed (Feb 25 2026):** GemmaWorkers online; first descriptions written within 2 minutes of startup. `_assemble_file_description` correctly waits for all keyframes before writing file-level description. GemmaWorker throughput: ~14s/image, ~8 images/min combined across both GPUs.

**Phase 4 deployed (Feb 25 2026):** FaceWorker online; `face_detect` added to `ENABLED_TASK_TYPES`; `_backfill_face_detect` queues 500 tasks/cycle; ~4 hours to process all existing keyframes.

---

## CrawlerWorker

Walks all vault folders on a 5-minute timer. CIFS mounts don't support inotify, so polling is the only option.

**Each crawl cycle:**
1. Walk every configured vault folder
2. For each file: compute `file_id` from path+size+mtime
   - **New file** → register in `files` table, create tasks
   - **Known file, unchanged** → skip
   - **Known file, size/mtime changed** → mark old tasks stale, create fresh tasks
3. For each DB record in the folder: check if file still exists on NAS
   - **Gone** → cascade delete (tasks, keyframes, faces, thumbnails, FTS entry)
4. **Backfill (pending)** → create tasks for up to 1,000 existing `pending` files that have no tasks yet (handles pre-PTP legacy files and `file_type=NULL` migration)
5. **Backfill (transcribe)** → scan up to 5,000 `file_type=NULL` candidates, filter to video/audio by extension in Python, create `transcribe` tasks for up to 500 per cycle — fixes Whisper starvation from legacy files (see Issue #32)
6. **Backfill (scene_detect)** → create `scene_detect` tasks for up to 200 already-indexed videos that predate Phase 2
7. **Backfill (visual_analysis)** → create `visual_analysis` tasks for up to 500 keyframes with no `ai_description` and no existing task

Crawler also stamps `file_type` on `file_type=NULL` records when it creates their transcribe task, fixing the NULL for future queries.

Crawler always saves `file_type` on registration (video/audio/image inferred from extension).

---

## PerpetualWhisperWorker — Performance

**How it works:**
1. Claims the next `transcribe` task from `tasks` table (atomic UPDATE)
2. Immediately kicks off a background prefetch thread for the next task
3. Processes current task (audio extract → split into 300s chunks → transcribe each)
4. Writes transcript to `files.transcript` (FTS5 trigger updates search index automatically)
5. Marks task `complete` → TaskCoordinator picks it up and marks file `indexed`

**Prefetch optimization (Feb 25 2026):**

The original design had a sequential gap: ffmpeg audio extraction runs before the first chunk is sent to Whisper, leaving the GPU idle. Fix: extract audio for the next job in a background thread while the current job is transcribing.

| Metric | Before prefetch | After prefetch |
|---|---|---|
| Gap per job (start → chunk 1) | 22.4s (ffmpeg extraction) | 0.28s (`split_audio_into_chunks` only) |
| GPU idle % | ~36% | ~0.7% |
| 2-chunk files/hour | ~58 | ~89 |
| Throughput gain | — | ~53% |

**Observed timing (steady-state, confirmed Feb 25 2026):**
```
15:47:17,361  starting — Jon Step 1.mov
15:47:17,408  prefetch hit — Jon Step 1.mov        (+47ms: dict lookup)
15:47:17,641  transcribing chunk 1/2               (+233ms: split_audio_into_chunks)
15:47:48,699  transcribing chunk 2/2               (31s: chunk 1 transcription)
15:48:00,627  Jon Step 1.mov → 9729 chars          (12s: chunk 2 transcription)
15:48:00,694  starting — The Ultimate Lifestyle…   (67ms: claim next task)
15:48:00,727  prefetch hit                         (33ms: dict lookup)
15:48:00,968  transcribing chunk 1/2               (241ms: split)
```

The 0.28s remaining gap is `split_audio_into_chunks()` — splitting the pre-extracted .wav into 5-minute pieces. This is CPU + local disk only (no NAS I/O). It cannot be eliminated without pre-splitting during prefetch, which is not worth 280ms.

**The first job in any batch always has the full 22s extraction gap** — no prior job ran a prefetch for it. All subsequent jobs get prefetch hits.

**Prefetch ready time:** typically 2–7 seconds after a job starts (extraction completes well before the current job finishes).

---

## TaskCoordinator

Lightweight background thread. No inference, no media processing.

- Every 5 seconds: finds files where all tasks are `complete` or `abandoned` → writes `ai_description`, sets `status='indexed'`
- Resets tasks stuck in `assigned` for >30 minutes back to `pending` (handles crashed workers)
- Logs every file completion event

---

## SceneWorker

Scene detection uses VAAPI decode ASICs — separate hardware from the Vulkan compute used by Whisper and Gemma. SceneWorkers and WhisperWorker run fully parallel with zero interference (confirmed Feb 25 2026).

**Design:**
- 3 SceneWorkers, one per GPU VAAPI device (`renderD128`, `renderD129`, `renderD130`)
- Claims `scene_detect` tasks from `tasks` table
- Runs ffmpeg with `-hwaccel vaapi` and `-vf select='gt(scene,0.3)'`
- **Stuck-frame watchdog** instead of time cap: monitors `out_time` from `ffmpeg -progress pipe:1`. If `out_time` stops advancing for 30 seconds → kill and retry with CPU fallback → fall back to fixed-interval keyframes
- On completion: extracts keyframe thumbnails, emits `visual_analysis` + `face_detect` tasks

**Fixed-interval fallback** (only if watchdog fires twice):
- Duration < 5s: 1 mid-frame
- Duration 5s – 30min: frames at 30%, 60%, 90%
- Duration > 30min: 1 frame per `max(60s, duration/15)`

**Confirmed:** Whisper gap unchanged at ≤1s with all 3 SceneWorkers running simultaneously.

---

## Proprietary Camera Raw Formats (R3D and BRAW)

ffmpeg/ffprobe cannot read `.R3D` (RED) or `.braw` (Blackmagic) files. SceneWorker uses native SDK tools instead.

### BRAW (Blackmagic RAW)

ffprobe reads the MOV container (duration, audio stream), so transcription works natively. For keyframes:

- **Tool**: `braw-frame` — custom C++ tool built from Blackmagic RAW SDK 5.1
- **Paths**: Binary at `/usr/local/bin/braw-frame`, libraries at `/opt/brawsdk/`, camera datapacks at `/usr/share/blackmagicdesign/blackmagicraw/camerasupport/`
- **Flow**: `braw-frame info <clip>` → get frame count, fps, duration → fixed-interval timestamps → `braw-frame <clip> <frame_idx> <out.bmp>` → ffmpeg BMP→JPEG conversion

### R3D (RED Digital Cinema)

ffprobe cannot read R3D files at all (returns "Invalid data found"). SceneWorker uses probe-based extraction:

- **Tool**: `REDline Build 65.0.22` at `/usr/local/bin/REDline` (downloaded from red.com)
- **Flow**: Try frame indices `[0, 24, 120, 360, 720, 1440, 2880, 5760]` in sequence; stop on first failure (past end of clip). Timestamps approximated at 24fps. REDline command: `--format 3 --res 4 --start N --end N`
- **Duration**: Estimated from last successfully extracted frame + 2s; stored in DB
- **Caution**: `--resizeX` flag causes silent empty output — do not use it. Resize with ffmpeg after REDline instead.

**REDline libmpg123 conflict**: REDline installer copies old `libmpg123.so.0` to `/usr/local/lib/`. If ffmpeg starts failing with `undefined symbol: mpg123_param2`, remove it: `sudo rm /usr/local/lib/libmpg123.so* && sudo ldconfig`.

---

## Scene Detection Config

```python
SCENE_CHANGE_THRESHOLD = 0.3   # in media-indexer.py
```

---

## Face Recognition (Phase 3)

CPU-based, uses `face_recognition` library (dlib). Doesn't compete with GPU workers.

1. `FaceWorker` claims `face_detect` tasks after SceneWorker emits keyframe thumbnails
2. Detects faces, generates 128-D embeddings
3. `faces cluster` groups same-person faces (tolerance 0.5)
4. Person names added to FTS search index

---

## CLI Commands

All commands run on the Mac Pro at `/home/mediaadmin/media-indexer.py`.

```bash
ssh mediaadmin@10.10.11.157

# Pipeline (systemd — use these instead of running directly)
sudo systemctl restart media-indexer
journalctl -u media-indexer -f

# Manual index run (one folder, then exit)
python3 ~/media-indexer.py index "/mnt/vault/Videos Vault/2024/Easter 2024"

# Perpetual pipeline (what systemd runs)
python3 ~/media-indexer.py watch "/mnt/vault/Videos Vault" "/mnt/vault/Weekend Service Vault" ...

# Status
python3 ~/media-indexer.py status

# Search
python3 ~/media-indexer.py search "sunset landscape"

# Face recognition
python3 ~/media-indexer.py faces detect
python3 ~/media-indexer.py faces cluster [tolerance]
python3 ~/media-indexer.py faces assign
python3 ~/media-indexer.py faces name <id> <name>
python3 ~/media-indexer.py faces merge <src> <dst>
python3 ~/media-indexer.py faces persons
python3 ~/media-indexer.py faces status
python3 ~/media-indexer.py faces reset
```

---

## Search API (Port 8081)

Accessible from the network at `http://10.10.11.157:8081`.

| Method | Path | Parameters | Response |
|---|---|---|---|
| GET | `/search` | `q` (required), `limit` (optional, max 200) | JSON: query, count, results[] |
| GET | `/thumbnail` | `id` (required) | JPEG image bytes |
| GET | `/status` | — | JSON: counts by status, folders list |
| GET | `/health` | — | JSON: `{"status": "ok"}` |
| GET | `/folders` | — | JSON: indexed folders list |
| GET | `/faces/ui` | — | Web management page |
| GET | `/faces/clusters` | — | All clusters with sample thumbnails |
| GET | `/faces/persons` | — | Named persons list |
| GET | `/faces/thumbnail` | `id` (required) | Face crop thumbnail |
| GET | `/faces/status` | — | Face stats |
| POST | `/faces/name` | `{"cluster_id": 0, "name": "Jon"}` | — |
| POST | `/faces/merge` | `{"source_cluster_id": 3, "target_cluster_id": 0}` | — |
| POST | `/faces/cluster` | `{"tolerance": 0.5}` | — |
| POST | `/faces/assign` | — | Assign new faces to known persons |

---

## MCP Server for Continue

- File: `media-search-mcp.py` (runs on dev Mac)
- Tools: `search_media`, `media_status`, `list_indexed_folders`, `search_person_media`, `list_known_persons`, `face_recognition_status`
- Configured in `~/.continue/config.yaml` under `mcpServers`
- Calls the HTTP search API on the Mac Pro

---

## Data Model (SQLite)

Database at `/home/mediaadmin/media-index/index.db`.

| Table | Key Columns |
|---|---|
| `folders` | id, path, name, enabled, last_scan, file_count |
| `files` | id, path, filename, folder_id, file_type, size_bytes, modified_at, duration_seconds, width, height, codec, ai_description, transcript, face_names, tags, indexed_at, status, error_message |
| `tasks` | id (`{file_id}_{task_type}`), file_id, task_type, status, worker_id, created_at, started_at, completed_at, error_message, retry_count |
| `keyframes` | id, file_id, timestamp_seconds, thumbnail_path, ai_description |
| `files_fts` | FTS5 virtual table on filename, ai_description, transcript, face_names, tags |
| `persons` | id, name, face_count |
| `faces` | id, file_id, keyframe_id, person_id, cluster_id, embedding BLOB, bbox, thumbnail_path |

**Task states:** `pending → assigned → complete / failed → abandoned`

---

## Key Paths

| | |
|---|---|
| Database | `/home/mediaadmin/media-index/index.db` |
| Thumbnails | `/home/mediaadmin/media-index/thumbnails/` |
| Face thumbnails | `/home/mediaadmin/media-index/face-thumbnails/` |
| Vault mounts | `/mnt/vault/Videos Vault`, `/mnt/vault/Projects Vault`, `/mnt/vault/Weekend Service Vault`, `/mnt/vault/Stockfootage Vault` |
