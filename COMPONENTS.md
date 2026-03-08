# LLM Server — Component Reference

This document lists every named component in the media indexing system, what it does, and what hardware it runs on. Use this as a quick reference when reading logs or debugging.

---

## Overall Architecture: Perpetual Task Pipeline (PTP)

Every file on the NAS gets broken into a set of tasks (transcribe, detect scenes, analyze images, etc.). Workers run continuously, claiming tasks from a shared database and processing them. Nothing stops — workers just wait when there's nothing to do.

**Flow:** File discovered → tasks created → workers claim and process tasks → file marked as indexed when all tasks finish.

---

## Components

### CrawlerWorker ✅ RUNNING

**What it does:** Walks the 4 media folders on the NAS every 5 minutes. When it finds a new file, it registers it in the database and creates the appropriate tasks for it. When a file is deleted from the NAS, it removes the database record.

**Hardware:** CPU only — no GPU needed.

**Log name:** `CrawlerWorker`

**Sub-functions (run during each crawl cycle):**
- `_backfill_tasks` — finds old files that have no tasks yet (legacy files from before the pipeline existed) and creates tasks for them. Processes up to 1,000 files per cycle.
- `_backfill_transcribe` — specifically finds `file_type=NULL` files whose extension is video/audio and creates `transcribe` tasks for up to 500 per cycle. Also stamps `file_type` on those records. Prevents Whisper starvation from legacy files mixed in with large batches of images (see Issue #32).
- `_backfill_scene_detect` — finds videos that were indexed before Phase 2 was added and creates `scene_detect` tasks for them. Processes up to 200 per cycle.
- `_backfill_visual_analysis` — finds keyframes with no `ai_description` and no existing `visual_analysis` task, creates up to 500 tasks per cycle.

---

### PerpetualWhisperWorker ✅ RUNNING

**What it does:** Listens to audio and video files and produces transcripts (text of what was said). It claims `transcribe` tasks from the database, sends the audio to the Whisper AI model, and saves the resulting transcript.

**Hardware:** Radeon Pro 580X GPU (PCIe 07:00.0, port 8092). This is the Apple MPX module card — it has no fans of its own and is cooled by the Mac Pro's chassis fans.

**Log name:** `PerpetualWhisperWorker`

**Key feature — Prefetch:** While Whisper is processing file N, the worker simultaneously extracts audio from file N+1 in the background. By the time Whisper finishes file N, the audio for file N+1 is already ready to go. This reduces the idle gap between jobs from 22 seconds down to under 1 second.

---

### SceneWorker ✅ RUNNING

**What it does:** Analyzes video files to find scene changes (cuts between shots). At each scene change, it extracts a thumbnail image (keyframe) that represents that scene. These keyframes are what future AI workers will analyze visually.

Three instances run at the same time, one per GPU, so three videos can be processed simultaneously.

**Hardware:** All 3 GPUs provide VAAPI decode acceleration (hardware-accelerated video decoding). VAAPI is a separate hardware block on each GPU — it handles video decoding without touching the AI compute cores, so it does not interfere with Whisper running on the same card.
- SceneWorker 1 → /dev/dri/renderD128 (Pro 580X)
- SceneWorker 2 → /dev/dri/renderD129 (RX 580 #1)
- SceneWorker 3 → /dev/dri/renderD130 (RX 580 #2)

**Log name:** `SceneWorker`

**Built-in reliability features:**
- **Stuck-frame watchdog** — watches ffmpeg's progress output. If the video position (`out_time`) stops advancing for 30 seconds, the watchdog kills the process and marks the file for retry. This catches files that hang silently instead of crashing.
- **VAAPI → CPU fallback** — if hardware-accelerated decoding stalls, the worker retries the same file using software (CPU) decoding.
- **Fixed-interval fallback** — if CPU decoding also stalls (watchdog fires a second time), the worker gives up on scene detection and instead pulls evenly-spaced frames at fixed time intervals. The file still gets keyframes, just not at actual scene cuts.

---

### TaskCoordinator ✅ RUNNING

**What it does:** Runs quietly in the background, watching the tasks table every 5 seconds. It handles two jobs:

1. **Completion detection** — when every task for a file is marked complete, it flips the file's status to `indexed` so it becomes searchable.
2. **Stuck task recovery** — if a task has been in `assigned` state for more than 30 minutes (meaning the worker that claimed it crashed), the coordinator resets it back to `pending` so another worker can pick it up.

**Hardware:** CPU only — no GPU needed.

**Log name:** `TaskCoordinator`

---

### GemmaWorker ✅ Running (Phase 3)

**What it does:** Looks at each keyframe thumbnail extracted by SceneWorker and writes a plain-English description of what's in the image. Also handles image files directly. Once all keyframes for a video are described, calls `_assemble_file_description()` to write the file-level `ai_description` (concatenated with ` | ` separators) and `tags` (derived from the file path).

**Hardware:** The two RX 580 GPUs — one instance per card.
- GemmaWorker [8090] → RX 580 #1, port 8090
- GemmaWorker [8091] → RX 580 #2, port 8091

**Backfill:** `CrawlerWorker._backfill_visual_analysis()` creates up to 500 tasks per crawl cycle for keyframes with no `ai_description` and no existing task.

**Log name:** `GemmaWorker`

---

### FaceWorker ✅ RUNNING (Phase 4)

**What it does:** Claims `face_detect` tasks from the queue, runs dlib-based face detection on each keyframe thumbnail, and stores face embeddings + crop thumbnails in the database. Clustering and naming are separate on-demand operations.

**Hardware:** CPU only — uses the `face_recognition` library (dlib), which does not require a GPU.

**Log name:** `FaceWorker`

---

## Task Types

| Task | Phase | Status | Description |
|------|-------|--------|-------------|
| `transcribe` | 1 | ✅ Running | Transcribe audio/video to text using Whisper |
| `scene_detect` | 2 | ✅ Running | Find scene cuts in video, extract keyframe thumbnails |
| `visual_analysis` | 3 | ✅ Running | Describe a keyframe image using Gemma vision AI |
| `face_detect` | 4 | ✅ Running | Detect and identify faces in a keyframe |

## Task States

```
pending → assigned → complete
                   ↘ failed
                   ↘ abandoned   (reassigned after 30+ min by TaskCoordinator)
```

---

## Hardware Reference

| GPU | PCIe Address | VAAPI Device | AI Port | Role |
|-----|-------------|-------------|---------|------|
| Radeon Pro 580X | 07:00.0 | /dev/dri/renderD128 | 8092 | Whisper (transcription) |
| RX 580 #1 | 0c:00.0 | /dev/dri/renderD129 | 8090 | Gemma0 (visual analysis) |
| RX 580 #2 | 0f:00.0 | /dev/dri/renderD130 | 8091 | Gemma1 (visual analysis) |

Note: The Pro 580X is in an Apple MPX module and has no fans. It is cooled entirely by the Mac Pro's 4 chassis fans. Do not investigate "missing fan" on that card — it is normal.

---

## Pipeline Diagram

What is currently running vs. what is planned for future phases.

```
NAS (/mnt/vault/)
      │
      ▼
 CrawlerWorker ✅          ← polls every 5 min, walks vault folders
      │
      │  VIDEO  → transcribe task + scene_detect task
      │  AUDIO  → transcribe task
      │  IMAGE  → visual_analysis task + face_detect task (Phase 4)
      │
      ├──────────────────────────────────┐
      ▼                                  ▼
[ transcribe queue ] ✅          [ scene_detect queue ] ✅
      │                                  │
 PerpetualWhisperWorker          SceneWorker ×3
   Pro 580X  port 8092           renderD128/129/130
   (with prefetch)               VAAPI decode hardware
      │                                  │
      ▼                          extracts keyframe thumbnails
   SQLite: transcript                    │
                                         ▼
                              [ visual_analysis queue ] ✅
                              [ face_detect queue ] ✅
                                    │               │
                              GemmaWorker ×2 ✅  FaceWorker ✅
                              RX 580s  8090/8091   CPU (dlib)
                              ~14s/image, ~8/min

          ┌──────────────────────────────┐
          │      TaskCoordinator ✅       │  ← always running
          │  Watches task table every 5s │
          │  All tasks done → file       │
          │  marked indexed + searchable │
          │  Stuck tasks → reset to      │
          │  pending after 30 min        │
          └──────────────────────────────┘
```

---

## Phase Summary

| Phase | What it adds | Status |
|-------|-------------|--------|
| Phase 1 — Transcription | CrawlerWorker, PerpetualWhisperWorker, TaskCoordinator | ✅ Running |
| Phase 2 — Scene Detection | SceneWorker ×3 | ✅ Running |
| Phase 3 — Visual Analysis | GemmaWorker ×2 | ✅ Running |
| Phase 4 — Face Detection | FaceWorker | ✅ Running |
