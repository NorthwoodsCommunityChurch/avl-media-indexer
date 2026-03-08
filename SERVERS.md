# Servers

**CURRENT — Ubuntu Server 24.04.4 LTS, systemd services.**

All servers run as systemd services under `mediaadmin`. They auto-start on boot.

## Services Overview

| Service | Description | Port | Binds to |
|---|---|---|---|
| `gemma0.service` | llama-server, Gemma 3 12B Q3_K_S, Vulkan1 (RX 580) | 8090 | 127.0.0.1 |
| `gemma1.service` | llama-server, Gemma 3 12B Q3_K_S, Vulkan2 (RX 580) | 8091 | 127.0.0.1 |
| `whisper.service` | whisper-server, large-v3-turbo, GPU 0 (Pro 580X) | 8092 | 127.0.0.1 |
| `media-indexer.service` | media-indexer.py watch mode (4 vaults) | — | — |
| `media-search.service` | media-indexer.py serve (search API) | 8081 | 0.0.0.0 |
| `dashboard-agent.service` | AVL Dashboard Agent (Python) | 49990 | — |
| `gpu-fans-max.service` | Sets RX 580 fans to max PWM on boot | — | — |

> **Important**: LLM servers (8090/8091/8092) bind to **127.0.0.1 only** — they are reachable only from the Mac Pro itself. The `media-indexer.py` runs on the same machine, so this works fine. The search API (8081) binds to 0.0.0.0 and is reachable from the network.

## Binary Paths

| Service | Binary |
|---|---|
| llama-server | `/home/mediaadmin/llama.cpp/build/bin/llama-server` |
| whisper-server | `/home/mediaadmin/whisper.cpp/build/bin/whisper-server` |
| media-indexer | `/home/mediaadmin/media-indexer.py` |
| dashboard-agent | `/home/mediaadmin/dashboard-agent.py` (venv: `/home/mediaadmin/agent-venv/`) |
| Models | `/home/mediaadmin/models/` |
| DB + thumbnails | `/home/mediaadmin/media-index/` |

## Service Management

```bash
# Status
sudo systemctl status gemma0
sudo systemctl status gemma1
sudo systemctl status whisper
sudo systemctl status media-indexer
sudo systemctl status media-search
sudo systemctl status dashboard-agent

# Start / Stop / Restart
sudo systemctl start gemma0
sudo systemctl stop gemma0
sudo systemctl restart gemma0

# View logs (last 50 lines)
sudo journalctl -u gemma0 -n 50
sudo journalctl -u media-indexer -n 50 --follow
```

## Health Checks

LLM servers respond to `/health` from localhost only:
```bash
# From the Mac Pro (SSH in first):
ssh mediaadmin@10.10.11.157
curl http://localhost:8090/health   # Gemma (RX 580 #1)
curl http://localhost:8091/health   # Gemma (RX 580 #2)
curl http://localhost:8092/health   # Whisper (Pro 580X)

# Search API is reachable from the network:
curl http://10.10.11.157:8081/health
curl http://10.10.11.157:8081/status
```

## LLM Server Startup Commands

The systemd services launch the servers approximately like this (for reference/debugging):

**Gemma instances (ports 8090 and 8091):**
```bash
# gemma0 (port 8090) — GGML_VK_VISIBLE_DEVICES=1 set in service file
GGML_VK_VISIBLE_DEVICES=1 /home/mediaadmin/llama.cpp/build/bin/llama-server \
  --host 127.0.0.1 \
  --port 8090 \
  -m /home/mediaadmin/models/gemma-3-12b-it-Q3_K_S.gguf \
  --mmproj /home/mediaadmin/models/mmproj-gemma-3-12b-it-f16.gguf \
  --device Vulkan0 \
  -ngl 99 \
  -c 1024 \
  --parallel 1

# gemma1 (port 8091) — GGML_VK_VISIBLE_DEVICES=2 set in service file
GGML_VK_VISIBLE_DEVICES=2 /home/mediaadmin/llama.cpp/build/bin/llama-server \
  --host 127.0.0.1 \
  --port 8091 \
  -m /home/mediaadmin/models/gemma-3-12b-it-Q3_K_S.gguf \
  --mmproj /home/mediaadmin/models/mmproj-gemma-3-12b-it-f16.gguf \
  --device Vulkan0 \
  -ngl 99 \
  -c 1024 \
  --parallel 1
```

> **Critical**: `GGML_VK_VISIBLE_DEVICES` must be set so each server only sees its own GPU. Without it, the CLIP/SigLIP vision encoder defaults to Vulkan0 (Pro 580X) regardless of `--device`, causing both Gemma instances to compete for the Pro 580X — killing parallel throughput. With it, each process sees exactly one GPU and both LLM + vision encoder use it. See Issue #24.

**Whisper instance (port 8092):**
```bash
/home/mediaadmin/whisper.cpp/build/bin/whisper-server \
  --host 127.0.0.1 \
  --port 8092 \
  -m /home/mediaadmin/models/ggml-large-v3-turbo.bin \
  --device 0
```

> **Note on Vulkan indices**: The `--device VulkanN` values in the gemma service files were set when the services were configured. If GPUs are reassigned after a reboot (indices are non-deterministic), verify with `llama-server --list-devices` and update the service files accordingly.

## Dashboard Agent

- **Purpose**: Python port of the macOS Swift AVL Dashboard Agent
- **Endpoint**: `/status` — compatible with AVL Dashboard macOS app
- **Discovery**: mDNS via `_computerdash._tcp` for auto-discovery
- **Port**: 49990
- **Service**: `dashboard-agent.service`

## NAS Mount

The NAS is auto-mounted at `/mnt/vault/` via `/etc/fstab` with `_netdev,nofail`. The 4 vaults being watched by `media-indexer.service`:
- `/mnt/vault/Videos Vault`
- `/mnt/vault/Projects Vault`
- `/mnt/vault/Weekend Service Vault`
- `/mnt/vault/Stockfootage Vault`

Check if mounted:
```bash
ssh llm-server "mount | grep vault"
```

## Reboot Procedure

```bash
# Stop services gracefully
ssh llm-server "sudo systemctl stop media-indexer media-search gemma0 gemma1 whisper"

# Reboot
ssh llm-server "sudo reboot"

# Services auto-start on boot via systemd
# After ~2 minutes, verify:
curl http://10.10.11.157:8081/health
```

## Log Locations

| Service | Log command |
|---|---|
| All systemd services | `sudo journalctl -u SERVICE -n 50` |
| Media indexer run log | `/home/mediaadmin/media-index/indexer-run.log` |
| Thumbnails | `/home/mediaadmin/media-index/thumbnails/` |
| Face thumbnails | `/home/mediaadmin/media-index/face-thumbnails/` |
