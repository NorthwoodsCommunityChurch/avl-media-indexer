> ⚠️ ARCHIVE — This document describes the macOS → Windows development journey and is kept for historical reference only. Do NOT use for current operations. Current docs: `SERVERS.md`, `HISTORY.md`

# GPU Pipeline Optimization Plan

## Goal

**Eliminate GPU idle time during media indexing.** Each of the 3 GPUs should run non-stop — when one vision task finishes, the next should start immediately with zero gap. Activity Monitor's GPU History should show solid blue bars, not burst-gap-burst.

---

## Hardware Setup

- **Machine**: Mac Pro (Intel Xeon W-3245, 96GB RAM)
- **GPUs**: 2x AMD Radeon RX 580 (8GB each) + 1x AMD Radeon Pro 580X (8GB)
- **IP**: 10.10.11.157 (DHCP — was 10.10.11.157)
- **SSH**: mediaadmin@10.10.11.157

### Architecture: One Model Per GPU

Each GPU runs its own **independent** Gemma 3 12B Q3_K_S instance. They do NOT share a model — each GPU has the full model loaded in its own 8GB VRAM.

```
GPU0 (RX 580, Slot 5)     GPU1 (RX 580, Slot 3)     GPU2 (Pro 580X, Slot 1)
  Gemma Q3_K_S               Gemma Q3_K_S               Gemma Q3_K_S
  port 8090                  port 8091                  port 8092
  ~6.9 GB VRAM used          ~6.9 GB VRAM used          ~6.9 GB VRAM used
       │                          │                          │
       └──────────────────────────┼──────────────────────────┘
                                  │
                        media-indexer.py (CPU)
                        PipelineWorker per GPU:
                          1 prep thread (CPU: ffmpeg, metadata)
                          1 inference thread (GPU: vision + text gen)
                                  │
                        Search API :8081 (CPU)
                        ChromaDB semantic + FTS5 keyword
```

### Key Files

**On Mac Pro:**
- `~/media-indexer.py` — main indexer + search API
- `~/start-indexer-gpus.sh` — launches 3 GPU servers (`--parallel 1`, staggered)
- `~/stop-llm-server.sh` — stops all llama-server processes
- `~/status-llm-server.sh` — checks all ports
- `~/media-index/index.db` — SQLite database
- `~/media-index/gpu{0,1,2}-server.log` — per-GPU llama-server logs

**On Dev Mac (this project):**
- `media-indexer.py` — source, deployed via SCP
- `media-search-mcp.py` — MCP server for Continue
- `start-indexer-gpus.sh` — GPU startup script
- `start-llm-server.sh`, `stop-llm-server.sh`, `status-llm-server.sh`

---

## What's Been Implemented

### Pipeline Architecture (media-indexer.py)

Split the old monolithic `index_file()` into a pipelined design:

1. **`prepare_media_tasks()`** — CPU-only work: extract keyframes with ffmpeg, probe metadata, prepare task dicts. Runs in the prep thread.
2. **`process_vision_task()`** — GPU work: send image to LLM, write description to DB. Runs in inference threads.
3. **`PipelineWorker` class** — manages the pipeline per GPU:
   - 1 prep thread: pulls files from shared queue, extracts keyframes, feeds per-GPU task queue
   - 2 inference threads: pull from per-GPU task queue, send to llama-server
   - Per-GPU task queue (maxsize=12) acts as prefetch buffer

### GPU Server Config (start-indexer-gpus.sh)

- `--parallel 2` — each GPU server accepts 2 concurrent requests
- Staggered startup to avoid driver crash
- Health check between each GPU launch

### What This Fixed

- **Zero delay between requests from Python side** — logs show same-timestamp transitions between task completion and next task start
- **Prep threads stay ahead** — queue depth stays at 11-12 (near-full), meaning ffmpeg extraction happens while GPU processes
- **Face detection removed from GPU path** — no CPU-bound face detection blocking the inference thread
- **DB writes are minimal** — commit after each description, WAL mode, 30s busy timeout

---

## The Remaining Problem: Gaps Are Inside llama-server

### What the GPU Server Logs Reveal

```
prompt eval time = 124,916ms / 365 tokens (342ms/token)   ← THIS IS THE GAP SOURCE
       eval time =   8,797ms / 112 tokens (79ms/token)    ← text generation (fast)
      total time = 133,713ms / 477 tokens                  ← ~135s total per image
```

**Each vision request spends 125 seconds on prompt eval (vision encoding) and only ~10 seconds on text generation.** The 365 "prompt tokens" are the image embeddings being processed through the language model.

### Per-token breakdown during prompt eval

- 365 image tokens × 342ms per token = **125 seconds**
- During each 342ms token cycle, the GPU does a forward pass (~some ms) then waits for CPU synchronization
- The GPU is NOT idle for 125 seconds straight — it's doing short bursts of work with small gaps between each token batch
- Activity Monitor shows these as the visible burst-gap-burst pattern

### Why the Python pipeline didn't fix it

The pipeline eliminated gaps BETWEEN requests (Python-to-Python overhead). But the gaps are WITHIN each request, inside llama-server's vision processing loop. No amount of Python-side optimization can fix gaps that happen during the server's internal prompt eval.

### Timing comparison

| Phase | Time | GPU Active? |
|-------|------|-------------|
| HTTP round-trip + JSON parse | ~1-2s | No (CPU) |
| Vision encoder (image → embeddings) | ~???s | Partially (CPU preprocessing) |
| Prompt eval (embeddings through LLM) | ~125s | Yes, but with per-token gaps |
| Text generation (~130 tokens) | ~10s | Yes, solid |
| DB write | ~0.1s | No (CPU) |
| **Total per image** | **~135s** | |

### KV Cache behavior

When consecutive requests have the same prompt format (same video keyframes), llama-server's KV cache can skip prompt eval entirely:
- **Cache hit**: prompt eval = 82ms, total = ~11s (text gen only)
- **Cache miss**: prompt eval = 125s, total = ~135s

With `--parallel 1`, same-video keyframes got cache hits (frame_01 and frame_02 reuse frame_00's cache). With `--parallel 2`, cache hits may be disrupted by the second slot's interleaved requests.

---

## `--parallel 2` Attempt: FAILED (Caused Crash)

The idea: if each GPU server has 2 slots, one slot can be doing prompt eval (with GPU gaps) while the other slot fills those gaps with its own work.

### What happened

- Ran with `--parallel 2` on each GPU server + 2 inference threads per GPU
- First completion: **194s** (vs ~135s with `--parallel 1`) — 44% slower
- Shortly after: **Mac crashed** — WindowServer watchdog timeout

### Why it crashed

Each GPU has 8 GB VRAM. Q3_K_S uses ~6.9 GB. With `--parallel 2`, llama-server allocates 2 KV caches (~200 MB each = 400 MB extra), pushing total to ~7.3 GB. Three GPUs all at near-max VRAM overwhelmed the AMD driver. WindowServer (which needs GPU access for display compositing) became unresponsive for 80+ seconds, triggering the watchdog kill → kernel panic.

### Why it was slower even before crashing

- Two concurrent vision requests on the same GPU compete for the same VRAM bandwidth
- The bottleneck is reading model weights from VRAM — two requests double the reads
- KV cache hits are disrupted by interleaved requests from the second slot

### Verdict: `--parallel 2` is not viable on 8 GB GPUs with vision models

Reverted to `--parallel 1` with 1 inference thread per GPU.

---

## MoltenVK Metal Command Serialization (Critical Discovery)

### The Problem

Three independent llama-server processes (one per GPU) do NOT run truly in parallel. MoltenVK translates Vulkan API calls to Metal, and Metal serializes GPU command submission across all devices through a shared system-level pipeline.

### Evidence

Observed during 3-GPU indexing run (Feb 17, 2026):

**CPU usage tells the story:**
```
GPU2 (port 8092): 99.8% CPU  ← actively doing vision encoding
GPU1 (port 8091): 34.7% CPU  ← moderate, partially stalled
GPU0 (port 8090): 10.9% CPU  ← almost completely idle
```

Later snapshot — two GPUs fully stalled:
```
One GPU:  29.9% CPU  ← the only one working
Other two: 0.0% CPU  ← completely idle, waiting
```

**Eval speed degradation under contention:**
- GPU running alone: **12 t/s** (normal)
- GPU0 while GPU2 does vision encoding: **1.73 t/s** (7x slower)
- GPU0 while GPU2 does vision encoding (second observation): **3.04 t/s** (4x slower)

**Audio task timing proves it:**
- `describe_audio_filename` (text-only, no vision): should take ~10s
- Actual time when other GPUs are active: **60 seconds** — 6x slower
- The GPU isn't doing heavy work, it's waiting for Metal command queue access

**Temperature confirms it:**
- User observed GPU temps dropping during "active" indexing
- GPUs are genuinely idle, not just doing light work

### Why This Happens

1. All 3 Vulkan devices go through MoltenVK → Metal translation
2. Metal command submission is serialized at the system level
3. When GPU2 submits heavy vision encoding commands, GPU0 and GPU1 block waiting for the Metal command queue
4. The GPUs take turns rather than running simultaneously
5. Effective throughput with 3 GPUs ≈ 1.0-1.5x a single GPU (not 3x)

### What This Means

- **`--parallel` is NOT the solution** — `--parallel 2` or 3 makes things worse (VRAM + contention)
- **Layer-split mode** (one model across 3 GPUs) would have the same serialization issue
- **3 independent models is the right architecture** — each GPU has its own model, its own VRAM, its own KV cache
- **The problem is scheduling** — we need to avoid sending work to multiple GPUs simultaneously
- The GPUs physically CAN run independently, but the command submission path serializes them

---

## Firm Decisions

1. **NO `--parallel 2` or `--parallel 3`** — proven slower, caused crash. Each server stays `--parallel 1`.
2. **3 independent models, one per GPU** — this is the correct architecture. Each GPU runs its own Gemma Q3_K_S.
3. **The fix is scheduling, not parallelism** — we need a command pipeline that sends work to one GPU at a time, or staggers requests so GPUs take turns without fighting over the Metal command queue.

---

## Current Configuration

- `start-indexer-gpus.sh`: `--parallel 1`, `--ctx-size 2048`
- `media-indexer.py`: PipelineWorker with 1 prep + 1 inference thread per GPU
- Total: 6 threads (3 GPUs × 2 threads)
- **Actual throughput**: ~1-1.5x single GPU due to Metal serialization (not 3x as designed)

---

## Metal Backend Test: FAILED (GPU Timeout, Feb 20 2026)

### Reasoning

MoltenVK serializes GPU commands at the system level because it translates Vulkan → Metal through a shared pipeline. The hypothesis: if we use native Metal directly (no MoltenVK), each GPU gets its own `MTLDevice` and `MTLCommandQueue`. Two separate Metal processes on separate physical GPUs might truly parallelize, bypassing the MoltenVK bottleneck.

The Metal build exists at `~/llama-metal-mgpu/` (from the earlier Metal multi-GPU research). It auto-detects all 3 GPUs as `MTL0`, `MTL1`, `MTL2` with separate Metal devices and compiles shaders with AMD's SIMD width (64).

### What Happened

Started a single Metal llama-server on MTL0 with Gemma 3 12B Q3_K_S + vision encoder:
```
~/llama-metal-mgpu/build-metal/bin/llama-server \
    --device MTL0 -m gemma-3-12b-it-Q3_K_S.gguf --mmproj mmproj-gemma-3-12b-it-f16.gguf \
    -ngl 99 --ctx-size 2048 --parallel 1 --port 8090
```

Server loaded successfully. Sent a vision request (single keyframe thumbnail). After ~72 seconds:

```
error: Caused GPU Timeout Error (00000002:kIOAccelCommandBufferCallbackErrorTimeout)
ggml_metal_synchronize: error: command buffer 0 failed with status 5
```

The Metal command buffer timed out during `clip_image_batch_encode`. macOS's GPU watchdog killed the operation because the CLIP encoding took too long on unoptimized Metal shaders for AMD.

### Why It Failed

The Metal shaders in llama.cpp were designed for Apple Silicon (SIMD width 32, `simdgroup_matrix_multiply`). The Metal multi-GPU research branch fixed some issues (SIMD width 64, early-exit guards, float4 loads), but those fixes were for the **LLM text kernels** — not for the CLIP vision encoder kernels. The CLIP encoder's Metal shaders are still Apple Silicon-optimized and too slow on AMD, causing the operation to exceed macOS's GPU command buffer timeout threshold.

### Comparison

| Backend | CLIP Encoding | Text Gen | Status |
|---------|--------------|----------|--------|
| Vulkan (MoltenVK) | ~33s (works) | ~12 t/s | Production |
| Metal (native) | >72s (GPU timeout crash) | untested | **Dead end** |

### Verdict

**Native Metal is not viable for vision workloads on AMD GPUs.** The CLIP encoder shaders would need AMD-specific optimization (similar to what was done for the Q_K text kernels), and even then it's unclear whether they'd be fast enough to avoid the GPU watchdog timeout. MoltenVK/Vulkan remains the only working option for vision on this hardware.

This also means the MoltenVK serialization problem has **no software workaround on macOS**. Both paths (Vulkan via MoltenVK, native Metal) have fundamental limitations with AMD GPUs.

---

## Next Step: Scheduling Pipeline

The goal: keep 2 independent GPU servers but schedule work so they don't contend on the Metal command queue. Options to explore:

### Option 1: Round-Robin with Backpressure
- Single dispatch thread sends one request at a time
- Wait for GPU to finish before sending next request to a DIFFERENT GPU
- GPUs cycle: GPU0 → GPU1 → GPU0 → ...
- Each GPU works while the others sit idle but their KV caches stay warm

### Option 2: Token-Based Scheduling
- Each GPU gets a "token" when idle
- Dispatcher only sends work to GPUs that hold a token
- When a GPU finishes, it releases its token and the next GPU picks up
- Prevents overlapping Metal commands

### Option 3: Staggered Timing
- Use sleep/delay between dispatching to different GPUs
- If vision encoding takes ~60-130s, stagger starts by ~5-10s
- Allows overlap during the lighter phases (text generation) while avoiding overlap during heavy phases (vision encoding)

---

## Bugs Fixed (This Session + Previous)

1. **`--parallel 2` crash** — 2 KV caches per GPU pushed VRAM to ~7.3 GB on 8 GB cards, crashing AMD driver → WindowServer watchdog kill. Reverted to `--parallel 1`.
2. **Generator deadlock** — `yield` inside `with lock:` held the lock forever across threads
2. **Database locked crashes** — All DB connections use `timeout=30` + `PRAGMA busy_timeout=30000`
3. **ChromaDB blocking workers** — Moved to batch sync after all GPU work completes
4. **Transaction held during vision** — `db.commit()` after each keyframe description
5. **Worker thread death** — Each task wrapped in try/except
6. **Simultaneous GPU loading crash** — Staggered startup with health checks
7. **Face detection blocking GPU** — Removed from inference thread (deferred to post-processing)

---

## Other Items

### IP Address Change
- Mac Pro DHCP changed from 10.10.11.157 → 10.10.11.157
- `media-search-mcp.py` has hardcoded `SEARCH_API = "http://10.10.11.157:8081"` — needs update
- `CLAUDE.md` references 10.10.11.157 throughout — needs update
- Consider setting a static IP or DHCP reservation

### Model Performance Reference
| Model | Quant | Per-GPU VRAM | Prompt Eval (365 tokens) | Text Gen (~130 tokens) |
|-------|-------|-------------|--------------------------|------------------------|
| Gemma 3 12B | Q3_K_S | ~6.9 GB | ~125s (342ms/token) | ~10s (79ms/token) |
| Gemma 3 12B | Q4_K_M | ~8 GB (too big for single GPU) | ~20s (shared 3-GPU) | ~8s |

### Current Indexing State
```sql
-- As of Feb 17, 2026 ~4:40 PM
SELECT status, COUNT(*) FROM files GROUP BY status;
-- indexed: ~2960
-- pending: ~92890
-- error: ~17
-- indexing: ~14
```
