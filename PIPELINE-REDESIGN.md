# Perpetual Task Pipeline — Design

This document describes the target architecture for the media indexer. The goal is an indexer that runs continuously, assigns discrete tasks to independent workers, and always knows the exact state of every file.

---

## The Concept: Perpetual Task Pipeline

**Every file is a set of tasks. Every worker is independent. The system never stops.**

When a file is discovered, the system creates all the tasks it will ever need. Each task goes into the appropriate queue. Workers pull from queues independently — they don't coordinate with each other, don't wait on other worker types, and don't care about queue depth. As long as there's a task in the queue, a worker picks it up.

A **Task Coordinator** watches the task table. When all tasks for a file are complete, it assembles the final record and marks the file indexed. This is the only centralized logic.

The system never exits. Workers loop forever. When queues are empty, workers sleep and poll. When new work arrives, they wake up. The crawler runs on a timer — not real-time, but frequent enough that new files on the NAS are indexed within a reasonable window.

---

## Core Principles

1. **A task is the unit of work** — not a file, not a batch. Every distinct operation on a file is its own task with its own status.
2. **Workers are independent** — a Gemma worker doesn't know or care what the other Gemma worker is doing. If there's a task in the queue, it starts.
3. **Queues are shared within a worker type** — all scene detect workers share one queue. Whoever finishes first takes the next job. No pre-assignment.
4. **The pipeline has one dependency rule** — visual analysis and face detection depend on scene detection. Everything else (transcription, scene detection) starts immediately after discovery. Nothing else blocks anything else.
5. **The Task Coordinator owns completion** — workers write results and mark their task done. The Coordinator detects when all tasks for a file are complete and finalizes it.
6. **The DB is the queue** — task state is persisted in SQLite. A crashed worker leaves tasks in `assigned` state; on restart, they reset to `pending`. In-memory state is never the source of truth.

---

## Task Types

| Task | Created by | Input | Output | Worker |
|------|-----------|-------|--------|--------|
| `scene_detect` | Crawler (on video discovery) | video file path | keyframe timestamps → emits `visual_analysis` + `face_detect` tasks | SceneWorker |
| `transcribe` | Crawler (on video/audio discovery) | video or audio file path | transcript text → DB | WhisperWorker |
| `visual_analysis` | SceneWorker (after scene detect) | keyframe thumbnail path | AI description → DB | GemmaWorker |
| `face_detect` | SceneWorker (after scene detect, same keyframes) | keyframe thumbnail path | face embeddings, clusters → DB | FaceWorker |

Images skip `scene_detect` entirely — the Crawler emits `visual_analysis` and `face_detect` directly.

Audio skips everything except `transcribe`.

---

## Workers and Hardware

| Worker | Count | Hardware | Queue | Notes |
|--------|-------|----------|-------|-------|
| **Crawler** | 1 | CPU | — | Polls NAS on timer, writes tasks to DB, detects removals |
| **SceneWorker** | 3 | renderD128 / D129 / D130 (VAAPI decode ASICs) | `scene_detect_q` | VAAPI uses decode hardware — doesn't compete with Vulkan compute |
| **WhisperWorker** | 1 | Pro 580X — port 8092 | `transcribe_q` | Handles audio chunking internally (300s chunks, sequential per file) |
| **GemmaWorker** | 2 | RX 580 × 2 — ports 8090/8091 | `visual_analysis_q` | GGML_VK_VISIBLE_DEVICES=1/2, true parallel confirmed |
| **FaceWorker** | N | CPU (24 threads, dlib) | `face_q` | CPU-only, doesn't compete with GPU workers |
| **Task Coordinator** | 1 | CPU | — | Watches task table, marks files complete, handles retries |

---

## Pipeline Flow

```
NAS (CIFS mount at /mnt/vault/)
        │
        ▼
  ┌─────────────┐
  │   Crawler   │  ← runs on timer (every ~10 min)
  │             │    walks directory tree, checks mtime
  └──────┬──────┘
         │  on discovery:
         │  VIDEO  → scene_detect task + transcribe task
         │  IMAGE  → visual_analysis task + face_detect task
         │  AUDIO  → transcribe task
         │  on removal → cascade delete from DB
         │
         ├──────────────────────────────────────────┐
         │                                          │
         ▼                                          ▼
  [ scene_detect_q ]                      [ transcribe_q ]
         │                                          │
   ┌─────┼─────┐                             [ WhisperWorker ]
   │     │     │                             Pro 580X port 8092
[SW0] [SW1] [SW2]                                   │
 D128  D129  D130                                   └──► DB: transcript
   │     │     │
   └──┬──┘     │
      └─────┬──┘
            │  SceneWorker, for each keyframe found:
            │    → extract thumbnail
            │    → emit visual_analysis task
            │    → emit face_detect task
            │
      ┌─────┴──────────────────────────┐
      │                                │
      ▼                                ▼
[ visual_analysis_q ]           [ face_detect_q ]
      │                                │
  ┌───┴───┐                    [ FaceWorkers × N ]
[GW0]   [GW1]                    CPU, dlib
port    port                         │
8090    8091                         └──► DB: faces, embeddings
  │       │
  └───┬───┘
      └──► DB: keyframe descriptions

                         ┌─────────────────────┐
                         │  Task Coordinator   │  ← always running
                         │                     │
                         │  Watches task table  │
                         │  When all tasks for  │
                         │  a file = complete:  │
                         │  - assemble summary  │
                         │  - write ai_description│
                         │  - mark file indexed │
                         └─────────────────────┘
```

---

## The Task Coordinator

The Task Coordinator is a lightweight background thread. It does not do any inference or media processing — it only manages state.

**Responsibilities:**
1. Detect when all tasks for a file are `complete` → assemble the file's `ai_description` from keyframe descriptions, write it, mark file `status = indexed`
2. Detect `failed` tasks that are eligible for retry → reset to `pending` after a backoff delay
3. Detect tasks stuck in `assigned` state too long → reset to `pending` (handles crashed workers)
4. Log file-level completion events

**File completion logic:**
A file is complete when every task with its `file_id` has `status = complete`. The Coordinator queries for files where all tasks are complete but the file is not yet `indexed`. This query runs every few seconds — it's lightweight because it's just a SQL GROUP BY count check.

For video files, the Coordinator assembles the final `ai_description` as a summary of all keyframe descriptions once they're all in. For images, it's just the single `visual_analysis` result. For audio, it's just the transcript.

---

## Scene Detection: Stuck-Frame Watchdog

Instead of a time cap, scene detection uses a **progress watchdog**. ffmpeg reports the timestamp it is currently processing (`out_time`) via its `-progress` flag. If that timestamp stops advancing for more than N seconds, the process is stuck — not slow, but genuinely hung (NAS I/O stall, kernel issue, corrupted frame).

**How it works:**
1. Run ffmpeg with `-progress pipe:1` in addition to the scene filter on stderr
2. A watchdog thread reads `out_time` from ffmpeg's progress output
3. If `out_time` hasn't advanced in **30 seconds**, kill the process
4. **On first kill**: retry once with CPU decode fallback (no VAAPI) — sometimes VAAPI hangs on certain codec profiles
5. **On second kill**: fall back to fixed-interval keyframe extraction — one keyframe every `max(60s, duration / 15)` seconds

This handles every case:
- **Edited content with many cuts**: ffmpeg processes quickly, watchdog never fires
- **Long continuous recordings (SDDP, C06xx)**: ffmpeg runs the whole file at full speed — no cuts detected, but no timeout either. A 67-minute file finishes in a few minutes. Falls back to fixed interval only if it actually hangs.
- **Corrupt or NAS-stalled file**: `out_time` freezes → killed after 30s → retry → fall back

**Fixed-interval fallback** (only reached if watchdog fires twice):
- Duration < 5s: 1 mid-frame
- Duration 5s – 30min: 1 frame at 30%, 60%, 90% (3 keyframes minimum coverage)
- Duration > 30min: one frame every `max(60s, duration / 15)` — a 67-min file gets ~15 keyframes

---

## The Crawler

The Crawler is not a queue worker — it's a timer loop that owns the "discovery" and "removal" logic.

**Crawl cycle (every ~10 minutes):**
1. Walk each configured vault folder
2. For each directory: check its mtime. If unchanged since last crawl, skip its contents entirely (NAS mtime updates on file add/remove)
3. For each file in a changed directory: check if it exists in the DB
   - **New file**: register it, create its tasks, enqueue them
   - **Modified file** (size or mtime changed): mark existing tasks `stale`, create fresh tasks
   - **Known file, no change**: skip
4. For each DB record in the folder: check if the file still exists on NAS
   - **Gone**: delete file record + cascade (keyframes, faces, thumbnails, FTS entries)

**Crawl interval**: Every 10 minutes is reasonable. A file shot during a Sunday service will be indexed within ~10 minutes of landing on the NAS. This is fast enough for the use case — editors don't need sub-second freshness.

**On startup**: Full crawl runs immediately, then on the timer. Workers start before the crawl completes — they begin pulling tasks as soon as the first files are enqueued.

---

## Task State Machine

Tasks live in a `tasks` table in SQLite. This is the authoritative queue — in-memory queues are not used.

```
pending ──► assigned ──► complete
                │
                └──► failed ──► (retry after backoff) ──► pending
                         │
                         └──► (max retries exceeded) ──► abandoned
```

| State | Meaning |
|-------|---------|
| `pending` | Waiting to be claimed by a worker |
| `assigned` | Claimed by a worker, in progress |
| `complete` | Worker finished successfully |
| `failed` | Worker encountered an error |
| `abandoned` | Failed too many times, needs manual review |

Workers claim tasks by setting `status = assigned, worker_id = <id>, started_at = now` in a single atomic UPDATE. Only the worker that wins that UPDATE processes the task.

On startup: all `assigned` tasks are reset to `pending` (handles prior crash).

---

## File Status

The `files` table `status` field tracks overall file state:

| Status | Meaning |
|--------|---------|
| `discovered` | File registered, tasks created, not yet processed |
| `indexing` | At least one task is assigned or complete |
| `indexed` | All tasks complete, `ai_description` assembled |
| `offline` | File was on NAS but is now missing |
| `error` | One or more tasks abandoned (max retries exceeded) |

The Coordinator owns all transitions into `indexed` and `error`.

---

## Logging

Every worker and the Coordinator log at a consistent level of detail. Log lines always include: timestamp, worker name, task ID or file ID, and what happened.

**Log events:**

| Event | Who logs it |
|-------|------------|
| File discovered (new) | Crawler |
| File removed from NAS | Crawler |
| Task created | Crawler / SceneWorker |
| Task claimed | Worker (at start) |
| Task progress (scene detect: frame N of M) | SceneWorker |
| Task completed | Worker (with timing) |
| Task failed (with reason) | Worker |
| Watchdog fired (scene detect stuck) | SceneWorker |
| File complete (all tasks done) | Coordinator |
| File error (abandoned tasks) | Coordinator |
| Retry scheduled | Coordinator |
| Worker idle (queue empty) | Worker (once, not repeatedly) |

**Log rotation**: Daily rotation, keep 7 days. One log file: `indexer.log`.

---

## File Removal — Full Cascade

When the Crawler detects a file is gone from the NAS:

1. Find the file record by path
2. Delete all `tasks` for this `file_id`
3. Delete all `keyframes` (ON DELETE CASCADE from `files`)
4. Delete all `faces` (ON DELETE CASCADE from `keyframes` and `files`)
5. Remove thumbnail files from disk (`thumbnails/{file_hash}/`)
6. Remove face thumbnail files from disk (`face-thumbnails/` entries for this file)
7. Delete the `files` record — FTS5 trigger removes it from search index automatically
8. Log the removal

Result: deleted files disappear from search results on the next crawl cycle.

---

## Queues: Shared Within Type, Not Across Types

```
Worker type     | Workers | Queue they share
----------------|---------|----------------
SceneWorker     | 3       | scene_detect_q
WhisperWorker   | 1       | transcribe_q
GemmaWorker     | 2       | visual_analysis_q
FaceWorker      | 24      | face_detect_q
```

Each queue is a view into the `tasks` table: `SELECT * FROM tasks WHERE type = ? AND status = 'pending' LIMIT 1`. Workers poll this. When a worker finishes and the queue is empty, it sleeps 2 seconds and polls again.

No worker of type A waits on a worker of type B. WhisperWorker getting well ahead of GemmaWorker is fine — transcripts sit in the DB waiting for visual analysis to catch up. GemmaWorker running out of tasks while scene detection is in progress is fine — it just sleeps.

---

## What Changes from the Current Design

| Current | Redesigned |
|---------|-----------|
| SceneDetectPool → PipelineWorker prep thread | SceneWorker emits tasks directly to visual_analysis_q and face_q |
| Prep thread owns thumbnail extraction + base64 encode | SceneWorker extracts thumbnails as part of scene detect completion |
| Per-GPU task_queue (maxsize=12 in-memory) | SQLite `tasks` table — all workers share, crash-safe |
| Workers exit when queue empties | Workers loop forever, sleeping when idle |
| Face detection is a separate manual CLI command | FaceWorker is a pipeline stage, runs automatically after scene detect |
| No file completion logic — "last keyframe" hack | Task Coordinator detects completion atomically |
| Scene detect has a time-based timeout | Stuck-frame watchdog: kills only if `out_time` stops advancing |
| Watcher runs on 5-min rescan with full NAS walk | Crawler uses directory mtime to skip unchanged dirs; ~10 min cycle |
| No file removal handling | Crawler detects removals and cascades DB deletes |
| File status is a single field on the files table | Tasks table tracks each operation independently; file status derived |

---

## What Does NOT Change

- Hardware assignments: RX 580 × 2 for Gemma, Pro 580X for Whisper, all 3 GPUs for VAAPI scene detect
- `GGML_VK_VISIBLE_DEVICES=1/2` on Gemma services (parallelism fix, Issue #24)
- `--parallel 1` per GPU (VRAM constraint, Issue #11)
- Context `-c 1024` for Gemma (OOM at 2048, Issue #11)
- SQLite + FTS5 for search
- llama.cpp HTTP API for Gemma and Whisper
- 300s audio chunks for Whisper
- 1280px max image dimension for Gemma
- Fan control via `gpu-fans-max.service` (Issue #30)
- BACO disabled via `amdgpu.runpm=0` (Issue #23)
