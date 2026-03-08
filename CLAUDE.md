# LLM Server

## Security

See [SECURITY.md](SECURITY.md) for security findings from the 2026-03-01 review.

Local AI-powered media search running on the Intel Mac Pro (hostname: `dc-macpro`) at 10.10.11.157 — Ubuntu Server 24.04.4 LTS with 3 AMD GPUs.

## Machine Quick Reference

| | |
|---|---|
| SSH | `ssh mediaadmin@10.10.11.157` (or `ssh llm-server` if alias configured) |
| Hostname | `dc-macpro` |
| OS | Ubuntu Server 24.04.4 LTS |
| IP | 10.10.11.157 (static — netplan configured, Issue #25 fixed) |
| MAC (enp3s0) | cc:2d:b7:07:2a:ca |
| NAS | 10.10.11.185 — 173 TB, mounted at `/mnt/vault/` |
| Search API | http://10.10.11.157:8081 |
| Models | `/home/mediaadmin/models/` |

## Before You Start

1. **Check ISSUES.md first** — 21+ issues documented with root causes and fixes
2. **LLM servers bind to 127.0.0.1** — ports 8090/8091/8092 are NOT reachable from the network
3. **WoL does not work** — always use a full power cycle; never attempt Wake-on-LAN (Issue #21)
4. **Don't trust VaultSearch file counts** — `/status` shows registered files, not indexed files

## Doc Map

| I need to... | Read... |
|---|---|
| Machine, OS, network, SSH, GPU layout, fans | `HARDWARE.md` |
| What's running, systemd services, start/stop/restart, health | `SERVERS.md` |
| LLM models, download, VRAM, which model to use | `MODELS.md` |
| Scene detection, video analysis, face detection, pipeline (current) | `PIPELINE.md` |
| Desired pipeline redesign — task queues, workers, architecture | `PIPELINE-REDESIGN.md` |
| Debug a problem or check if it's already been solved | `ISSUES.md` |
| Project goals, requirements, GPU serialization research | `PRD.md` |
| Monitor system health, 24/7 reliability, watchdog, recovery | `OPERATIONS.md` |
| Configure Continue / VS Code tool proxy | `TOOL-PROXY.md` |
| How we got here (macOS → Windows → Ubuntu), key decisions | `HISTORY.md` |

## Agent Team Rules

- **NEVER shut down Alice, Ben, or Clara** — do not send shutdown requests at the end of a session. Leave all teammates running. The user wants agents to persist between tasks.
- Always use model `claude-sonnet-4-6` for lead and all teammates
- Always spawn exactly 3 teammates named **Alice**, **Ben**, and **Clara**

## Critical Rules

- **`--parallel 1` only** — two slots per GPU causes VRAM contention and crashes on 8 GB cards
- **`-c 1024` for indexer** — at 2048, vision inference OOMs on 8 GB RX 580 cards (Issue #11)
- **Vulkan device order is non-deterministic** — systemd services handle GPU assignment; never hardcode indices
- **Vision inference is fully parallel on Linux** — GGML_VK_VISIBLE_DEVICES fix confirmed 0.99x parallel; all 3 GPUs run independently (Issue #24 FIXED)
- **LLM servers bind to 127.0.0.1** — not accessible from the network; search API (8081) binds to 0.0.0.0

## Archive / Historical Docs

These files describe earlier phases. Do NOT use for current operations:

| File | Phase |
|---|---|
| `HARDWARE-SETUP.md` | Replaced by `HARDWARE.md` |
| `MEDIA-INDEXER.md` | Replaced by `PIPELINE.md` |
| `HISTORY.md` | Full macOS → Windows → Ubuntu migration story |
| `GPU-PIPELINE-PLAN.md` | macOS + Windows GPU research |
| `METAL-MULTI-GPU-CONTEXT.md`, `METAL-RESEARCH.md` | macOS MoltenVK research |
| `start-all.py`, `start-all-wrapper.bat` | Windows-only server orchestration |
| `start-indexer-gpus.sh`, `start-llm-server.sh` | macOS + MoltenVK scripts |
