# LLM Server

Local LLM inference server running on the Intel Mac Pro (DC---Pro).

## Hardware
- **Machine**: Mac Pro (Intel Xeon W-3245, 96GB RAM)
- **GPUs**: 2x AMD Radeon RX 580 (8GB each) + 1x AMD Radeon Pro 580X (8GB) = 24GB VRAM
- **IP**: 10.10.11.173
- **SSH**: mediaadmin@10.10.11.173

## SSH Access

Passwordless SSH is configured from the development Mac to the Mac Pro.

### Connection
```bash
ssh mediaadmin@10.10.11.173
```

### Running Remote Commands
```bash
# Single command
ssh mediaadmin@10.10.11.173 "command here"

# Multi-line script via heredoc — use this pattern for writing files:
ssh mediaadmin@10.10.11.173 'cat > ~/somefile.sh << '\''SCRIPT'\''
#!/bin/bash
echo "file contents here"
SCRIPT'

# IMPORTANT: chmod must be a separate SSH command after the heredoc write:
ssh mediaadmin@10.10.11.173 "chmod +x ~/somefile.sh"
```

### Deploying Files
```bash
scp localfile.py mediaadmin@10.10.11.173:~/remotefile.py
```

### Background Processes
```bash
# Start process in background (survives SSH disconnect)
ssh mediaadmin@10.10.11.173 'nohup python3 ~/script.py > ~/log.log 2>&1 &'

# Check if process is running
ssh mediaadmin@10.10.11.173 "ps aux | grep script.py | grep -v grep"
```

### Gotchas
- **Python 3.9 on Mac Pro**: No f-string nested quotes (`f"{'x'}"` syntax fails). Use temp variables instead.
- **chmod after heredoc**: When writing a file via heredoc over SSH, the `chmod` runs locally if chained in the same command. Always use a separate `ssh` call for chmod.
- **SMB paths with spaces**: Quote paths carefully in SSH commands: `"ls '/Volumes/Vault/Videos Vault'"`
- **Long-running indexer**: Always use `nohup` + `&` so the process survives SSH disconnect.
- **Querying SQLite remotely**: Use `sqlite3 ~/media-index/index.db "SELECT ..."` — avoid `!=` operator (use `<>` or `NOT IN` instead for compatibility with shell escaping).

## Software Stack
- **llama.cpp** with Vulkan backend (via MoltenVK)
- Multi-GPU support across all 3 cards (Vulkan0, Vulkan1, Vulkan2)
- OpenAI-compatible API on port 8080

## Models

All model files stored at `~/models/` on the Mac Pro.

### Default: Qwen3-14B Q4_K_M (recommended)
- **File**: `Qwen3-14B-Q4_K_M.gguf` (8.4 GB)
- **Speed**: ~14 t/s single-user, ~3.4 t/s per-user with 3 concurrent
- **VRAM**: ~8.2 GB across 3 GPUs (leaves ~16 GB free)
- **Parallel**: Runs with `--parallel 3` for multi-user/agentic use
- **Thinking mode**: Qwen3 has built-in reasoning (thinking mode ON by default). Add `/no_think` to prompts for direct answers without internal reasoning
- Text-only (no vision)

### Available: Qwen3-VL-32B Q4_K_M
- **File**: `Qwen3VL-32B-Instruct-Q4_K_M.gguf` (19.8 GB, no hyphen between Qwen3 and VL)
- **Vision encoder**: `mmproj-Qwen3VL-32B-Instruct-F16.gguf`
- **Speed**: ~7 t/s single-user
- **VRAM**: ~20 GB across 3 GPUs (tight fit)
- Vision-capable (can process images)

### Available: Gemma 3 12B Q4_K_M (vision + indexer)
- **File**: `gemma-3-12b-it-Q4_K_M.gguf` (6.8 GB) from bartowski
- **Vision encoder**: `mmproj-gemma-3-12b-it-f16.gguf` (815 MB) from bartowski
- **Speed**: ~13 t/s single-user (text and vision)
- **VRAM**: ~8 GB across 3 GPUs (leaves ~16 GB free)
- **Parallel**: Runs with `--parallel 3` for indexer + user queries
- Vision-capable, used by the media indexer

### Available: Qwen3-VL-8B Q4_K_M (tested, not recommended)
- **File**: `Qwen3VL-8B-Instruct-Q4_K_M.gguf` (4.7 GB)
- **Vision encoder**: `mmproj-Qwen3VL-8B-Instruct-F16.gguf` (1.1 GB)
- **Speed**: ~21.6 t/s text generation (fastest model tested)
- **Tool calling**: Works natively with `--jinja` flag — no proxy needed
- **Vision**: Unusable on AMD GPUs — encoder takes 87s for 640px image, full-size images timeout
- **Verdict**: Great for text/tool calling, but vision is broken on Vulkan/AMD. Use Gemma for vision tasks.

### Available: Qwen3-VL-32B Q8_0 (not recommended)
- **File**: `Qwen3VL-32B-Instruct-Q8_0.gguf` (34.8 GB)
- **Speed**: ~0.14 t/s — spills ~11 GB to CPU RAM, 44x slower

## Key Files on Mac Pro
- `~/llama.cpp/` — llama.cpp source and build (Vulkan backend)
- `~/llama-metal-mgpu/` — experimental Metal multi-GPU build (see Research section below)
- `~/models/` — GGUF model files
- `~/start-llm-server.sh` — start server (`14b` default, `32b` for vision, `32b-q8` for Q8, `gemma` for indexer)
- `~/stop-llm-server.sh` — stop server
- `~/status-llm-server.sh` — check if server is running
- `~/media-indexer.py` — media indexer and search API
- `~/start-media-services.sh` — start search API (+ optional watcher)
- `~/stop-media-services.sh` — stop search API and watcher
- `~/media-index/` — SQLite database, thumbnails, and logs

## API Access
Any tool that supports OpenAI-compatible APIs can connect to:
```
http://10.10.11.173:8080
```

## Tool-Calling Proxy

Gemma 3 12B does not support the OpenAI `tools` API natively. The tool-calling proxy (`tool-proxy.py`) bridges this gap so Continue's MCP tools work with Gemma.

### How It Works
```
Continue (VS Code) → localhost:8083 → 10.10.11.173:8080 (Gemma)
```

1. **Intercepts** the `tools` parameter from Continue's chat request
2. **Injects** tool descriptions into the system prompt as plain text
3. **Forwards** the modified request to Gemma (without the `tools` field)
4. **Parses** Gemma's text response for tool call patterns
5. **Rewrites** the response to OpenAI `tool_calls` format if a tool call was detected

### Supported Response Formats
The proxy recognizes three patterns Gemma might use:
- XML tags: `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`
- Backtick blocks: `` ```tool_call {"name": "...", "arguments": {...}} ``` `` (Gemma prefers this)
- Raw JSON: `{"name": "...", "arguments": {...}}`

### Running the Proxy
```bash
# Default (port 8083, backend at Mac Pro)
python3 tool-proxy.py

# Custom ports
python3 tool-proxy.py --port 8083 --backend http://10.10.11.173:8080
```

### Continue Config
Continue must point to the proxy, not directly to Gemma:
```yaml
models:
  - name: Gemma 3 12B (Vision)
    provider: openai
    model: gemma-3-12b
    apiBase: http://localhost:8083/v1  # proxy, NOT direct
    apiKey: none
```

### Key Files
- `tool-proxy.py` — proxy server (in LLM Server project, runs on dev Mac)
- No external dependencies — Python 3 stdlib only

### Limitations
- Non-streaming only (waits for full response to parse tool calls)
- Single tool call per response (no parallel tool calling)
- Gemma may occasionally respond with plain text instead of a tool call

## Performance Notes
- The RX 580s use Vulkan via MoltenVK (Metal translation layer) since native Vulkan isn't on macOS
- **Bottleneck is VRAM bandwidth**, not compute — each token reads the entire model from VRAM
- Layer-split mode is sequential: GPU0 → GPU1 → GPU2 pipeline (not parallel per-token)
- `--parallel N` lets N users share the GPUs efficiently (batched inference)
- **~7 t/s is near the hardware limit** for 32B Q4_K_M; **~14 t/s** for 14B Q4_K_M
- Models that fit entirely in VRAM run fast; any spill to CPU RAM causes massive slowdown
- **Critical build note**: NEVER enable both Metal and Vulkan backends simultaneously (`GGML_METAL=ON` + `GGML_VULKAN=ON`). Both claim the same physical GPU, causing VRAM conflicts, 156 graph splits, and ~1 t/s performance. Always build with `GGML_METAL=OFF`.

## Environment Variables Required
```
VK_ICD_FILENAMES=/usr/local/etc/vulkan/icd.d/MoltenVK_icd.json
```
This is set automatically by the startup script.

## Media Indexer

AI-powered media search for the Northwoods vault (173TB NAS at 10.10.11.185).

### How It Works
1. **Crawl**: Walks vault folders, registers media files (images, video, audio)
2. **Describe**: Sends images/keyframes to Gemma 3 12B vision for AI descriptions
3. **Store**: SQLite with FTS5 full-text search on descriptions, filenames, and folder tags
4. **Search**: HTTP API on port 8081 + MCP server for Continue in VS Code

### Architecture
- `media-indexer.py` — single Python file, no external dependencies (Python 3.9 stdlib only)
- Uses `ffmpeg`/`ffprobe` (static binaries at `/usr/local/bin/`) for metadata and keyframes
- SQLite database at `~/media-index/index.db` with WAL mode for concurrent access
- 2 concurrent LLM workers via ThreadPoolExecutor (leaves 1 `--parallel` slot for user queries)

### Commands
```bash
python3 ~/media-indexer.py index "/Volumes/Vault/Videos Vault/2024/Easter 2024"
python3 ~/media-indexer.py search "sunset landscape"
python3 ~/media-indexer.py status
python3 ~/media-indexer.py watch "/Volumes/Vault/Videos Vault"  # continuous
python3 ~/media-indexer.py serve 8081                           # HTTP search API
```

### Search API (port 8081)
- `GET /search?q=sunset&limit=20` — full-text search
- `GET /status` — indexing progress
- `GET /health` — health check
- `GET /folders` — list tracked folders

### MCP Server for Continue
- File: `media-search-mcp.py` (in LLM Server project)
- Tools: `search_media`, `media_status`, `list_indexed_folders`
- Configured in `~/.continue/config.yaml` under `mcpServers`
- Calls the HTTP search API on the Mac Pro

### Startup Order
1. Mount vault: `open "smb://10.10.11.185/Vault"` (uses Keychain credentials)
2. Start LLM: `~/start-llm-server.sh gemma` (Gemma 3 12B with vision, parallel=3)
3. Start media services: `~/start-media-services.sh` (search API on port 8081)
4. Start tool proxy (on dev Mac): `python3 tool-proxy.py &` (port 8083)
5. Optionally start watcher: `~/start-media-services.sh watch "/Volumes/Vault/Videos Vault"`

### Safe Reboot Procedure
**CRITICAL**: Active SMB mounts can cause kernel panics during reboot. Always follow this order:
1. Stop indexer: `~/stop-media-services.sh`
2. Stop LLM server: `~/stop-llm-server.sh`
3. Unmount NAS: `umount /Volumes/Vault`
4. Wait 10 seconds for GPU resources to release
5. Reboot: `sudo reboot`

---

## Research: Native Metal Multi-GPU for AMD (Feb 2026)

### Goal
Investigate whether native Metal could outperform Vulkan/MoltenVK by eliminating the Vulkan→Metal translation layer.

### Repository
- **Repo**: NorthwoodsCommunityChurch/llama.cpp, branch `metal-multi-gpu-amd`
- **Local clone on Mac Pro**: `~/llama-metal-mgpu`
- **Build command**: `cmake -B build-metal -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DCMAKE_BUILD_TYPE=Release && cmake --build build-metal --config Release -j$(sysctl -n hw.ncpu)`

### Result: Vulkan Wins — Native Metal Cannot Beat It

**Conclusion: The MoltenVK translation overhead is negligible (~1-2%). The ~7 t/s speed is a memory bandwidth limit, not a software limit. Stick with Vulkan.**

| Build | 32B Q4_K_M Speed | Notes |
|-------|------------------|-------|
| Vulkan (MoltenVK) | ~7 t/s | Production — already near hardware max |
| Metal (initial) | ~3.14 t/s | Broken — Metal shaders assumed Apple Silicon |
| Metal (dispatch fix) | ~3.14 t/s | Fixed threadgroup dispatch for AMD SIMD width |
| Metal (shader fix) | ~3.14 t/s | Fixed early-exit guards that caused garbage output |
| Metal (64-thread opt) | ~3.85 t/s | All 64 AMD threads utilized in Q_K kernels |
| Metal (float4 + n_cb=2) | ~4.86 t/s | vec4 memory loads + command buffer parallelism |

### Why ~7 t/s Is the Hardware Limit

The bottleneck is **VRAM bandwidth**, not compute or translation overhead:
- Each token requires reading the entire model (~20 GB) from VRAM
- In layer-split mode, GPUs work sequentially: GPU0 → GPU1 → GPU2
- GPU0 reads ~6 GB at 256 GB/s → ~24ms
- GPU1 reads ~6 GB → ~23ms
- GPU2 reads ~6 GB → ~24ms
- Plus CPU synchronization between 4 graph splits
- **Total: ~140ms per token → ~7 t/s**

No shader optimization can overcome this. The only ways to go faster:
- Smaller model (fewer GB to read per token)
- Faster GPUs (higher memory bandwidth)
- Smaller quantization (Q3_K trades quality for speed)

### Why Vulkan Shaders Are Faster Than Metal on AMD

The Vulkan Q4_K shader was designed for AMD GCN architecture. The Metal shaders were designed for Apple Silicon. Key differences:

| Aspect | Vulkan (AMD-optimized) | Metal (Apple Silicon design) |
|--------|----------------------|----------------------------|
| B-vector loads | `vec4` float (16-byte) | scalar `float` (4-byte) |
| Quant weight loads | `uint32` (4-byte) | `uint16` (2-byte) |
| Load instructions per block | ~12 | ~40 |
| Threads per quant block | 16 | 8 |
| Memory coalescing | Excellent (adjacent threads → adjacent memory) | Poor (threads scattered across blocks) |

The Vulkan shader issues 4x fewer memory load instructions for the same work, and those loads are wider (16-byte vs 4-byte), giving much better utilization of AMD's 128-bit memory channels.

### Bugs Fixed in Metal Backend (For Reference)

These bugs only affected the **Metal** backend on AMD GPUs. Vulkan does not have these issues.

**1. Dispatch threadgroup width (commit 7ce10b3)**
- `ggml-metal-ops.cpp` hardcoded SIMD width of 32 (Apple Silicon)
- AMD GPUs have SIMD width of 64
- Fix: replaced hardcoded `32` with `simd_w` variable throughout dispatch code

**2. Shader early-exit guards (commit 3d357c5)**
- Q_K shader kernels had `if (tiisg >= 32) return` guards
- On AMD (64-wide SIMD), threads 32-63 would exit immediately
- Some kernels had "contribute zero" guards that were also wrong for 64-wide
- This caused garbage output (@@@@) on the 1.5B model
- Fix: removed early-exit guards, replaced with dynamic `N_SIMDWIDTH`-based decomposition

**3. Q_K kernel 64-thread utilization (commit b2073c3)**
- Q_K kernels used fixed thread decomposition for 32-wide SIMD
- On 64-wide AMD, threads 32-63 processed duplicate work or were idle
- Fix: changed loop strides and thread decomposition to use `N_SIMDWIDTH` throughout
- Changed from `ib += 4` to `ib += N_SIMDWIDTH/8` (or `/16` for Q6_K)
- Improved Metal from 3.14 → 3.85 t/s (22% gain)

**4. Float4 vectorized loads (commit f199cd8)**
- Metal Q4_K kernel used 32 scalar float loads for B-vector data
- Vulkan equivalent uses 4 vec4 loads (4x fewer instructions)
- Fix: rewrote Q4_K and Q6_K kernels to use `float4` loads and `dot()` for sums
- Also set `n_cb=2` for discrete GPUs (command buffer parallelism)
- Improved Metal from 3.85 → 4.86 t/s (26% gain)

### Key Technical Learnings About AMD GPUs on macOS

**SIMD Width**: Apple Silicon = 32, AMD GCN (Polaris/RX 580) = 64. The Metal `N_SIMDWIDTH` compile-time constant controls this. `tiisg` (thread index in SIMD group) ranges 0-63 on AMD.

**Private vs Shared VRAM**: Discrete GPUs MUST use `MTLResourceStorageModePrivate` with blit-copy for model weights. Using Shared mode forces PCIe reads every inference pass (0.8 t/s → 40 t/s fix from GitHub issue #15228). This is already implemented in our Metal backend.

**Concurrent Dispatch Disabled on AMD**: `MTLDispatchTypeConcurrent` allows overlapping kernel execution but is disabled on AMD because `MTLBarrierScopeBuffers` does not properly flush AMD's L2 cache. MoltenVK also uses serial dispatch — this is NOT a performance factor.

**MoltenVK Translation Overhead Is Negligible**: MoltenVK converts Vulkan API calls to Metal API calls. The actual GPU execution is Metal underneath either way. The performance difference comes entirely from the shader algorithms, not the API path.

**nsg (SIMD Groups per Threadgroup)**: All Q_K mul_mv kernels use nsg=2, giving 128 threads per threadgroup on AMD (2 × 64). Vulkan on AMD GCN also uses 64 threads per workgroup (1 wavefront). Defined in `ggml-metal-impl.h` as `N_SG_Q4_K = 2`, etc.

**Three Metal Dispatch Paths**:
- Path 1 (ext kernels): for small-batch prompt eval (ne11=4-8)
- Path 2 (mm kernels): Apple Silicon only (uses `simdgroup_matrix_multiply`) — NEVER used on AMD
- Path 3 (mv kernels): default for AMD token generation

**Q4_K_M Model Composition**: Uses a mix of Q4_K blocks (385 tensors) and Q6_K blocks (65 tensors). Both kernel types matter for performance.

### Files Modified (Metal Backend)

All changes on branch `metal-multi-gpu-amd`:

- `ggml/src/ggml-metal/ggml-metal.metal` — shader kernels (Q2_K through Q6_K)
- `ggml/src/ggml-metal/ggml-metal-ops.cpp` — dispatch threadgroup widths
- `ggml/src/ggml-metal/ggml-metal.cpp` — n_cb=2 for discrete GPUs
- `ggml/src/ggml-metal/ggml-metal-impl.h` — N_SG constants (unchanged, for reference)
- `ggml/src/ggml-metal/ggml-metal-context.m` — concurrent dispatch logic (unchanged, for reference)
- `ggml/src/ggml-metal/ggml-metal-device.cpp` — pipeline creation (unchanged, for reference)
- `ggml/src/ggml-metal/ggml-metal-device.m` — device detection, SIMD width (unchanged, for reference)
