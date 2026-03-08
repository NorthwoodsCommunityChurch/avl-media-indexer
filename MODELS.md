# Models

All model files stored at `/home/mediaadmin/models/` on the Mac Pro (Ubuntu).

## Active: Gemma 3 12B Q3_K_S (currently running via systemd)

Two instances of this model run as systemd services (`gemma0.service` on port 8090, `gemma1.service` on port 8091), one per RX 580.

- **File**: `gemma-3-12b-it-Q3_K_S.gguf` (5.46 GB) from bartowski
- **Vision encoder**: `mmproj-gemma-3-12b-it-f16.gguf` (815 MB) — same file used by both instances
- **Speed**: ~16-20 t/s per GPU
- **VRAM**: ~6.9 GB per GPU (model + vision encoder + KV cache at ctx 1024)
- **Context**: Must use `-c 1024` (not 2048). At ctx 2048, KV cache + compute buffers OOM during vision inference (Issue #11).
- **Purpose**: One per RX 580 for parallel indexing throughput

## Available: Gemma 3 12B Q4_K_M (vision, single-server mode)

- **File**: `gemma-3-12b-it-Q4_K_M.gguf` (6.8 GB) from bartowski
- **Vision encoder**: `mmproj-gemma-3-12b-it-f16.gguf` (815 MB) — same encoder as Q3_K_S
- **Speed**: ~13 t/s single-user (text and vision)
- **VRAM**: ~8 GB across 3 GPUs (leaves ~16 GB free)
- **Parallel**: Runs with `--parallel 3` for indexer + user queries
- **Upgrade opportunity**: Running one instance per RX 580 at `-c 1024` (matching current Q3_K_S config) may fit within 8 GB per-GPU VRAM. If confirmed, this replaces Q3_K_S on both RX 580s for better quantization quality at the same speed. **Untested** — needs OOM verification before deploying.

## Available: Qwen3-14B Q4_K_M (text-only, recommended for interactive use)

- **File**: `Qwen3-14B-Q4_K_M.gguf` (8.4 GB)
- **Speed**: ~14 t/s single-user, ~3.4 t/s per-user with 3 concurrent
- **VRAM**: ~8.2 GB across 3 GPUs (leaves ~16 GB free)
- **Parallel**: Runs with `--parallel 3` for multi-user/agentic use
- **Thinking mode**: Qwen3 has built-in reasoning (thinking mode ON by default). Add `/no_think` to prompts for direct answers
- Text-only (no vision)

## Available: Qwen3-VL-32B Q4_K_M (vision, large)

- **File**: `Qwen3VL-32B-Instruct-Q4_K_M.gguf` (19.8 GB)
- **Vision encoder**: `mmproj-Qwen3VL-32B-Instruct-F16.gguf`
- **Speed**: ~7 t/s single-user
- **VRAM**: ~20 GB across 3 GPUs (tight fit)
- Vision-capable

## Available: Qwen3-VL-8B Q4_K_M (tested, not recommended for vision)

- **File**: `Qwen3VL-8B-Instruct-Q4_K_M.gguf` (4.7 GB)
- **Vision encoder**: `mmproj-Qwen3VL-8B-Instruct-F16.gguf` (1.1 GB)
- **Speed**: ~21.6 t/s text generation (fastest model tested)
- **Tool calling**: Works natively with `--jinja` flag — no proxy needed
- **Vision**: Unusable on AMD GPUs — encoder takes 87s for 640px image, full-size images timeout
- **Verdict**: Great for text/tool calling, but vision is broken on Vulkan/AMD. Use Gemma for vision tasks.

## Available: Qwen3-VL-32B Q8_0 (not recommended)

- **File**: `Qwen3VL-32B-Instruct-Q8_0.gguf` (34.8 GB)
- **Speed**: ~0.14 t/s — spills ~11 GB to CPU RAM, 44x slower

## Whisper Model

- **File**: `ggml-large-v3-turbo.bin`
- **Service**: `whisper.service` (port 8092, Pro 580X)

## Downloading Models

Models can be downloaded via `huggingface-cli` or direct `wget`/`curl`. Example for the active Gemma model:
```bash
cd /home/mediaadmin/models/
# Gemma 3 12B Q3_K_S
wget "https://huggingface.co/bartowski/google_gemma-3-12b-it-GGUF/resolve/main/gemma-3-12b-it-Q3_K_S.gguf"
# Vision encoder (shared across Gemma variants)
wget "https://huggingface.co/bartowski/google_gemma-3-12b-it-GGUF/resolve/main/mmproj-gemma-3-12b-it-f16.gguf"
```
