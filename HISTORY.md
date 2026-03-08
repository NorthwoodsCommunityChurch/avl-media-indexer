# History

How this system got to where it is today — macOS → Windows → Ubuntu. Kept for context on key decisions. For current operations, see SERVERS.md and PIPELINE.md.

## Phase 1: macOS (2025)

The Mac Pro originally ran macOS. The plan was to use llama.cpp with Metal (Apple's GPU API) for multi-GPU inference across the 3 AMD Radeon cards.

**The problem**: Metal on macOS does not properly support AMD GPUs in the Mac Pro. Apple's Metal shaders are optimized for Apple Silicon; the AMD GPU compute performance was too slow. A 72-second GPU timeout crash on CLIP encoding made Metal unusable for vision inference (documented in `METAL-MULTI-GPU-CONTEXT.md` and `METAL-RESEARCH.md`).

**Next attempt**: MoltenVK — a translation layer that maps Vulkan API calls to Metal. This worked, but introduced a critical limitation: MoltenVK serializes all Vulkan command submissions at the system level. Even separate processes on separate physical GPUs would serialize, dropping throughput from ~12 t/s to ~1.73 t/s when a second GPU was active.

**Decision**: Move to Windows for native AMD Vulkan drivers.

## Phase 2: Windows / Atlas OS (Late 2025 – Early 2026)

The machine was reformatted to Windows with Atlas OS (a stripped-down Windows build for performance). Native AMD Vulkan drivers meant each GPU got its own independent Vulkan command queue — no MoltenVK translation layer, no cross-GPU serialization.

**What worked**: Text inference ran truly in parallel. With 2× RX 580 each running Gemma 3 12B, aggregate text throughput was ~32-40 t/s.

**New problem discovered — vision inference serialization**: Even on Windows with native drivers, vision inference (the SigLIP encoder in Gemma 3) serialized across all AMD Polaris GPUs. ~67s wall clock for 2 GPUs simultaneously vs ~37s for a single GPU. Root cause: AMD WDDM kernel driver (`amdkmdag.sys`) serializes large sustained compute dispatches system-wide across all Polaris GPUs. Text inference (per-token micro-dispatches) doesn't trigger it; the SigLIP vision encoder (sustained large compute) does.

**Dead ends on Windows** (full list in PRD.md §9):
- HAGS requires RDNA (RX 5000+) — Polaris has no on-chip scheduler
- ROCm/HIP: AMD dropped Polaris support after ROCm 4.x
- `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1`: 5x prompt improvement on some systems, no effect on vision
- AMD Compute Mode registry keys: doubled mining throughput, no effect on vision serialization
- Various llama.cpp split-mode approaches: CUDA-only or crashed on Vulkan

**Windows management pain**: PowerShell processes die when SSH session ends (Issues #3, #12). Required batch files + `schtasks` for reliable remote management. `start-all.py` used crash-and-retry to assign Vulkan devices by name because indices were non-deterministic.

**Decision**: Migrate to Ubuntu. Native AMD Vulkan on Linux (Mesa) performs similarly to Windows, but systemd provides clean process management without the SSH/PowerShell complications.

## Phase 3: Ubuntu Server 24.04.4 LTS (Feb 2026 — Current)

The machine was reformatted again to Ubuntu Server 24.04.4 LTS with a T2-patched kernel (`6.12.74-1-t2-noble`) for Mac Pro hardware support.

**GPU driver**: Mesa Gallium 25.2.8 with `radeonsi` — open-source, works well, no proprietary driver needed.

**Management**: All services run as systemd units. SSH + `systemctl` replaces the Windows `schtasks`/batch file complexity.

**Vision serialization status**: Still present — this is an AMD hardware/driver characteristic, not OS-specific. The WDDM behavior on Windows and the AMD Linux kernel DRM behavior both produce the same outcome. The ~67s dual-GPU vision inference wall clock is the same on Ubuntu as on Windows. This is a hardware limitation, not a configuration problem.

**VAAPI**: All 3 GPUs support hardware video decode, used by `SceneDetectPool` for parallel scene detection — this is a pure CPU/decode task and does parallelize correctly.

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| OS | Ubuntu Server 24.04.4 LTS | Clean systemd management, avoids Windows SSH/PowerShell issues |
| GPU backend | Vulkan (Mesa, native Linux) | Only viable path — Metal doesn't support AMD properly, MoltenVK serializes |
| Database | SQLite + FTS5 | Zero dependencies, WAL mode, FTS5 built-in keyword search |
| Semantic search | FTS5 only (no ChromaDB) | Keyword search across detailed AI descriptions works well; embeddings not needed |
| Parallelism | `--parallel 1` per GPU | `--parallel 2` pushed VRAM to ~7.3 GB on 8 GB cards, crashing AMD driver |
| Model per GPU | One model per GPU (not split) | Sharing a model across GPUs via layer-split serializes anyway |
| Context | 1024 for indexer | 2048 OOMs during vision inference on 8 GB cards (Issue #11) |
| Language | Python 3.x stdlib | No pip dependencies for core indexer |
| Scene detection | ffmpeg scene filter | One keyframe per actual scene change — better than fixed-interval keyframes |
