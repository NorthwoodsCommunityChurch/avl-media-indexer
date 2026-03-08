> ⚠️ ARCHIVE — This document describes the macOS/MoltenVK phase of the project and is kept for historical reference only. Do NOT use for current operations. Current docs: `HARDWARE.md`, `SERVERS.md`

# Metal Multi-GPU AMD Project Context

This document contains everything needed to continue working on native Metal multi-GPU support for llama.cpp on the Mac Pro with AMD GPUs. Use this to bootstrap a fresh Claude Code chat.

## Goal

Replace Vulkan->MoltenVK->Metal translation chain (~7 tokens/sec with Qwen3-VL-32B-Q4_K_M) with direct Metal API calls across all 3 AMD GPUs for better performance.

## Hardware

- **Machine**: Mac Pro (Intel Xeon W-3245, 96GB RAM)
- **GPUs**: 2x AMD Radeon RX 580 (8GB each) + 1x AMD Radeon Pro 580X (8GB) = 24GB VRAM
- **GPU Architecture**: GCN4 (Polaris), 64-wide wavefronts (SIMD width = 64)
- **IP**: 10.10.11.157
- **SSH**: `ssh mediaadmin@10.10.11.157` (then `export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"` for cmake)

## Repository

- **Fork**: `NorthwoodsCommunityChurch/llama.cpp`
- **Branch**: `metal-multi-gpu-amd`
- **Current commit**: `4d0e376` (8 commits on branch)
- **Local clone**: `/tmp/nwcc-llama`
- **Mac Pro clone**: `~/llama-metal-mgpu`
- **Base**: forked from upstream llama.cpp at commit `079feab`

## Build Command (Mac Pro)

```bash
cd ~/llama-metal-mgpu
cmake -B build-metal -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build-metal --config Release -j$(sysctl -n hw.ncpu)
```

`-DGGML_METAL_EMBED_LIBRARY=ON` embeds the Metal shader source and compiles it at runtime with preprocessor macros (required for dynamic N_SIMDWIDTH).

## Models on Mac Pro (`~/models/`)

- `Qwen3VL-32B-Instruct-Q4_K_M.gguf` (18.4 GB) - main model
- `qwen2.5-1.5b-q4_k_m.gguf` (1 GB) - for quick testing
- `Qwen3VL-32B-Instruct-Q8_0.gguf` (34.8 GB) - too large for VRAM
- `mmproj-Qwen3VL-32B-Instruct-F16.gguf` - vision projector

## What's Working

1. **Multi-GPU device enumeration** - `MTLCopyAllDevices()` finds all 3 AMD GPUs
2. **Discrete GPU vtable** - separate backend vtable with NULL `buffer_from_host_ptr` to force Private (VRAM) buffer allocation via blit copy
3. **`buffer_from_host_ptr` caps** - set to false for discrete GPUs so model loader doesn't try to create shared buffers
4. **Cross-device buffer fix** - `cpy_tensor_async` returns false when source/destination devices differ
5. **Dynamic N_SIMDWIDTH** - shader preprocessor macro set to 64 for AMD, 32 for Apple Silicon
6. **simdgroup_reduction re-enabled** - AMD supports simd_sum/simd_max (confirmed by Metal API)
7. **simdgroup_mm disabled** - AMD doesn't have Apple's simdgroup_T8x8 matrix types
8. **flash_attn_ext_vec guarded** - entire section wrapped in `#if N_SIMDWIDTH <= 32` (only used with simdgroup_mm anyway)
9. **Metal shader compilation** - succeeds with N_SIMDWIDTH=64 on all 3 GPUs
10. **Model loading** - 32B Q4_K_M loads across 3 GPUs (6.2 + 6.0 + 6.2 GB GPU VRAM, only 417 MiB CPU)
11. **Graph splits** - only 4 splits (efficient multi-GPU scheduling)

## Current Bug: Garbage Output

Both 1.5B and 32B models produce garbage output (repeating `@@@@` or `""""""` characters). The root cause has been identified.

### Root Cause: Hardcoded SIMD Width of 32 in Thread Dispatch

In `ggml-metal-ops.cpp`, all `ggml_metal_encoder_dispatch_threadgroups()` calls use `32` as the threads-per-SIMD-group dimension:

```cpp
// Example: line 2055
ggml_metal_encoder_dispatch_threadgroups(enc, ..., 32, nsg, 1);
// This maps to: threadsPerThreadgroup: MTLSizeMake(32, nsg, 1)
```

With `nsg=2` (a function constant baked into the compiled pipeline):
- **Apple Silicon** (SIMD=32): `32 * 2 = 64` threads / 32 = **2 SIMD groups** (correct)
- **AMD** (SIMD=64): `32 * 2 = 64` threads / 64 = **1 SIMD group** (WRONG)

The kernel's `sgitg == 1` branch never runs. Half the computation is skipped, producing garbage.

There are **15+ locations** in ggml-metal-ops.cpp with hardcoded `32` in dispatch calls:

```
Line 1259: dispatch_threadgroups(enc, ..., 32, 1, 1)
Line 1618: dispatch_threadgroups(enc, ..., 32, nsg, 1)
Line 2055: dispatch_threadgroups(enc, ..., 32, nsg, 1)         # mul_mv_ext
Line 2101: dispatch_threadgroups(enc, ..., 128, 1, 1)          # mul_mm (not used on AMD)
Line 2145: dispatch_threadgroups(enc, ..., 32, nsg, 1)         # mul_mv quantized
Line 2147: dispatch_threadgroups(enc, ..., 32, nsg, 1)
Line 2296: dispatch_threadgroups(enc, ..., 128, 1, 1)          # mul_mm_id (not used on AMD)
Line 2350: dispatch_threadgroups(enc, ..., 32, nsg, 1)         # mul_mv_id
Line 2352: dispatch_threadgroups(enc, ..., 32, nsg, 1)
Line 2649: dispatch_threadgroups(enc, ..., 32, 1, 1)           # flash_attn_ext_pad
Line 2678: dispatch_threadgroups(enc, ..., 32, 1, 1)           # flash_attn_ext_blk
Line 2768: dispatch_threadgroups(enc, ..., 32, nsg, 1)         # flash_attn_ext
Line 2817: dispatch_threadgroups(enc, ..., 32, 1, 1)
Line 2917: dispatch_threadgroups(enc, ..., 32, nsg, 1)         # flash_attn_ext_vec
Line 2930: dispatch_threadgroups(enc, ..., 32, nsg, 1)
Line 2950: dispatch_threadgroups(enc, ..., 32*nwg, 1, 1)
Line 4356: const int nth = 32*pipeline.nsg;                     # count_equal
```

Also, line 2005 has `nypsg = 32/nxpsg` which should be `simd_width/nxpsg`.

### Fix Needed

Replace the `32` in dispatch calls with `ggml_metal_device_get_props(ctx->dev)->simd_width` (or a local variable). For AMD, this gives 64, making `64 * nsg` threads per threadgroup = nsg actual SIMD groups.

**Important notes:**
- Lines 2101 and 2296 use `128` and are for `mul_mm` (gated by `has_simdgroup_mm`), which is disabled for AMD. These may not need changing.
- Flash attention dispatch lines (2649, 2678, 2768, 2817, 2917, 2930, 2950) are also gated by `has_simdgroup_mm`. May not need changing but should be fixed for correctness.
- The `32` in grid-size calculations like `(ne11 + 31)/32` or `(ne01 + 63)/64` are tile/block sizes, NOT SIMD width. Leave those alone.

## Files Changed (7 files, 194 insertions, 47 deletions)

### `ggml/src/ggml-metal/ggml-metal.cpp` (multi-GPU + discrete vtable)
- `MTLCopyAllDevices()` for GPU enumeration (instead of just default device)
- Separate discrete-GPU vtable (`ggml_backend_metal_device_i_discrete`) with NULL `buffer_from_host_ptr`
- Device init selects vtable based on `has_unified_memory`
- `buffer_from_host_ptr` caps set to false for discrete GPUs

### `ggml/src/ggml-metal/ggml-metal-device.m` (device props + library compilation)
- `simd_width = 64` for discrete GPUs, `32` for Apple Silicon
- `has_simdgroup_reduction = true` for AMD (kept enabled)
- `has_simdgroup_mm = false` for AMD (Apple-specific matrix types)
- Skip pre-compiled `.metallib` when `simd_width != 32`
- Pass `N_SIMDWIDTH` preprocessor macro during runtime shader compilation
- Added `ggml_metal_library_get_simd_width()` accessor function
- SIMD width logged per device

### `ggml/src/ggml-metal/ggml-metal-device.h` (header additions)
- `int simd_width;` field in `ggml_metal_device_props` struct
- `int ggml_metal_library_get_simd_width(ggml_metal_library_t lib);` declaration

### `ggml/src/ggml-metal/ggml-metal.metal` (shader changes)
- `N_SIMDWIDTH` made overridable via `#ifndef N_SIMDWIDTH` guard
- Entire `flash_attn_ext_vec` section (typedef + all template instantiations) wrapped in `#if N_SIMDWIDTH <= 32`

### `ggml/src/ggml-metal/ggml-metal-ops.cpp` (dispatch + threadgroup sizing)
- All `int nth = 32; // SIMD width` replaced with `ggml_metal_device_get_props(ctx->dev)->simd_width`
- `nsg` calculation: `(nth + simd_w - 1) / simd_w` instead of `(nth + 31) / 32`
- cumsum shared memory: `simd_width * sizeof(float)` instead of `32 * sizeof(float)`
- **NOT YET FIXED**: dispatch calls still use hardcoded `32` (the root cause bug)

### `ggml/src/ggml-metal/ggml-metal-device.cpp` (pipeline compilation + shared memory)
- All `32*sizeof(float)` shared memory sizes replaced with `simd_width*sizeof(float)`
- SSM scan nsg calculation updated
- argmax, count_equal, solve_tri, mul_mv threshold updated

### `ggml/src/ggml-metal/ggml-metal-context.m` (cross-device copy fix)
- `cpy_tensor_async` returns false when source and destination devices differ

## Commit History

```
4d0e376 fix: guard ALL flash_attn_ext_vec instantiations for SIMD width > 32
be0a1bd fix: guard flash_attn_ext_vec dk32/dk96 for wide SIMD
04b0531 feat: dynamic SIMD width for AMD discrete GPU support
6c8e856 fix: disable simdgroup_reduction on AMD discrete GPUs
df09629 fix: prevent cross-device buffer access in cpy_tensor_async
fa43789 fix: set buffer_from_host_ptr cap to false for discrete GPUs
2a55e13 fix: use separate vtable for discrete GPUs to trigger VRAM allocation
682eb7d metal: add multi-GPU support for discrete AMD GPUs
```

Note: Commit `6c8e856` disabled simdgroup_reduction; commit `04b0531` re-enabled it. The net state is: simdgroup_reduction=true, simdgroup_mm=false.

## Key Technical Concepts

### GGML Metal Backend Architecture
- **Registry** -> **Device** -> **Backend**
- Each device has a vtable (`ggml_backend_device_i`) and properties (`ggml_backend_dev_props`)
- `supports_op()` checks capabilities: `MUL_MAT` needs `has_simdgroup_reduction`, `FLASH_ATTN_EXT` needs `has_simdgroup_mm`

### Private vs Shared Buffers
- **Apple Silicon (unified memory)**: Shared buffers (CPU+GPU same memory)
- **AMD discrete GPUs**: Private buffers (GPU VRAM only) + blit copy from CPU staging buffer
- Discrete GPU vtable has NULL `buffer_from_host_ptr` to force this path

### SIMD Width
- Apple Silicon: 32-wide SIMD groups
- AMD GCN4/Polaris (RX 580): 64-wide wavefronts
- Metal confirms via `threadExecutionWidth = 64` on compiled pipelines
- `simd_sum`/`simd_max` reduce across ALL threads in the SIMD group

### The nsg Problem (Current Bug)
- `nsg` = number of SIMD groups per threadgroup (function constant baked into pipeline)
- Dispatch must create exactly `nsg` SIMD groups: `threadsPerThreadgroup = simd_width * nsg`
- Currently: `threadsPerThreadgroup = 32 * nsg` (wrong for AMD)
- Kernel uses `sgitg` (SIMD group index) to split work across SIMD groups
- With fewer SIMD groups than expected, work assigned to missing groups is never computed

### Metal Library Compilation Paths
- `GGML_METAL_EMBED_LIBRARY=ON`: source embedded in binary, compiled at runtime with preprocessor macros (what we use)
- `GGML_METAL_EMBED_LIBRARY=OFF`: pre-compiled `.metallib`, source as fallback
- Our code skips pre-compiled metallib when `simd_width != 32` and falls through to source compilation

## Next Steps (in order)

1. **Fix dispatch threadgroup size** - Replace `32` with `simd_width` in all `dispatch_threadgroups()` calls in `ggml-metal-ops.cpp` (and the `nypsg = 32/nxpsg` on line 2005, and `nth = 32*pipeline.nsg` on line 4356)
2. **Commit + push + build on Mac Pro**
3. **Test 1.5B model** - verify correct text output (not garbage)
4. **Test 32B model multi-GPU** - verify correct output and measure speed
5. **Benchmark vs Vulkan** (~7 t/s baseline)
6. **If performance is good** - update `~/start-llm-server.sh` on Mac Pro to use Metal build

## Test Commands

```bash
# Quick test (1.5B, single GPU auto-selected)
./build-metal/bin/llama-completion -m ~/models/qwen2.5-1.5b-q4_k_m.gguf -p "The capital of France is" -n 20 --no-display-prompt

# 32B multi-GPU
./build-metal/bin/llama-completion -m ~/models/Qwen3VL-32B-Instruct-Q4_K_M.gguf -p "Explain quantum computing:" -n 50

# Server mode (for API testing)
./build-metal/bin/llama-server -m ~/models/Qwen3VL-32B-Instruct-Q4_K_M.gguf --port 8090 --host 0.0.0.0

# API test
curl -s http://10.10.11.157:8090/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"test","messages":[{"role":"user","content":"What is 2+2?"}],"max_tokens":10}'

# Debug logging (shows pipeline compilation details including threadExecutionWidth)
./build-metal/bin/llama-completion --log-verbosity 5 -m ~/models/qwen2.5-1.5b-q4_k_m.gguf -p "Hello" -n 1 2>&1 | grep "th_width"
```

## Verified GPU Capabilities (from Metal API)

```
GPU name:   AMD Radeon RX 580 / AMD Radeon Pro 580X
GPU family: MTLGPUFamilyCommon3 (3003)
simd width            = 64
simdgroup reduction   = true    (simd_sum, simd_max work)
simdgroup matrix mul. = false   (no simdgroup_T8x8 types)
has unified memory    = false   (discrete GPU)
has bfloat            = false
threadExecutionWidth  = 64      (confirmed per-pipeline)
```

## Performance So Far

| Model | Metric | Value | Note |
|-------|--------|-------|------|
| 1.5B Q4_K_M | Prompt eval | 107.65 t/s | via server API |
| 1.5B Q4_K_M | Generation | 40.98 t/s | via server API |
| 32B Q4_K_M | Prompt eval | 5.25 t/s | garbage output due to dispatch bug |
| 32B Q4_K_M | Generation | 4.33 t/s | garbage output due to dispatch bug |
| 32B Q4_K_M (Vulkan) | Generation | ~7 t/s | baseline to beat |

Note: Performance numbers with Metal are likely WRONG because the dispatch bug means only half the SIMD groups are computing. After fixing the dispatch, we should see different (hopefully better) numbers.
