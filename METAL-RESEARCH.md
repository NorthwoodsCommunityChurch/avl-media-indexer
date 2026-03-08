> ⚠️ ARCHIVE — This document describes the macOS/Metal research phase and is kept for historical reference only. Do NOT use for current operations. Current docs: `HARDWARE.md`, `SERVERS.md`

# Research: Native Metal Multi-GPU for AMD (Feb 2026)

## Goal
Investigate whether native Metal could outperform Vulkan/MoltenVK by eliminating the Vulkan→Metal translation layer.

## Repository
- **Repo**: NorthwoodsCommunityChurch/llama.cpp, branch `metal-multi-gpu-amd`
- **Local clone on Mac Pro**: `~/llama-metal-mgpu`
- **Build command**: `cmake -B build-metal -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DCMAKE_BUILD_TYPE=Release && cmake --build build-metal --config Release -j$(sysctl -n hw.ncpu)`

## Result: Vulkan Wins — Native Metal Cannot Beat It

**Conclusion: The MoltenVK translation overhead is negligible (~1-2%). The ~7 t/s speed is a memory bandwidth limit, not a software limit. Stick with Vulkan.**

| Build | 32B Q4_K_M Speed | Notes |
|-------|------------------|-------|
| Vulkan (MoltenVK) | ~7 t/s | Production — already near hardware max |
| Metal (initial) | ~3.14 t/s | Broken — Metal shaders assumed Apple Silicon |
| Metal (dispatch fix) | ~3.14 t/s | Fixed threadgroup dispatch for AMD SIMD width |
| Metal (shader fix) | ~3.14 t/s | Fixed early-exit guards that caused garbage output |
| Metal (64-thread opt) | ~3.85 t/s | All 64 AMD threads utilized in Q_K kernels |
| Metal (float4 + n_cb=2) | ~4.86 t/s | vec4 memory loads + command buffer parallelism |

## Why ~7 t/s Is the Hardware Limit

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

## Why Vulkan Shaders Are Faster Than Metal on AMD

The Vulkan Q4_K shader was designed for AMD GCN architecture. The Metal shaders were designed for Apple Silicon. Key differences:

| Aspect | Vulkan (AMD-optimized) | Metal (Apple Silicon design) |
|--------|----------------------|----------------------------|
| B-vector loads | `vec4` float (16-byte) | scalar `float` (4-byte) |
| Quant weight loads | `uint32` (4-byte) | `uint16` (2-byte) |
| Load instructions per block | ~12 | ~40 |
| Threads per quant block | 16 | 8 |
| Memory coalescing | Excellent (adjacent threads → adjacent memory) | Poor (threads scattered across blocks) |

The Vulkan shader issues 4x fewer memory load instructions for the same work, and those loads are wider (16-byte vs 4-byte), giving much better utilization of AMD's 128-bit memory channels.

## Bugs Fixed in Metal Backend (For Reference)

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

## Key Technical Learnings About AMD GPUs on macOS

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

## Files Modified (Metal Backend)

All changes on branch `metal-multi-gpu-amd`:

- `ggml/src/ggml-metal/ggml-metal.metal` — shader kernels (Q2_K through Q6_K)
- `ggml/src/ggml-metal/ggml-metal-ops.cpp` — dispatch threadgroup widths
- `ggml/src/ggml-metal/ggml-metal.cpp` — n_cb=2 for discrete GPUs
- `ggml/src/ggml-metal/ggml-metal-impl.h` — N_SG constants (unchanged, for reference)
- `ggml/src/ggml-metal/ggml-metal-context.m` — concurrent dispatch logic (unchanged, for reference)
- `ggml/src/ggml-metal/ggml-metal-device.cpp` — pipeline creation (unchanged, for reference)
- `ggml/src/ggml-metal/ggml-metal-device.m` — device detection, SIMD width (unchanged, for reference)
