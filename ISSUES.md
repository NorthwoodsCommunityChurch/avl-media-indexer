# Issues Tracker

Track every issue encountered so we can spot when we're going in circles.

---

## FIXED

### 1. MoltenVK serializes multi-GPU on macOS
**Symptom**: GPU0 drops from 12 t/s to 1.73 t/s when GPU1 is active.
**Root cause**: MoltenVK translates Vulkan→Metal and serializes all GPU commands at the system level, even across separate processes on separate physical GPUs.
**Fix**: Moved to Windows/Atlas OS with native AMD Vulkan drivers. Each GPU gets its own independent command queue.
**Status**: FIXED

### 2. Vulkan device enumeration is non-deterministic
**Symptom**: Gemma loaded on Pro 580X instead of RX 580 → OOM crash (`ErrorOutOfDeviceMemory` allocating 854MB for vision encoder). GPU assignments kept being wrong after reboots.
**Root cause**: Vulkan device indices (Vulkan0, Vulkan1, Vulkan2) can change between reboots and even between process launches. Hardcoded batch files break randomly.
**Fix**: `start-servers.ps1` uses crash-and-retry: tries each Vulkan index (0, 1, 2), reads the log for "using device VulkanX (GPU Name)", kills if wrong GPU, retries next index. Uses `curl.exe` for health checks (not Invoke-RestMethod). Launches via `schtasks` for process survival. Requires "RX 580" in GPU name for Gemma, rejects Pro 580X.
**Status**: FIXED

### 3. PowerShell processes die when SSH disconnects
**Symptom**: Servers started via `Start-Process` in PowerShell SSH sessions die when the SSH connection closes.
**Root cause**: Windows kills child processes of the SSH session when it ends.
**Fix**: Use Windows scheduled tasks (`schtasks /create` + `schtasks /run`) to launch batch files. Scheduled task processes survive SSH disconnection.
**Status**: FIXED

### 4. PowerShell health checks fail silently over SSH
**Symptom**: `Invoke-RestMethod` returned nothing, health checks appeared to succeed when servers were actually down. Also caused start-servers.ps1 to time out even when servers were healthy.
**Root cause**: PowerShell over SSH doesn't properly handle HTTP — `Invoke-RestMethod` silently fails, returns nothing, and exceptions are swallowed.
**Fix**: Use `curl.exe` (Windows native binary) for all health checks in start-servers.ps1. Match `"status"` in the JSON response with regex.
**Status**: FIXED

### 5. PowerShell $ErrorActionPreference treats stderr as error
**Symptom**: PowerShell script aborts when llama-server writes to stderr.
**Root cause**: `$ErrorActionPreference = "Stop"` makes PowerShell treat any stderr output as a terminating error. llama-server writes informational messages to stderr.
**Fix**: Removed `ErrorActionPreference`, used `ForEach-Object { $_.ToString() }` to safely capture stderr.
**Status**: FIXED

### 6. Vision inference is slow on AMD Polaris GPUs
**Symptom**: ~35s per image single-GPU, ~64s per image when both GPUs active (nearly serial, not parallel).
**Root cause**: SigLIP vision encoder has fixed overhead on AMD Polaris GPUs. Image resolution has NO effect (10x10 = 320px = 640px = 1280px). Token count is always ~256 regardless of `--image-max-tokens`. Dual-GPU slowdown is NOT PCIe bandwidth — see Issue #16.
**Fix**: No software fix possible on Windows/WDDM — hardware/driver limit on that stack.
**Status**: FIXED on Windows (won't fix — hardware/driver limit on Windows/WDDM). Not re-tested on Linux/Mesa — behaviour may differ. See Issue #24.

### 7. Whisper transcription blocks Gemma GPU pipeline
**Symptom**: GPUs take turns instead of running simultaneously. One GPU idle while the other works.
**Root cause**: `prepare_media_tasks()` called `transcribe_audio()` synchronously in the prep thread. While Whisper transcribes (can take minutes), no new keyframes are prepared for either Gemma GPU.
**Fix**: Created `WhisperWorker` class — dedicated thread with its own queue. Prep threads feed `(fid, filepath)` to the whisper queue without blocking. WhisperWorker extracts audio and transcribes independently, writing transcripts directly to DB. Gemma GPUs are never starved.
**Status**: FIXED — deployed and verified. WhisperWorker runs independently and does not block Gemma GPUs.

### 8. PowerShell $HOME is read-only
**Symptom**: `Cannot overwrite variable HOME because it is read-only or constant` warning in start-servers.ps1.
**Root cause**: PowerShell has a built-in `$HOME` automatic variable that can't be overwritten.
**Fix**: Renamed to `$HOMEDIR` in start-servers.ps1 (later replaced with `$LOGDIR`).
**Status**: FIXED

### 9. NAS share inaccessible from SSH session
**Symptom**: `[WinError 5] Access is denied: '\\10.10.11.185\Vault\...'` when the indexer tries to read files.
**Root cause**: The SMB share to the NAS (10.10.11.185) requires credentials. SSH sessions use Network logon (type 3) which cannot access SMB shares. `net use`, `cmdkey`, and `New-SmbMapping` all fail from SSH. Only `New-PSDrive -Credential` works in PowerShell but UNC paths still fail from Python/cmd.exe.
**Fix**: Embedded `net use \\10.10.11.185\Vault PASSWORD /user:mediaadmin` in `run-indexer.bat`, which runs as a scheduled task. Scheduled tasks use a different logon context where `net use` succeeds.
**Status**: FIXED

### 10. PowerShell $PID is read-only
**Symptom**: `Cannot overwrite variable PID because it is read-only or constant` in Kill-ServerOnPort function.
**Root cause**: `$PID` is a PowerShell automatic variable (current process ID). Using it as a loop variable fails.
**Fix**: Renamed loop variable from `$pid` to `$procId`.
**Status**: FIXED

### 11. Vision OOM with context=2048 on RX 580
**Symptom**: `ErrorOutOfDeviceMemory` allocating 512 bytes during image decoding on Gemma Q3_K_S. Server crashes mid-inference after successfully encoding the image (57s).
**Root cause**: Model (5199MB) + KV cache at ctx 2048 (608MB) + vision encoder (854MB) + compute buffer (519MB) = ~7180MB. With only ~7367MB free, fragmentation tips it over.
**Fix**: Reduced context from `-c 2048` to `-c 1024` in start-servers.ps1. KV cache drops to ~304MB, saving ~304MB of headroom. Indexer prompts only use ~600-800 tokens so 1024 is sufficient.
**Status**: FIXED

### 12. start-servers.ps1 process launch doesn't survive SSH disconnect
**Symptom**: Servers started via `[System.Diagnostics.Process]::Start()` in PowerShell SSH session die when SSH closes (regression of Issue #3).
**Root cause**: Even with `UseShellExecute=$true`, processes launched directly from PowerShell are children of the SSH session.
**Fix**: Replaced direct process launch with `schtasks /create` + `schtasks /run` in Try-StartServer. Each server gets a named scheduled task (Gemma-GPU0, Gemma-GPU1, Whisper-GPU2) that persists and auto-starts on boot.
**Status**: FIXED

### 13. Log file I/O buffering causes GPU name check race condition
**Symptom**: start-servers.ps1 says "UP (GPU unknown)" and accepts a server on the wrong GPU. GPU name check passes because the log file hasn't been flushed yet when health check succeeds.
**Root cause**: Windows cmd.exe redirect (`> log.txt 2>&1`) buffers stderr output. The "using device VulkanX (GPU Name)" line is in the buffer but not yet flushed to disk when the script reads the log.
**Fix**: After health check succeeds, wait in a retry loop (up to 5s, 500ms intervals) for the GPU name to appear in the log. If GPU name never appears, reject the server as unsafe.
**Status**: FIXED

### 14. Smart keyframe extraction (variable keyframes per clip)
**Symptom**: Previously extracted exactly 3 keyframes (10%, 50%, 90%) regardless of video length or content.
**Fix**: Added scene detection via histogram comparison. Extracts low-res grayscale frames at 2 fps, computes 64-bin histograms with NumPy, compares consecutive frames using normalized correlation. A drop below 0.85 = scene change. One keyframe extracted per scene (at 30% into each scene to avoid transitions). No artificial cap — a 2-hour worship service with 200 cuts gets 200 keyframes. Short videos (<5s) get a single mid-frame. Constants: `SCENE_CHANGE_THRESHOLD = 0.85`, `SCENE_DETECT_FPS = 2`.
**Status**: FIXED — deployed Feb 2026. Already-indexed videos keep old keyframes; run `reindex` to re-process with scene detection.

---

## OPEN

### 21. Network interface down after shutdown + Wake-on-LAN (Ubuntu)
**Symptom**: Machine is physically powered on (rack room temperature sensor confirms GPUs generating heat) but completely unreachable on the network. No response to ping, SSH, or any service port on 10.10.11.157 or any other IP in the 10.10.11.x subnet. Full subnet scan (SSH on all 254 IPs) found only the known macOS machines — no Ubuntu host anywhere.
**Context**: Machine was migrated from Windows/Atlas OS to Ubuntu Server 24.04.4 LTS. SSH and GPU inference were working. During a session investigating a missing GPU, the machine was shut down and Wake-on-LAN was used to power it back on. The machine powered on (confirmed by physical temp sensor in rack room) but the network interface never came up.
**What was tried**:
- Ping sweep of entire 10.10.11.1-254 subnet — no response from Mac Pro
- SSH to every responding IP — all are known macOS machines, none is the Mac Pro
- Port scan for llama.cpp (8090), search API (8081), Dashboard Agent (49990) — nothing
- Wake-on-LAN sent to e4:50:eb:ba:ce:6c — no change
- Earlier in the session, SSH to .157 returned "Connection refused" (meaning the machine was briefly on the network) before going completely silent
**Root cause**: WoL boot did not fully initialize the network interface. The machine powered on (GPUs active, generating heat) but had no network connectivity.
**Fix**: Full power cycle (physical power off + power on) brought the machine back online at 10.10.11.157. WoL alone was insufficient.
**Lesson**: WoL does not work on this machine. Always use a full power cycle (physical power off + power on). Do not attempt WoL.
**Status**: FIXED — full power cycle resolved it

### 22. VaultSearch app shows false "Processing" when servers are unreachable
**Symptom**: VaultSearch Status tab shows all 3 GPU servers as green "Processing" even though the machine is completely offline. Indexer shows "0 / 0 files indexed" and Scanner shows "Idle."
**Root cause**: The `StatusService.swift` `checkServer()` method (line 202) treats URL timeouts as "online + processing":
```swift
} catch let error as URLError where error.code == .timedOut {
    return (true, true)  // Assumes server is busy with vision encoding
}
```
This was designed for when llama.cpp blocks all HTTP during CLIP vision encoding (~32s). But it can't distinguish between "server is busy" and "nothing is listening and the TCP connection just hangs." When the machine is off the network entirely, some network configurations cause connections to hang (timeout) rather than get refused, triggering the false positive.
**The Scanner "Idle" and "0 / 0 files" are default values** — `IndexerCounts` and `ScannerStatus` initialize to zeros/idle, and `fetchIndexer()` returns `nil` on failure, leaving defaults in place.
**Fix needed**: Add a connectivity check — e.g., if the indexer `/status` endpoint fails, mark all GPU servers as offline regardless of timeout behavior. Or add a separate reachability check (ping or TCP connect to a known port) before interpreting timeouts as "busy."
**Fix (Feb 26 2026)**: Port 8081 (`/status`) used as connectivity gate — if it fails, all GPU servers are marked offline regardless of timeout behavior. 5-second timeout added so the app surfaces "Server offline" quickly instead of waiting for a long hang. (Ben)
**Status**: FIXED — Feb 26 2026. StatusService.swift uses port 8081 as connectivity gate: if fetchIndexer() fails for any reason, all GPU servers are immediately set offline. 5-second timeout replaces URLSession.shared default.

### 23. GPU drops out during operation — recurring issue

**Symptom**: One of the 3 AMD GPUs becomes unresponsive while the machine is running. The GPU's llama-server health endpoint still returns `{"status":"ok"}` but all inference requests time out — even simple text-only requests with no image. The service appears running in systemd but does not process any work.

**Occurrences:**
1. **First occurrence (prior session)**: Which GPU was missing and trigger were not documented. Power cycle resolved it.
2. **Second occurrence (Feb 24 2026, session 1)**: GPU1 (RX 580 at 0000:0f:00.0, gemma1 service, port 8091) became unresponsive during active indexing. Discovered during parallelism testing — GPU0 completed text-only inference in 1.3s while GPU1 timed out (300s). GPU0+GPU1 simultaneous test: GPU0 ran fine, GPU1 timed out. The GPU appeared healthy to the health endpoint but did not execute inference. Machine was shut down and physically power cycled to recover.
3. **Third occurrence (Feb 24 2026, session 2)**: GPU1 dropped out again approximately 30 minutes after the second power cycle, while the indexer was processing `Murdock Story Review.mp4` (49 keyframes). Frame_13 inference on port 8091 timed out after 300s. The sysfs path `/sys/bus/pci/devices/0000:0f:00.0/gpu_busy_percent` returned `Operation not permitted`. Shortly after, the entire machine became unresponsive — SSH connections timed out. Physical power cycle restored all three GPUs. After reboot: all sysfs paths readable again, both RX 580s at full utilization.

**What the GPU looks like when dead:**
- `curl http://localhost:8091/health` → `{"status":"ok"}` (misleading — server process is alive, GPU is not)
- `curl http://localhost:8091/v1/chat/completions` with ANY request → hangs until timeout
- `journalctl -u gemma1` shows the last `done request` entry was minutes ago with no new activity
- **`/sys/bus/pci/devices/0000:0f:00.0/gpu_busy_percent` returns `Operation not permitted`** — this is the reliable early warning sign. This file is world-readable when the GPU is healthy. When it returns EPERM, the GPU/driver is in a broken state. Does not require root or any inference request to check.
- The slowdown is not gradual — it happens suddenly and does not self-recover
- When severe, the machine itself becomes unresponsive (SSH freezes) and requires a physical power cycle

**Likely root cause identified (Feb 24 2026):**
BACO runtime power management. Both RX 580s had `power/control = auto` — the AMD driver puts them to sleep between inference requests and wakes them (BACO transition) for each new one. Each wake re-initializes the GPU GART. dmesg showed **107 GART re-enables in ~4 hours** of operation. During the 49-frame Murdock job (back-to-back inference with no idle gaps), BACO transitions were firing continuously under full Vulkan compute load. Polaris-era (RX 580) BACO implementation has known instability under sustained Vulkan compute — if a wake transition fails mid-way, the GPU hangs silently. The Pro 580X was unaffected because its dmesg shows `Runtime PM not available` — it never enters BACO.

`dmesg` shows no crash messages because a failed BACO transition leaves the GPU silently hung — the driver never logs it as an error.

**What we still do NOT know:**
- Whether restarting the gemma service (without power cycle) can recover it — worth trying first next time
- Why it hit GPU1 twice rather than GPU0 — may just be timing/luck

**Diagnostics if it happens again (before power cycling):**
```bash
# Check BACO/power state
cat /sys/bus/pci/devices/0000:0f:00.0/power/control        # should be "on" with fix applied
cat /sys/bus/pci/devices/0000:0f:00.0/power/runtime_status # "suspended" = BACO bug struck despite fix
# Check if GPU still visible on PCIe
lspci | grep -i radeon
# Try service restart before full power cycle
sudo systemctl restart gemma1 && sleep 5 && curl -s http://localhost:8091/health
```

**Fix (Feb 24 2026):**
1. **Immediate**: `echo on | sudo tee /sys/bus/pci/devices/0000:0c:00.0/power/control` and same for `0f:00.0` — locks GPUs in active state for current boot.
2. **Persistent (kernel param)**: Added `amdgpu.runpm=0` to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, ran `update-grub`. Takes effect on next boot.
3. **Belt-and-suspenders (udev)**: Created `/etc/udev/rules.d/99-amdgpu-runpm.rules` — sets `power/control=on` for all AMD VGA devices on `add` event, catches any case where kernel param isn't respected.

**Recovery**: Full physical power cycle (power off + power on). Do NOT use `sudo reboot` alone — Mac Pro hardware may not fully reinitialize PCIe devices on soft reboot (see Issue #21 pattern).

**Status**: ROOT CAUSE IDENTIFIED — BACO runtime PM. Fix deployed. Monitor for recurrence after next reboot to confirm `power/control` stays `on` throughout operation.

### 20. RED camera (.R3D) files not indexed — no keyframe extraction support
**Symptom**: R3D clips in the Lobby folder show "Video file: filename.R3D" as their AI description instead of a real vision description. Vision never ran on them.
**Root cause**: ffmpeg cannot extract keyframes from proprietary RED .R3D files without the RED SDK. The `extract_keyframes()` function calls ffmpeg directly; ffmpeg returns an error on .R3D input, so no keyframe images are produced and vision inference is never triggered. The file is still recorded in the DB as "indexed" with a placeholder description.
**Fix needed**: Detect .R3D files (and possibly .BRAW) before keyframe extraction. Options: (a) use the RED SDK or a transcoded proxy if one exists alongside the .R3D, (b) use ffprobe to grab a thumbnail via a different demuxer flag, (c) skip vision on .R3D and mark as "no vision support" so the description slot is left empty rather than filled with a placeholder.
**Impact**: All RED camera footage (significant portion of Projects Vault) will be unsearchable by content — only filename/path tags will match.
**Status**: FIXED — see Issue #31. REDline (RED SDK) and braw-frame (Blackmagic RAW SDK) installed Feb 25 2026. Both formats now get real keyframe thumbnails via native SDKs.

### 15. Dual-GPU vision inference is ~80s per frame (slower than expected)
**Symptom**: Each GPU takes ~64s per frame with both GPUs active (was ~80s before, may vary). Single-GPU: ~35s. Wall clock for both together ≈ sum of individual times, not max.
**Root cause**: AMD Vulkan driver serializes the SigLIP vision encoder across GPUs. See Issue #16 for full investigation. NOT PCIe bandwidth — PCIe topology confirmed each GPU has its own dedicated CPU root port.
**Fix**: `GGML_VK_VISIBLE_DEVICES=1` on gemma0 and `=2` on gemma1 — each service sees only its own GPU, so the mmproj vision encoder can no longer cross over to the other card. Deployed via Issue #24. Confirmed Feb 26 2026: overlapping log timestamps prove both GPUs run concurrently with no serialization. gemma0 runs ~17s/frame (63 t/s), gemma1 runs ~27s/frame (40 t/s). The gemma1 speed discrepancy vs gemma0 is a separate concern tracked under Issue #33 (thermal throttling).
**Status**: FIXED

### 16. AMD Vulkan driver serializes vision encoder across ALL 3 GPUs
**Symptom**: All three GPUs — both RX 580s running Gemma vision AND the Pro 580X running Whisper — take turns instead of running simultaneously. When any GPU is doing heavy inference, the others pause. Text-only inference is perfectly parallel.

> **Correction (Feb 2026):** Earlier entries stated "Whisper on the Pro 580X runs independently and is unaffected." This was wrong. User observation confirmed that Whisper serializes with Gemma vision inference just like the two Gemma GPUs serialize with each other. The earlier benchmark row showing "Whisper unaffected at 11.59s" reflected a short test clip that may have completed before the serialization bottleneck became visible. The full 3-way scope can be confirmed with `python3 gpu-monitor.py --benchmark`.

**Root cause (identified on Windows/WDDM — not re-tested on Linux)**: The AMD WDDM kernel driver (`amdkmdag.sys`) serializes large sustained compute dispatches across **all Polaris GPUs in the system**, regardless of model or role. SigLIP vision encoder triggers this because it's a massive F32 workload running as continuous large dispatches. Text generation (small per-token VRAM reads) doesn't trigger it. The serialization is system-wide — one GPU doing heavy inference stalls all others.

> **Note**: This root cause was identified while the machine ran Windows/Atlas OS. The machine now runs Ubuntu Server with Mesa radeonsi drivers — a completely different driver stack. Serialization has not been re-benchmarked on Linux. See Issue #24.

**Original controlled testing (Gemma vs Gemma — still valid):**

| Test | Time |
|------|------|
| GPU A vision alone | 34.69s |
| GPU B vision alone | 37.29s |
| Both vision simultaneously (wall) | **63.94s** (serial would be 71.98s) |
| GPU A text alone | 8.49s |
| GPU B text alone | 9.19s |
| Both text simultaneously (wall) | **8.63s** (perfectly parallel) |
| Both vision + Whisper simultaneously | **66.95s** ~~(Whisper unaffected at 11.59s)~~ ← incorrect — see note above |

**What was ruled out:**
- **PCIe bandwidth contention**: Each GPU has its own dedicated CPU root port (Pro 580X → Bus 7, RX 580 → Bus 12 via switch, RX 580 → Bus 15 direct). No shared bandwidth.
- **WDDM scheduler**: Windows gives each independent GPU adapter its own scheduler instance.
- **ggml-vulkan mutex**: Separate processes have separate per-device mutexes.
- **MoltenVK (Issue #1)**: Already fixed by moving to Windows. This is a different issue.

**What it IS**: The AMD WDDM kernel mode driver (`amdkmdag.sys`) serializes large sustained compute dispatches across multiple Polaris GPUs. The SigLIP vision encoder triggers this because it's a massive F32 workload (519MB compute buffer + 854MB encoder) that runs as continuous large dispatches. Text generation (small per-token VRAM reads) doesn't trigger the serialization because each token is a tiny dispatch.

**Deep dive — ggml-vulkan internals:**
- There is NO vision-specific dispatch path in ggml-vulkan. Vision and text inference use the exact same Vulkan compute pipeline code. The difference is workload size, not code path.
- `Vulkan_Host` buffer (pinned host memory for CPU↔GPU transfers) is per-process, NOT shared between processes. This is not the bottleneck.
- The serialization happens at the WDDM kernel driver level, not in ggml-vulkan or in user-space Vulkan. Each GPU adapter gets its own WDDM scheduler instance, but AMD's kernel driver appears to have a global lock for large compute dispatches on Polaris.
- The `--no-mmproj-offload` flag exists in llama.cpp to run the vision encoder on CPU instead of GPU. This avoids the serialization but is slower (~209s vs ~35s per image on this hardware). Not practical.
- `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM` environment variable could change the memory allocation pattern and might affect serialization behavior. Untested.
- Staggering vision requests by 1-2 seconds (so both GPUs aren't in the encoder phase simultaneously) could provide partial overlap during the text generation phase.

**Alternative models investigated:**
- **Gemma 3 4B Q4_K_M** (~4.2 GB total) — Strong recommendation. Same SigLIP encoder so same ~35s per image, but much smaller language model means more VRAM headroom and faster text generation. Better quantization quality than current 12B Q3_K_S (Q4 on a 4B model retains more quality than Q3 on a 12B model).
- **Qwen2.5-VL** — Larger ViT encoder, estimated ~87s per image on this hardware. WORSE than Gemma.
- **SmolVLM2 2.2B** — Ultra-lightweight but significantly weaker language model for description tasks.
- **Key finding**: ALL vision-language models in llama.cpp use the same ggml-vulkan dispatch path. Changing the model does NOT fix the serialization — any model with a vision encoder will serialize the same way.

**Alternative inference engines investigated:**
- **ROCm/HIP backend**: Dead end. AMD dropped Polaris (gfx803) GPU support after ROCm 4.x. Current ROCm requires RDNA or newer.
- **Ollama / KoboldCpp**: Both use the same ggml-vulkan backend internally. Same serialization problem.
- **DirectML (DirectX 12)**: The ONLY alternative compute API that uses a completely different driver path. ONNX Runtime + DirectML could potentially bypass the Vulkan serialization since it goes through DirectX 12 instead of Vulkan. However, requires ONNX model format (not GGUF), and vision-language model support in ONNX Runtime is limited.
- **AMD has deprecated Polaris**: Moved to extended/maintenance support. No future Vulkan driver improvements will come for these GPUs. The serialization will never be fixed upstream.

**Impact**: Second RX 580 provides zero throughput improvement for vision inference. Effective speed is ~35s/frame with 1 GPU or ~32s/frame with 2 GPUs (slight overlap during text generation phase). Text-only workloads scale perfectly.

**Potential workarounds:**
1. **Stagger vision requests** — Offset GPU work by 1-2s so encoder phases don't overlap. Would get partial parallelism during text generation.
2. ~~**Enable HAGS**~~ — **Dead end.** RX 580 (Polaris) has no on-chip scheduler hardware. HAGS requires RDNA (RX 5000+). AMD only supports HAGS on RX 7000 series.
3. ~~**`GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1`**~~ — **No effect on serialization.** Forces CPU↔GPU transfers through staging buffers. Tested: dual-GPU vision still ~67s wall clock (1.78x ratio), identical to baseline. Left enabled in batch files since it may help prompt processing on some workloads, but does not fix the cross-GPU serialization.
4. ~~**AMD Compute Mode registry keys**~~ — **No effect on serialization.** Applied `EnableCrossFireAutoLink=0`, `KMD_EnableInternalLargePage=2`, `EnableUlps=0` to all 3 AMD GPUs. Rebooted. Benchmark: dual-GPU vision ~67s wall clock (1.78x ratio), unchanged from baseline ~64s. Mining throughput gains don't apply — mining uses embarrassingly parallel workloads (each GPU independent), while SigLIP vision encoder serialization is a driver-level lock on large compute dispatches.
5. **Switch to Gemma 3 4B** — Doesn't fix serialization but frees VRAM and improves text generation speed. Better quality-per-bit than current 12B Q3_K_S.
6. ~~**Test DirectML via ONNX Runtime**~~ — **Dead end.** Windows-only (DirectX 12); machine now runs Ubuntu. Also requires ONNX models (not GGUF); DirectML in maintenance mode; limited VLM support in ONNX Runtime.
7. **Dedicated GPU roles** — Use one RX 580 for vision, other for text-only workloads (if we ever need text-only inference).
8. ~~**ik_llama.cpp split mode graph**~~ — **Dead end.** Achieves 3-4x on CUDA but crashes on Vulkan.
9. ~~**llama.cpp `--split-mode tensor` (PR #19378)**~~ — **Dead end.** Merged to mainline but Vulkan crashes with driver errors. CUDA-only for now.

**Fix**: Resolved via Issue #24 — `GGML_VK_VISIBLE_DEVICES=1/2` isolates each gemma process to its own GPU. CLIP vision encoder confirmed running on correct RX 580 (startup logs show `CLIP using Vulkan0 backend` on each card). Verified Feb 26 2026.
**Status**: FIXED

### 17. WhisperWorker audio extraction fails on DJI drone files
**Symptom**: Every DJI .MOV file from the NAS fails audio extraction instantly (~35ms). Log shows `WhisperWorker: audio extraction failed for DJI_XXXX.MOV` with no details.
**Root cause**: DJI drone files have no audio track. The `extract_audio_for_transcription()` function was calling ffmpeg directly without checking first, causing a 120s timeout per file (112 drone files × 120s = 3.5 hours wasted per run). ffmpeg stderr was also never logged so the error was invisible.
**Fix**: Added `has_audio_stream()` function that runs `ffprobe -select_streams a:0` before attempting extraction (fast, ~15s timeout). If no audio stream is found, skips transcription immediately with an info log. Also added ffmpeg stderr logging when extraction fails, so future errors are visible.
**Status**: FIXED — deployed Feb 2026. DJI files are skipped instantly. WhisperWorker will handle real audio content from vault folders.

### 18. Old scheduled tasks override correct server config on boot
**Symptom**: After reboot, servers start with wrong config — no `--mmproj` (vision returns 500), hardcoded Vulkan indices (wrong GPU), wrong context (`-c 2048` causes OOM).
**Root cause**: Old scheduled tasks (GemmaGPU0, GemmaGPU1, WhisperGPU2) still existed and auto-started on boot. They ran batch files without vision encoder, with hardcoded device indices, and with 2048 context. The correct tasks (Gemma-GPU0, etc.) existed but didn't auto-start.
**Fix**: Created `start-all.py` which: (1) kills stale processes, (2) deletes old scheduled tasks and batch files, (3) for each server tries Vulkan indices 0/1/2 via crash-and-retry (reads server log for actual GPU name, kills if wrong, retries next index), (4) writes batch files with correct Vulkan indices + `--mmproj` + `-c 1024` + `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1`, (5) registered as `Start-All-Servers` logon task via `start-all-wrapper.bat`.
**Status**: FIXED

### 19. WhisperWorker starved between prep batches
**Symptom**: Whisper (Pro 580X) sits idle for 35-64s between bursts of work. Processes in clumps instead of continuously.
**Root cause**: WhisperWorker only received files after a prep thread finished processing them (one at a time). Flow was: prep → queue whisper → next prep. With prep taking 35-64s per file (vision inference), Whisper got work in bursts separated by long gaps.
**Fix**: Pre-queue ALL video/audio files into `whisper_queue` at the start of `process_pending()`, before pipeline workers begin. WhisperWorker now gets its entire workload upfront and processes files at its own pace, completely independent of Gemma GPU pipeline timing.
**Status**: FIXED

---

### 24. mmproj (vision encoder) ran on wrong GPU — apparent serialization across all OSes

**Symptom**: Dual-GPU vision inference was slow on every OS — ~67s wall clock for two simultaneous inferences (Windows), GPU1 taking ~5 minutes per frame (Linux). Both GPUs appeared to interfere with each other. The same pattern appeared on macOS (MoltenVK), Windows (WDDM), and Linux (Mesa radeonsi), which made it look like a hardware or driver-level limitation.

**Finding (Feb 24 2026)**:
Not serialization. Not a driver bug. The `--mmproj` (CLIP/SigLIP vision encoder) silently defaults to **Vulkan0** regardless of the `--device` flag. On this machine, Vulkan0 is the **Pro 580X** — which also runs Whisper. Both `gemma0` (told to use Vulkan1) and `gemma1` (told to use Vulkan2) were sending all image encoding work to Vulkan0 (Pro 580X). Their LLMs ran on the correct RX 580s; their vision encoders ran on the same shared Pro 580X.

Confirmed via live `gpu_busy_percent` sysfs monitoring during inference:

| Test | GPU0 (RX 580) | GPU1 (RX 580) | Pro 580X |
|------|--------------|--------------|----------|
| Vision solo GPU0 (broken config) | 44% avg | 0% | **53% avg** ← encoder here |
| Vision solo GPU1 (broken config) | 0% | 46% avg | **53% avg** ← encoder here |
| Vision both simultaneously (broken) | 29% avg | 30% avg | **70% avg** ← both competing |

Both Gemma instances were fighting over the Pro 580X. Every vision inference queued behind whatever the Pro 580X was already doing (the other Gemma, or Whisper). This was the "serialization" observed on three operating systems — it had nothing to do with OS, driver, or GPU hardware architecture.

**Solution**:
Use `GGML_VK_VISIBLE_DEVICES` to limit each server process to seeing only its own GPU. When a process has only one Vulkan device, the mmproj has no choice but to use it — both LLM and vision encoder land on the same GPU.

Changes to systemd service files:
- `gemma0.service`: added `Environment=GGML_VK_VISIBLE_DEVICES=1`, changed `--device Vulkan1` → `--device Vulkan0`
- `gemma1.service`: added `Environment=GGML_VK_VISIBLE_DEVICES=2`, changed `--device Vulkan2` → `--device Vulkan0`

Startup log confirms fix: `ggml_vulkan: Found 1 Vulkan devices` and `clip_ctx: CLIP using Vulkan0 backend` on the correct RX 580.

**Test confirmation (Feb 24 2026, indexer stopped, clean benchmark)**:

| Test | GPU0 (RX 580) | GPU1 (RX 580) | Wall | Pro 580X |
|------|--------------|--------------|------|----------|
| Vision solo GPU0 | 5.6s | — | — | **0%** |
| Vision solo GPU1 | — | 5.8s | — | **0%** |
| Vision both simultaneously | 5.6s (0.99x) | 5.7s (0.99x) | 5.7s | **0%** |

True parallel — wall clock for simultaneous (5.7s) matches solo (5.8s). 0.99x = no interference. Pro 580X at 0% during all Gemma inference, fully reserved for Whisper.

**Live indexer confirmation (Feb 24 2026)**: Both GPUs confirmed active during real workload indexing — GPU0 handling short clips, GPU1 processing `Murdock Story Review.mp4` (49 keyframes simultaneously).

**All-three-GPU benchmark (Feb 24 2026)**: Extended benchmark added to `gpu-parallel-test.py` — fires vision on GPU0, vision on GPU1, and whisper on GPU2 simultaneously. Results:

| Test | GPU0 (RX580 0c) | GPU1 (RX580 0f) | GPU2 (Pro 580X) | Wall |
|------|----------------|----------------|-----------------|------|
| Vision solo GPU0 | 5.6s | — | 0% | — |
| Vision solo GPU1 | — | 5.8s | 0% | — |
| Vision both simultaneous | 5.6s (1.00x) | 5.7s (0.99x) | 0% | 5.7s |
| Whisper solo GPU2 | 0% | 0% | 1.2s | — |
| All three simultaneous | 5.5s (0.99x) | 5.7s (0.99x) | 1.2s (1.00x) | 5.7s |

Serial estimate for all three: 12.5s. Actual wall clock: 5.7s. All three GPUs hit 100% peak independently with zero interference. Pro 580X at 0% during all Gemma inference phases — fully reserved for Whisper.

**Status**: FIXED — Feb 24 2026. Confirmed fully parallel on Linux/Mesa.

### 25. No static IP — DHCP can change

**Symptom**: After a DHCP lease renewal, the machine gets a new IP. SSH alias `llm-server` (if configured to .157) breaks. Search API at `http://10.10.11.157:8081` becomes unreachable from the network and from apps like VaultSearch.
**Root cause**: IP is assigned by DHCP. Current IP is 10.10.11.157 but is not guaranteed to stay fixed.
**Fix (Feb 26 2026)**: Configured static IP via netplan. Created `/etc/netplan/01-static.yaml` with `dhcp4: false`, address `10.10.11.157/24`, gateway `10.10.11.1`, DNS `10.10.11.1`. Disabled cloud-init network management (`/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg`). Applied with `netplan apply`. Route is now `proto static`. (Clara)
**Status**: FIXED — Feb 26 2026. Static IP configured via netplan (01-static.yaml). IP 10.10.11.157 now scope global, not dynamic.

### 26. No watchdog for pipeline stall

**Symptom**: `media-indexer` service shows `active (running)` but no files are being indexed. Log shows no recent activity. Queue has pending files.
**Root cause**: systemd only restarts services that crash. A silent stall — NAS unreachable, ffmpeg hang, inference hang — leaves the service running but not making progress. Nothing detects or recovers from this automatically.
**Fix (Feb 26 2026)**: Implemented `pipeline-watchdog.service` + `pipeline-watchdog.timer` (runs every 5 minutes). Script polls `/status` endpoint, compares `visual_analysis_complete` count against a stored baseline, restarts `media-indexer.service` if no progress for 15 minutes. (Ben)
**Status**: FIXED — Feb 26 2026. pipeline-watchdog.service + timer deployed. Polls /status every 5 min, restarts media-indexer if no progress for 10 min, logs to journald.

### 27. No disk space monitoring

**Symptom**: DB and thumbnails grow continuously with no alerting. If disk fills, SQLite writes fail silently, thumbnails stop saving, and the indexer may crash or stall.
**Root cause**: No monitoring or alerting on `/home/mediaadmin/media-index/` disk usage.
**Fix (Feb 26 2026)**: Implemented `disk-monitor.service` + `disk-monitor.timer` (runs every 15 minutes). Checks `df` for `/home/mediaadmin/media-index`, writes a notification to the `notifications` table (Issue #28) when usage exceeds 85% threshold. (Ben)
**Status**: FIXED — Feb 26 2026. disk-monitor.service + timer deployed. Checks usage every 15 min, alerts at 80%, logs to journald and /tmp/disk-status.

### 28. No alerting system

**Symptom**: Services go down, pipeline stalls, or disk fills — nothing sends a notification.
**Root cause**: No alerting configured. No email, SMS, Slack, or other notification channel.
**Fix (Feb 26 2026)**: Added `notifications` table to SQLite DB. `/notifications` endpoint added to `media-search.service` (port 8081) returns recent alerts as JSON. VaultSearch app gained a bell icon in the toolbar — polls `/notifications` and shows a badge + dropdown for unread alerts. Pipeline watchdog (Issue #26) and disk monitor (Issue #27) both write to this table. (Ben)
**Status**: FIXED — Feb 26 2026. Notifications table in DB, /notifications and /notifications/mark-read endpoints on port 8081. Watchdog and disk-monitor scripts insert alerts. VaultSearch app has bell icon with badge and popover list.

### 29. Scene detection timeouts starve GPU — high network, mid CPU, low GPU

**Symptom**: High NAS network usage, mid CPU, very low GPU compute utilization. Nearly every file logs `Scene detect timed out` and falls back to 1 keyframe. 67 timeout warnings confirmed in a single session.
**Root cause**: The scene detection timeout formula is `max(120s, int(duration * 0.3))`. For a long file this becomes very large (4027s video → 1208s timeout). ffmpeg must stream the entire video file from the NAS to scan for cuts — on long continuous recordings (SDDP Sony camera files, C06xx multi-hour recordings) this saturates NAS bandwidth and runs until the timeout expires. The result is a repeating cycle:
1. Scene detect runs for 2–20 minutes streaming a large file from NAS (high network, mid CPU)
2. Times out, falls back to 1 keyframe
3. GPU Gemma inference runs for ~15 seconds on that 1 keyframe (brief GPU spike)
4. Repeat

GPU is busy roughly 10% of the time. The other 90% it sits idle waiting on scene detection.
**What this looks like from the outside**: High NAS network traffic + mid CPU (multiple ffmpeg processes) + very low GPU compute. Looks like the pipeline is stalled but it is actually processing — just extremely slowly.
**Files most affected**: SDDP*.MP4 (Sony camera continuous recordings) and long C06xx clips. These have no scene cuts, so ffmpeg reads the whole file and finds nothing.
**Confirmed**: Feb 23 2026 — 67 timeout warnings in a single session. Timeout formula and cycle timing verified from `journalctl -u media-indexer` logs.
**Fix deployed (Feb 26 2026)**: Added `SCENE_SCAN_CAP = 600` to `SceneWorker`. `_detect_scenes()` now computes `scan_duration = min(duration, 600)` and passes `-t scan_duration` to ffmpeg. For a 93-min file that previously ran for 30+ minutes streaming the whole file from NAS, the fix completes in ~2 minutes and still detects 64 scene cuts from the first 10 minutes. Log confirmation: `SceneWorker [renderD128]: capping scan at 600s (file is 5390s) -- SGNFLM_S001_S001_T001.MOV`. The 4 stuck tasks (72–93 min files) were manually reset to `pending` and processed immediately with the new cap.
**Status**: FIXED — deployed and verified Feb 26 2026.

### 30. GPU fans not at max during inference — gpu-fans-max.service ran too early

**Symptom**: `gpu-fans-max.service` reported success at boot, but during inference both RX 580 fans were in auto mode (`pwm1_enable=2`, `pwm1=0`) rather than max. Fan RPMs were not readable via sysfs. GPUs potentially running hotter than intended under sustained inference load.

**Root cause**: `gpu-fans-max.service` ran at boot with `After=multi-user.target` — before the gemma services started. When `gemma0` and `gemma1` start and the AMD GPU driver initializes the hardware, it resets fan control back to automatic mode. Since the fan service had already exited, the reset was never corrected. The fans spent all of inference under driver auto control.

**Secondary symptom**: `fan1_input` returns `EINVAL` on both RX 580s when in auto mode at low temperature — this is normal driver behavior, not a hardware fault. The file exists and is readable when fans are in manual mode.

**Fix (Feb 24 2026)**:

1. **Updated `/usr/local/bin/gpu-fans-max.sh`** — now polls `http://localhost:8090/health` and `http://localhost:8091/health` in a loop (up to 120s) before writing fan control. This ensures the script runs *after* GPU initialization has completed and reset the fans.

2. **Updated `/etc/systemd/system/gpu-fans-max.service`** — changed `After=multi-user.target` to `After=gemma0.service gemma1.service` with `Wants=gemma0.service gemma1.service`. Service now starts in the correct order.

3. **Added `ExecStartPost=/usr/bin/sudo /usr/local/bin/gpu-fans-max.sh`** to `media-indexer.service` — re-applies max fan speed each time the indexer starts (handles the gap between boot and first indexing run, and catches fan resets after service restarts).

4. **Added sudoers entry** `/etc/sudoers.d/mediaadmin-fans` so `mediaadmin` can run the fan script as root without a password (required for the `ExecStartPost` to write sysfs fan files).

**Verified**: After fix, both fans confirmed at `pwm1_enable=1`, `pwm1=255`, ~3200 RPM immediately after `media-indexer` starts.

**Status**: FIXED — Feb 24 2026.

---

### 31. R3D and BRAW files fail scene detection — ffmpeg cannot read proprietary RAW formats

**Symptom**: 1,383 `scene_detect` tasks failed with `cannot determine duration` on Phase 2 launch (Feb 25 2026). All failures were `.R3D` files (RED Digital Cinema raw) and potentially `.braw` files (Blackmagic RAW).

**Root cause**: ffmpeg/ffprobe on Linux does not support `.R3D` or `.braw` formats — these are proprietary camera raw formats that require official SDKs.

**Impact**: ~1,383 scene_detect tasks failed. The files were already `indexed` (Phase 1 transcript complete), so searchability was not affected. Temporary fix applied Feb 25 2026 to gracefully skip these formats.

**Full fix (Feb 25 2026)** — native SDK tools installed and integrated:

**Tools installed on server:**
- `REDline Build 65.0.22` (`/usr/local/bin/REDline`) — RED's official CLI for R3D extraction. Downloaded from red.com.
- `braw-frame` (`/usr/local/bin/braw-frame`) — custom C++ tool built from Blackmagic RAW SDK 5.1. Extracts frames as BMP using `CreateBlackmagicRawFactoryInstanceFromPath()`. SDK libraries at `/opt/brawsdk/`, camera support datapacks at `/usr/share/blackmagicdesign/blackmagicraw/camerasupport/`.

**Pipeline changes (`media-indexer.py`):**
1. `_braw_info()` — calls `braw-frame info` to get frame count, fps, duration from a BRAW file.
2. `_braw_extract_frame_jpeg()` — extracts a BRAW frame as BMP via `braw-frame`, converts to JPEG via ffmpeg.
3. `_r3d_extract_frame_jpeg()` — extracts a single R3D frame as JPEG via REDline (`--format 3 --res 4 --start N --end N`), resizes via ffmpeg.
4. `SceneWorker._process()` — detects `.braw` extension: skips ffmpeg scene detection (can't decode `brst` codec), gets duration via `_braw_info()`, uses `_braw_extract_frame_jpeg()` for fixed-interval thumbnails.
5. `SceneWorker._process_r3d()` — new method for R3D: probes frame indices `[0, 24, 120, 360, 720, 1440, 2880, 5760]` via REDline, stops when one fails (past end of clip), stores approximate timestamps at 24fps.
6. `_create_tasks_for_file()` — R3D/BRAW exclusion removed; both formats now get `scene_detect` tasks.
7. `_backfill_scene_detect()` — R3D/BRAW exclusion removed from SQL query.

**BRAW audio**: `ffprobe` reads the MOV container and finds `pcm_s24le` audio. ffmpeg extracts it fine. BRAW transcription has always worked — no code change needed.

**REDline libmpg123 conflict**: REDline installer copies an old `libmpg123.so.0` to `/usr/local/lib/` which breaks ffmpeg (`libopenmpt.so.0: undefined symbol: mpg123_param2`). Fixed by removing the conflicting library: `sudo rm /usr/local/lib/libmpg123.so* && sudo ldconfig`.

**One-time DB fix**: Reset 1,383 abandoned R3D scene_detect tasks back to `pending` so they get reprocessed with the new code.

**Counts**: ~22,273 R3D files, ~1,454 BRAW files in the vault. All will now get keyframe thumbnails extracted.

**Status**: FULLY FIXED — Feb 25 2026. R3D and BRAW files now get real keyframe thumbnails via native SDK tools.

---

### 33. RX 580 #2 runs 16°C hotter than RX 580 #1 under sustained load

**Symptom**: Under full inference load, `0000:0f:00.0` (RX 580 #2, Gemma1, port 8091) reached **70°C** while `0000:0c:00.0` (RX 580 #1, Gemma0, port 8090) stayed at **54°C**. Same workload, both card fans at PWM 255 max (~3000 RPM). The temperature gap triggered a GPU dropout (Issue #23 pattern) during an experiment to raise the chassis exhaust fan speed.

**Investigation**:
- Both RX 580 card fans confirmed at `pwm1_enable=1, pwm1=255` — not a fan control issue
- Both GPUs at 100% `gpu_busy_percent` — same workload
- Chassis fans 2–4 (intake, manual) at 2000 RPM
- Chassis fan1 (exhaust, T2 auto) at only ~499 RPM — T2 wasn't spinning it up because its own sensors weren't hot
- PCIe topology: 0f:00.0 has a direct CPU root port (bus 0e→0f), same as the Pro 580X; 0c:00.0 goes through a PCIe switch (bus 08→09→0a→0c). Physical slot position likely gives 0f:00.0 worse airflow.
- Both cards have the same factory power cap: **155W max** (already below the reference 185W TDP)

**Root cause**: Physical slot position gives `0f:00.0` worse chassis airflow than `0c:00.0`. The 155W cap means the card is already drawing as much power as the driver allows — it's not over-clocked, just poorly placed thermally.

**Fix (Feb 25 2026)**: Set power cap on `0000:0f:00.0` to **130W**:
```bash
echo 130000000 | sudo tee /sys/bus/pci/devices/0000:0f:00.0/hwmon/hwmon*/power1_cap
```
Result: temperature under full load dropped from **70°C → 48°C** (22°C improvement). Both RX 580s now within 7°C of each other at sustained load.

**Persistence**: Added to `/usr/local/bin/gpu-fans-max.sh` — runs after gemma services initialize on every boot, same timing as fan control. The cap is applied automatically each time the indexer starts. Longer sustained-load test still needed to confirm stability.

**What did NOT help**: Setting chassis fan1 (exhaust) to 1000 RPM — this coincided with the GPU dropout and was reverted on power cycle. It's unclear whether fan1 is even in the thermal zone of the hot card.

**Status**: FIX DEPLOYED — 130W cap in `gpu-fans-max.sh`, applied on every boot. Long-duration stability test still pending.

---

### 32. Whisper starved — `_backfill_tasks` processes images before video/audio

**Symptom**: Whisper idle with only 3 pending `transcribe` tasks despite ~75,000 video/audio files on the NAS. `transcribe` complete count stuck at ~1,134. Pipeline appeared healthy but Whisper wasn't actually working through the backlog.

**Root cause**: ~77,000 files registered before the `file_type` column existed have `file_type=NULL`. `_backfill_tasks` fetches up to 1,000 of these per crawl cycle in insertion order with no type filtering. A large batch of early-inserted image files (`.jpg`, `.bmp`, `.png`) came before the video files in that ordering. Image files get `visual_analysis` tasks (not `transcribe` tasks), so Whisper received no work from those batches. After Whisper drained the handful of tasks it did get, it sat idle between crawl cycles waiting for the next 1,000.

**What made it non-obvious**: `SELECT COUNT(*) FROM tasks WHERE task_type='transcribe' AND status='pending'` showed only 3 — correct, but only because the video files deeper in the `file_type=NULL` pile hadn't been queued yet. The DB showed `86,511 files with no tasks at all` and `75,901 with status=pending`.

**Fix (Feb 25 2026)**: Added `CrawlerWorker._backfill_transcribe()` — a dedicated method that fetches up to 5,000 `file_type=NULL` candidates per cycle (overfetch to tolerate the image majority), filters to video/audio by extension in Python, creates `transcribe` tasks for up to 500 per cycle, and stamps `file_type` on those records so future queries work correctly. Called from `crawl_once()` after `_backfill_tasks`.

**Confirmed**: Within one crawl cycle of deploying the fix, Whisper picked up a `transcribe` task and began working continuously.

**Status**: FIXED — Feb 25 2026.

---

### 34. PerpetualWhisperWorker thread dies on `sqlite3.OperationalError: database is locked`

**Symptom**: Whisper GPU goes idle. No `transcribe` tasks in `assigned` state despite thousands pending. Gemma GPUs may also show longer-than-expected inter-task gaps. System appears healthy (`/health` returns `ok` on all three servers, media-indexer service stays running), but Whisper work has silently stopped.

**Root cause**: The `PerpetualWhisperWorker.run()` loop has no top-level exception handler. When `_claim_next()` (or any other call inside the loop) raises `sqlite3.OperationalError: database is locked`, the exception propagates out of `run()`, kills the thread permanently, and logs `Exception in thread whisper-perpetual:` to journald. WAL mode and `busy_timeout=30000` are configured correctly on the connection, but a transient write contention (multiple workers — GemmaWorkers, SceneWorkers, TaskCoordinator — all writing simultaneously) can exceed the timeout, causing the error instead of retrying.

**Example log (journald)**:
```
Exception in thread whisper-perpetual:
  File ".../media-indexer.py", line 1669, in run
    task = self._claim_next(db)
sqlite3.OperationalError: database is locked
```

**Observed**: Feb 25 2026 — thread died at 21:29:15, ~9 minutes after boot. Whisper idle from then on. 1,879 pending `transcribe` tasks accumulated with zero assigned.

**Immediate fix**: Restart `media-indexer` service to revive the thread:
```bash
sudo systemctl restart media-indexer
```

**Permanent fix needed**: Wrap `run()` body in a try/except that catches `sqlite3.OperationalError`, logs a warning, sleeps briefly, and continues the loop instead of letting the thread die. Also applies to `GemmaWorker.run()` and `SceneWorker.run()` for the same reason.

**Status**: FIXED — Feb 25 2026. Wrapped the `while` loop body in `PerpetualWhisperWorker.run()` and `SceneWorker.run()` with a top-level `except Exception` that logs a warning and sleeps 5s before retrying, matching the existing `GemmaWorker` pattern. `TaskCoordinator` was already safe.

---

### 35. Scene detection stalls on non-RAW files — both VAAPI and CPU watchdogs fire

**Symptom**: SceneWorker logs `CPU also stalled on <file> — using fixed-interval` for ordinary `.mp4` and `.mov` files (not R3D or BRAW). The file completes scene_detect with evenly-spaced keyframes instead of real scene cuts. Pre-Feb-25-2026 code silently continued with fixed-interval; the bad keyframes were never flagged.

**Scale**: 204 occurrences logged before the behavior was changed (Feb 25 2026). Affected files include Canon Cinema C-series clips (`C0645.MP4`, `C0799.MP4`, `C0804.MP4`) and various training `.mov` files.

**Root cause**: Identified Feb 26 2026. The watchdog monitors `out_time` from ffmpeg's `-progress pipe:1` output, but `out_time` only advances when a frame **passes the `select='gt(scene,0.30)'` filter** — i.e., when a scene change is detected. For videos with long static sections (interviews, church classes, continuous long shots), there can be 30+ consecutive seconds of video with no scene changes. `out_time` stalls → watchdog fires → process killed → both VAAPI and CPU attempts fail → `abandoned`.

Two observed failure modes:
1. **HEVC + VAAPI**: `out_time=N/A` throughout — VAAPI decode of HEVC produces no selected output frames in the first 30s for certain files → watchdog fires immediately at T+30s.
2. **Any codec, long static sections**: `out_time` sticks at the timestamp of the last scene change. If the next cut is >30s later, watchdog fires mid-video.

Files confirmed affected:
- **270 HEVC 1080p** (codec=NULL in DB — indexed before codec detection was added): church service recordings, school-of-ministry class recordings, worship recordings with long continuous shots
- **83 ProRes 4K** (Atomos Ninja footage): long continuous camera takes (up to 6 hours, 60 GB files)
- **54 HEVC 4K** (Ninja exports): long-form edited exports

All files are readable, probe correctly, and decode at normal speed (4–12× realtime). The stall is NOT an I/O or decode issue — it is a watchdog design flaw: "no output frames for 30s" ≠ "ffmpeg is stuck".

**Impact (pre-fix)**: Affected files got 3 evenly-spaced keyframes (for videos under 30 min) or ~15 fixed-interval frames (for long videos). No scene cuts detected. Visual descriptions and face detection ran on those fixed keyframes — not necessarily on the most meaningful frames.

**Fix deployed (Feb 25 2026)**: SceneWorker now marks the `scene_detect` task `abandoned` with `error_message = "scene_detect_stalled"` instead of silently proceeding. No keyframes are emitted; the file still gets indexed (transcript preserved). The file is queryable and retriable.

**Already-stalled files**: The 204 files processed before this fix have fixed-interval keyframes and `status='complete'` on their scene_detect tasks. They are NOT retroactively flagged — their keyframes exist but may not be at scene cut points.

**Find currently-stalled files**:
```sql
SELECT f.path, t.error_message
FROM files f
JOIN tasks t ON t.file_id = f.id
WHERE t.task_type = 'scene_detect'
  AND t.status = 'abandoned'
  AND t.error_message = 'scene_detect_stalled'
ORDER BY f.path;
```

**Retry a stalled file** (after investigating root cause):
```sql
UPDATE tasks SET status='pending', worker_id=NULL, started_at=NULL, error_message=NULL
WHERE task_type='scene_detect' AND file_id='<file_id>' AND error_message='scene_detect_stalled';
```

**Fix deployed (Feb 25–26 2026)**: `_run_ffmpeg_watchdog()` now monitors CPU time via `/proc/{pid}/stat` (utime+stime jiffies). Watchdog fires only if the process has consumed **no CPU** for 30 seconds — meaning ffmpeg is genuinely hung, not just working on a quiet section. This eliminates all false `out_time`-based firings.

Combined with Issue #29 fix: `SceneWorker._detect_scenes()` now caps scan at 600s (`SCENE_SCAN_CAP`), so long files are not even at risk of triggering the watchdog.

**Status**: FIXED — CPU-time watchdog deployed Feb 25 2026; scan cap deployed Feb 26 2026. 439 previously-stalled files will be retried by the pipeline automatically.

---

### 36. TIFF (and BMP) images fail visual_analysis — llama.cpp vision rejects non-JPEG/PNG formats

**Symptom**: HTTP 400 errors on both Gemma servers, `visual_analysis` tasks marked `failed` with `error_message = "describe_image returned None"`. Burst of 9 errors at 22:00–22:01 UTC Feb 25 2026, then stopped.

**Root cause**: `pre_encode_image()` sends raw image bytes as base64 with the file's native MIME type (`image/tiff`, `image/bmp`, etc.). llama.cpp's vision endpoint only accepts `image/jpeg`, `image/png`, and `image/webp` — it returns HTTP 400 for any other format.

**Files affected**: `/mnt/vault/Videos Vault/DVD Rips/NWCC history book 2/Staff/*.tif` and one `misc/Cal&Steve.tif` — old scanned staff photos from a church history book digitization project. Also `file_type = NULL` on all 9 (never classified).

**Scale**: 9 files in this run. Unknown how many other TIFF/BMP files exist in the vault.

**Fix needed**: In `pre_encode_image()`, detect unsupported formats (TIFF, BMP) and convert to JPEG via ffmpeg before base64 encoding. ffmpeg is already available on the server. Alternatively, add format conversion as a pre-step in the GemmaWorker prep thread.

**Workaround**: The 9 failed tasks can be retried once the fix is deployed:
```sql
UPDATE tasks SET status='pending', worker_id=NULL, started_at=NULL, error_message=NULL
WHERE task_type='visual_analysis' AND status='failed' AND error_message='describe_image returned None';
```

**Fix deployed (Feb 25 2026)**: Added `_CONVERT_TO_JPEG = {"tif", "tiff", "bmp"}` set and conversion logic to `pre_encode_image()`. When the file extension is in that set, ffmpeg converts to JPEG in-process (pipe:1 output, no temp file) with `MAX_IMAGE_DIMENSION` resize applied. Returns `image/jpeg` MIME type. The 15 failed tasks were reset to pending via SQL after deployment. Service restarted at 22:56 UTC.

**Status**: FIXED — Feb 25 2026.

---

### 37. SQLite WAL file bloat (3.4 GB) — GemmaWorkers completely stalled for 16+ hours

**Discovered**: Feb 26 2026 (~15:10 UTC) by automated monitoring

**Symptom**: Both GemmaWorkers (8090, 8091) log `unexpected error: database is locked` every ~5 seconds continuously. **Zero visual_analysis tasks have completed since the service restarted at 22:56 UTC Feb 25** — 16+ hours of zero throughput. SceneWorker, FaceWorker, and WhisperWorker are making progress (with occasional DB errors) but GemmaWorkers cannot make any progress.

**Numbers at time of discovery**:
- WAL file: `/home/mediaadmin/media-index/index.db-wal` = **3.4 GB**
- `PRAGMA wal_checkpoint(PASSIVE)` returned `log=867167, checkpointed=889` — only 0.1% checkpointed
- DB lock errors in the last 1 hour: **2,106** (~35/min)
- DB lock errors since service start: **15,641**
- Open file descriptors to index.db: **34** (30+ threads all holding connections)
- pending visual_analysis: 120,274
- completed visual_analysis: 4,492 (all from before the Feb 25 22:56 restart)

**Root cause**: SQLite WAL (Write-Ahead Log) grows without bound when the auto-checkpoint cannot get exclusive access. With 30+ worker threads (GemmaWorkers, SceneWorkers, FaceWorkers, WhisperWorker, CrawlerWorker, TaskCoordinator) all holding open connections and writing continuously, the auto-checkpoint (fires at 1,000 pages / ~4 MB) can never acquire the brief exclusive lock it needs to flush WAL pages back to the main DB file. The WAL has accumulated to 867,167 pages (~3.4 GB). This causes cascading slowdown: every read must scan the entire 3.4 GB WAL to reconstruct page versions, slowing all DB operations, which increases lock hold times, which increases contention further.

**Why GemmaWorkers are uniquely blocked** (while other workers still make progress):
- GemmaWorker has a fast task cycle: claim task (write) → inference (~5s, no DB) → write results (write) → repeat
- The "time needing a write lock" vs "time doing work" ratio is HIGH for Gemma (~30% of cycle time needs DB writes)
- Other workers (SceneWorker, WhisperWorker) spend minutes doing their actual work (scene detection, transcription) between DB writes — much lower DB pressure ratio
- With the WAL at 3.4 GB and 30s busy_timeout, GemmaWorker's claim step times out every retry
- The 5-second sleep after error, plus instant retry that also fails, means Gemma never makes progress

**Immediate fix (run on server)**:
```bash
# Stop the indexer to release all reader connections
sudo systemctl stop media-indexer

# Checkpoint and truncate the WAL (safe — WAL data is flushed to main DB first)
python3 -c "
import sqlite3
conn = sqlite3.connect('/home/mediaadmin/media-index/index.db')
conn.isolation_level = None
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
print('WAL checkpointed and truncated')
conn.close()
"

# Restart the indexer
sudo systemctl start media-indexer
```

**Permanent fix needed in media-indexer.py**: Add a periodic WAL checkpoint in CrawlerWorker (or a dedicated background thread) that runs `PRAGMA wal_checkpoint(PASSIVE)` every N minutes when load is low. Also consider reducing `PRAGMA wal_autocheckpoint` from default 1000 pages to 100 pages so checkpointing happens more aggressively. Long-term: consolidate DB connections — instead of each thread holding its own persistent connection, use a connection pool or a single DB writer thread with a queue.

**Fix applied (Feb 26 2026, ~15:21 UTC)**: Ben ran `PRAGMA wal_checkpoint(TRUNCATE)` with media-indexer stopped. WAL file dropped from **3.4 GB → 4.1 MB**. DB lock errors dropped from **353/10min → 9/10min** (97% reduction). Both Gemma GPUs immediately returned to **100% utilization**. visual_analysis completions resumed (4,492 → 4,555 in first 10 minutes after fix). visual_analysis assigned: 5 tasks in-flight.

**Permanent fix (Feb 26 2026)**: Added `PRAGMA wal_autocheckpoint(100)` to `init_db()` (reduces WAL page threshold from 1,000 to 100) and `PRAGMA wal_checkpoint(PASSIVE)` call inside `CrawlerWorker.run()` loop (runs every crawl cycle). WAL will now stay small even under sustained multi-worker load. (Ben)

**Status**: FIXED — Feb 26 2026. Permanent fix deployed: wal_autocheckpoint(100) in init_db() and PRAGMA wal_checkpoint(PASSIVE) in CrawlerWorker after each crawl cycle.

---

### 38. TIFF visual_analysis failures persist after Issue #36 fix — 99 tasks failed

**Discovered**: Feb 26 2026 (~15:10 UTC) by automated monitoring

**Symptom**: 99 `visual_analysis` tasks have `status='failed'` with `error_message='describe_image returned None'`. All are `.tif` files. Issue #36 was declared FIXED on Feb 25 2026 with a JPEG conversion fix in `pre_encode_image()`, but failures are still accumulating.

**Affected files (sample)**:
- `Videos Vault/DVD Rips/NWCC history book 2/Staff/*.tif` (staff photos)
- `Videos Vault/DVD Rips/Auditorium pictures/Nwoods locations/*.tif`

**Possible causes**:
1. The JPEG conversion fix in `pre_encode_image()` deployed at 22:56 UTC but 99 failures were marked before the fix applied (old tasks in flight at restart time)
2. The conversion is running but the resulting JPEG is corrupt/empty for some files (very old scans, unusual encoding)
3. A different code path is being hit for these files that bypasses `pre_encode_image()`

**Diagnostic**:
```sql
-- Check if these are all pre-fix failures (completed_at before 22:56 UTC Feb 25)
SELECT completed_at, COUNT(*) FROM tasks
WHERE task_type='visual_analysis' AND status='failed'
GROUP BY date(completed_at);
```

**Fix (once #37 is resolved and GemmaWorkers are running)**:
1. Reset failed TIFF tasks to pending — they will retry with the JPEG conversion fix in place
2. If they fail again, check ffmpeg JPEG conversion output for these specific files
```sql
UPDATE tasks SET status='pending', worker_id=NULL, started_at=NULL, error_message=NULL
WHERE task_type='visual_analysis' AND status='failed' AND error_message='describe_image returned None';
```

**Fix (Feb 26 2026)**: Confirmed root cause was a second code path in `describe_image()` fallback that read raw file bytes instead of calling `pre_encode_image()`. Fixed the fallback to always call `pre_encode_image()`. 38 remaining failed tasks (others had already retried) reset to pending via SQL. (Clara)
**Status**: FIXED — Feb 26 2026. 38 remaining failed TIFF tasks reset to pending and retried successfully with the describe_image fix already in place.

---

### 39. VaultSearch app shows false "Processing" status when server is unreachable

**Discovered**: Feb 26 2026

**Symptom**: The VaultSearch app displays a "Processing" state even when the search API (port 8081) is down or unreachable. The user has no way to tell the server is actually offline.

**Root cause**: App does not distinguish between "server is processing a request" and "server is not responding." Network errors or timeouts are not surfaced as an error state in the UI.

**Fix (Feb 26 2026)**: Port 8081 connectivity check gates all GPU server status. If `/status` fails or times out (5s timeout), all GPU servers are shown as offline — no more false "Processing." VaultSearch now shows "Server offline" with a clear error message. (Ben)
**Status**: FIXED — Feb 26 2026. StatusService.swift uses port 8081 as connectivity gate: if fetchIndexer() fails for any reason, all GPU servers are immediately set offline. 5-second timeout replaces URLSession.shared default.

---

### 40. Face detection, scanning, and clustering should be fully automatic

**Discovered**: Feb 26 2026

**Symptom**: The user is expected to manually trigger or manage face detection, scanning, and clustering operations from within the app. This is an operational burden and easy to forget.

**Desired behavior**: Face detection, scanning, and clustering should run automatically as part of the pipeline — no user intervention required. The app should handle all phases without the user needing to initiate or monitor them manually.

**Status**: OPEN

---

### 41. extract_thumbnail() silently succeeds with no file when -ss precedes -i at small timestamps

**Discovered**: Feb 26 2026 by Clara during FaceWorker investigation

**Symptom**: `extract_thumbnail()` returns `True` but never writes the thumbnail file. Downstream tasks (`face_detect`, `visual_analysis`) then fail with "thumbnail not found."

**Root cause**: ffmpeg command uses `-ss` (seek) before `-i` (input) for fast seeking. When the timestamp is very small (e.g., 0.021s), ffmpeg fast-seeks past the frame, encodes nothing, and exits 0. The function sees exit code 0 and returns `True` without checking whether the output file was actually written.

**Known affected file**: `NINJVP_S001_S001_T002.MOV` (Ninja camera footage, 21 Days Devos 2025). Other files with very small first-keyframe timestamps may be affected.

**Fix**: Either move `-ss` after `-i` (slower but accurate) for small timestamps, or add a check after ffmpeg exits that the output file exists and has nonzero size before returning `True`.

**Status**: FIXED (Feb 26 2026) — 3-attempt fallback deployed: fast seek → accurate seek → first frame. File size check after each attempt.

---

### 42. RESOURCE.FRK files indexed as images — will always fail visual_analysis

**Discovered**: Feb 26 2026 by Ben during Issue #38 investigation

**Symptom**: 32 files with fake image extensions (`.jpg`) inside `RESOURCE.FRK` directories were registered by the crawler, producing 48 tasks that always fail. llama.cpp rejects them — they are macOS resource fork metadata blobs, not real images.

**Root cause**: `RESOURCE.FRK` is a macOS resource fork container directory. Files inside it have real-looking extensions but contain binary metadata. The crawler didn't exclude this directory.

**Fix** (Feb 26 2026):
- Added `"RESOURCE.FRK"` to `SKIP_DIRS` in `media-indexer.py`
- Changed dirs filter to case-insensitive: `d.upper() not in {s.upper() for s in SKIP_DIRS}`
- Deleted 32 existing DB entries and 48 tasks via SQL
- Restarted media-indexer

**Status**: FIXED

---

### 43. Staff face seeding used too-loose threshold — wrong names assigned

**Discovered**: Feb 26 2026 (user reported names in app were wrong)

**Symptom**: Searching by person name in VaultSearch returned wrong people. Faces labeled "Brianna Sturgeon" or "Kelly Benefield" clearly weren't those people.

**Root cause**: Two-pass staff photo seeding used thresholds of 0.50 and 0.55. At 0.55 several clusters were incorrectly matched. Additionally, `assign_new_faces()` was called after seeding — this propagated each named cluster's identity to ALL faces within distance threshold across ALL clusters, cascading incorrect names to thousands of files. For example, Brianna Sturgeon (seed cluster #886, 2 faces) spread to 60+ other clusters.

**Fix** (Feb 26 2026):
1. Wiped all person assignments: `UPDATE faces SET person_id = NULL`, `DELETE FROM persons`, `UPDATE files SET face_names = NULL`
2. Confirmed `assign_new_faces()` is NOT called automatically — only via manual CLI/HTTP endpoint — so no code change needed
3. Re-seeded with strict threshold 0.45 + ambiguity check (second-best match must be >0.03 worse than best) + 200-face cluster cap
4. Note: at 0.45, NO staff matched (best distance was Larry Riley at 0.479). The `face_recognition` HOG model tends to produce distances of 0.48–0.59 for correct matches. A threshold of 0.50 is the practical minimum for this model/hardware combination.

**Lesson**: Do NOT call `assign_new_faces()` after seeding. The seed step names only the representative cluster; assign propagation must be a separate, deliberate manual action after verifying seed accuracy. The cascade is the danger, not the threshold alone.

**Re-seed (Feb 26 2026)**: Implemented `/home/mediaadmin/seed_staff.py` — fetches staff roster from Blackpulp portal API (`https://widgets.blackpulp.com/api/v1/northwoods/group_collections/1/contacts`), downloads profile photos, runs `face_recognition` HOG encoding, compares against cluster centroids. Rules: distance ≤ 0.50, ambiguity margin > 0.03, cluster cap 200. Does NOT call `assign_new_faces()`. (Clara)

**Result**: 12 staff seeded at threshold 0.50: Amy Evans, Amy Sutton, Bri Hallstrom, JoAnn Hageman, Jeannine Vote, Jeremy Moser, Kayla Archdale, Layla Fuller, Michelle Potter, Natalie Lee, Bruce Nielson, Todd Parmenter. Josh Rivera already existed. 3 borderline matches held back (Jon Rychener dist=0.38 but margin 0.0047, Cal Rychener margin 0.0244, Dawn Minch margin 0.0203) — name manually via web UI if desired. Note: previous staff roster (Larry Riley, Jason Eskridge, etc.) no longer on staff page — roster has changed.

**Status**: FIXED — Feb 26 2026. seed_staff.py deployed on server. 12 current staff seeded at threshold 0.50 with 0.03 ambiguity margin. Script is reusable — re-run when roster changes.

---

### 44. Files stuck in 'indexing' status are not reset on service restart

**Discovered**: Feb 26 2026 (Alice monitoring session, ~21:27 UTC)

**Symptom**: 5 files have been stuck in `status='indexing'` for hours (possibly days). After a clean service restart at 21:26 UTC, the startup code did NOT reset them to `pending`. They remain permanently stuck and will never be processed unless manually fixed.

**Affected files (as of Feb 26 2026 21:27 UTC):**
- `/mnt/vault/Projects Vault/2024/Christmas/Announcements/Comp 1.mp4`
- `/mnt/vault/Projects Vault/2024/Christmas/Announcements/Giving V2.mov`
- `/mnt/vault/Projects Vault/2024/Christmas/Announcements/Giving.mov`
- `/mnt/vault/Projects Vault/2024/21 Days Devos 2025/Footage/Ninja/Camera 2/NINJVP_S001_S001_T002.MOV`
- `/mnt/vault/Projects Vault/2024/The Lost Stories/Murdochs INVITED/Murdock Story Review.mp4`

**Root cause**: The `status='indexing'` field is set when a file is claimed by a worker. On restart, orphaned 'indexing' files are not reset to 'pending'. The startup code likely has no recovery logic for this state.

**Manual fix**:
```sql
UPDATE files SET status='pending' WHERE status='indexing';
```

**Permanent fix**: Add startup recovery in `init_db()` or startup code:
```python
db.execute("UPDATE files SET status='pending' WHERE status='indexing'")
```

**Update (Feb 26 2026 21:33 UTC)**: A second service restart reset the files to pending. Files are no longer stuck.

**Permanent fix (Feb 26 2026)**: Added `UPDATE files SET status='pending' WHERE status='indexing'` to `init_db()`, called before any worker threads start. Orphaned `indexing` files are now reliably reset on every service startup. (Ben)
**Status**: FIXED — Feb 26 2026. Startup recovery added to init_db() — resets status='indexing' to 'pending' before workers start, eliminating race condition.

---

### 45. Search improvements not taking effect — media-search.service not restarted

**Discovered**: Feb 26 2026 (~21:50 UTC)

**Symptom**: Natural language searches like "show me a stage with lighting" return 0 results even after `build_fts_query()` was added to `_fts_search` and `media-indexer.service` was restarted. Shorter queries like "stage lighting" returned different result counts than "stage with lighting" despite producing the same FTS query.

**Root cause**: The system has TWO separate services:
- `media-indexer.service` — runs `media-indexer.py watch` (pipeline workers, no HTTP)
- `media-search.service` — runs `media-indexer.py serve 8081` (HTTP API on port 8081)

When code changes were deployed (including `build_fts_query` in `_fts_search`), only `media-indexer.service` was restarted. `media-search.service` kept running code loaded at 18:01 UTC — 3.5 hours before the changes were deployed. It was running the old phrase-match `_fts_search` (`'"full query string"'`) while the file on disk had the keyword-AND match version.

**Fix**: `sudo systemctl restart media-search.service`

**Status**: FIXED (Feb 26 2026 21:50 UTC). "Show me a stage with lighting" now returns 5 relevant results.

**Prevention**: Whenever deploying changes to `media-indexer.py`, restart BOTH services:
```bash
sudo systemctl restart media-indexer.service media-search.service
```

---

## How to Use This Document

When encountering an issue:
1. **Check this list first** — is it something we've already fixed?
2. If it's a regression of a fixed issue, note that and investigate why the fix reverted
3. If it's new, add it with symptom + root cause + fix
4. Always mark the status clearly

### Patterns to Watch For
- **Machine unreachable but physically on** → Issue #21. WoL does not work. Needs full physical power cycle.
- **VaultSearch shows "Processing" but nothing works** → Issue #22/#39 FIXED (Feb 26 2026). Port 8081 is now the connectivity gate. If the problem recurs, check if `media-search.service` is running (`systemctl status media-search`).
- **GPU missing under Ubuntu / inference hangs** → Issue #23. Check `cat /sys/bus/pci/devices/0000:0f:00.0/power/control` — should be `on`. If `auto` or `suspended`, BACO fix didn't apply; run `echo on | sudo tee /sys/bus/pci/devices/0000:0f:00.0/power/control`. Also check `lspci`, `dmesg | grep amdgpu`.
- **GPU mapping wrong after reboot** → Issue #2. Never hardcode Vulkan indices. Verify with `llama-server --list-devices`.
- **Server OOMs during vision** → Issue #11. Context must be 1024, not 2048.
- **Whisper idle, no `transcribe` tasks assigned despite pending backlog** → Issue #34. Thread died on DB lock. Check `journalctl -u media-indexer | grep "Exception in thread"`. Fix: `sudo systemctl restart media-indexer`.
- **Pipeline slow / GPUs idle** → Check Issue #7 (Whisper blocking) and Issue #6/#15 (hardware limits). Issue #16 serialization was identified on Windows — run benchmark on Linux before assuming it applies (Issue #24).
- **GPU1 very slow / completely unresponsive, health endpoint shows ok** → Issue #23. GPU has dropped out. Run diagnostics (lspci, dmesg, try restarting gemma1 service) before power cycling. See Issue #23 for full diagnostic checklist.
- **Pipeline stalled (service running, no progress)** → Issue #26 FIXED (Feb 26 2026). Pipeline watchdog now auto-restarts `media-indexer` if no progress for 15 min. If stall recurs, check watchdog logs (`journalctl -u pipeline-watchdog`) and NAS mount (`mount | grep vault`).
- **IP address changed / unreachable** → Issue #25 FIXED (Feb 26 2026). Static IP configured via netplan. Should not recur. If it does, verify `/etc/netplan/01-static.yaml` still exists.
- **Disk full / write failures** → Issue #27 FIXED (Feb 26 2026). Disk monitor alerts at 85% via the notifications system. Check VaultSearch bell icon for alerts, or `df -h /home/mediaadmin/media-index`.
- **Apparent serialization / slow dual-GPU vision** → Issue #24 FIXED. Root cause was mmproj defaulting to Vulkan0 (Pro 580X) on both Gemma servers. Fix: `GGML_VK_VISIBLE_DEVICES=1/2` in service files. Verified 0.99x parallel on Linux — all three GPUs run fully independently. Not a driver bug, not a hardware limit.
- **Whisper fails on all files** → Issue #17. Check if ffmpeg stderr is being logged. DJI drone files may lack audio tracks.
- **Can't read files** → Issue #9. NAS credentials needed for NAS mount.
- **High network + mid CPU + very low GPU** → Issue #29. Scene detection is timing out on long files, streaming them fully from NAS before giving up. GPU sits idle ~90% of the time waiting on scene detect. Check `journalctl -u media-indexer -n 50` for `Scene detect timed out` warnings.
- **All recent files have only 1 keyframe / no scene cuts detected** → Issue #29. Scene detection timing out, falling back to 1 keyframe on every file. Same root cause as above.
- **GPU fans in auto mode during inference / fan RPM unreadable** → Issue #30. AMD driver resets fan control when GPU initializes. Run `sudo systemctl restart gpu-fans-max` to re-apply. On next boot this is automatic.
- **File indexed but has no keyframes / no ai_description, scene_detect abandoned** → Issue #35. Both VAAPI and CPU stalled; file abandoned without keyframes. Root cause unknown. Find with: `SELECT f.path FROM files f JOIN tasks t ON t.file_id=f.id WHERE t.task_type='scene_detect' AND t.status='abandoned' AND t.error_message='scene_detect_stalled'`.
- **GemmaWorkers log `database is locked` every 5s, zero visual_analysis completions** → Issue #37. WAL file bloat. Check `ls -lh /home/mediaadmin/media-index/index.db-wal` — if >500 MB, checkpoint is failing. Fix: `sudo systemctl stop media-indexer`, run `PRAGMA wal_checkpoint(TRUNCATE)`, restart. See Issue #37 for full procedure.
- **TIFF/BMP visual_analysis tasks keep failing with `describe_image returned None`** → Issue #38 (or regression of #36). Reset to pending and retry; if they fail again, check ffmpeg JPEG conversion for those specific files.
