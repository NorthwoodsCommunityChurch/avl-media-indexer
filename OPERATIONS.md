# Operations

Goal: 24/7 unattended operation. Once running, editors can rely on the system without manual intervention. If production focus shifts elsewhere for weeks, the pipeline should still be indexing new content and search should remain available.

## What Healthy Operation Looks Like

### Resource Utilization Baseline

| Resource | Expected During Indexing | Problem If... |
|---|---|---|
| GPU (vision) | Both RX 580s at high utilization simultaneously | Only one GPU active — serialization is present (Issue #24) |
| GPU (text gen) | Both RX 580s at ~100% simultaneously | One GPU idle — pipeline or assignment issue |
| CPU | High | Low with files in queue — prep thread or face workers stalled |
| Network (NAS) | High | Low with files in queue — NAS mount issue |
| Network high + GPU low | Brief spikes every 2–20 min | Sustained high network + near-zero GPU = scene detect timing out on long files (Issue #29) |

> **GPU serialization is not acceptable**: Both RX 580s should be running vision inference in parallel. If only one GPU is active during vision, that means one GPU is being wasted. This was a confirmed problem on Windows/WDDM (Issue #16) and has not yet been re-tested on Linux/Mesa. If you observe GPUs taking turns, treat it as an active problem — see Issue #24.

### Services — All Must Be Running

```bash
sudo systemctl status gemma0 gemma1 whisper media-indexer media-search
```
All five should show `active (running)`.

### Health Checks

```bash
# LLM servers (SSH in first — bind to 127.0.0.1 only):
ssh llm-server
curl http://localhost:8090/health   # Gemma RX 580 #1
curl http://localhost:8091/health   # Gemma RX 580 #2
curl http://localhost:8092/health   # Whisper Pro 580X

# Search API (reachable from network):
curl http://10.10.11.157:8081/health
curl http://10.10.11.157:8081/status
```
Expect `{"status": "ok"}` from all.

### Pipeline Is Actually Moving

A service can be `active (running)` and still be stalled. Verify actual progress:

```bash
# Live log — should show file activity
sudo journalctl -u media-indexer -f

# Full GPU status and activity
ssh llm-server "python3 /home/mediaadmin/gpu-monitor.py"
```

**Critical**: `/status` shows *registered* file counts, not *indexed* counts. A growing registered count with a flat indexed count means the pipeline is stalled.

---

## Silent Stall vs. Crash

This distinction is critical for 24/7 reliability:

| Type | What Happens | systemd Response |
|---|---|---|
| Crash | Service exits with error | systemd restarts automatically |
| Silent stall | Service running, no progress | **Nothing** — must be detected manually |

Silent stalls are the harder problem. Known causes:
- NAS mount dropped — ffmpeg hangs trying to read files from `/mnt/vault/`
- A bad or corrupt file causes inference to hang indefinitely
- GPU inference hung (no crash, just no response)

There is currently **no watchdog** that detects a silent stall. See Issue #26.

---

## Post-Reboot Checklist

After any reboot, Vulkan device indices are non-deterministic and can change. Wrong GPU assignment puts the wrong model on the wrong GPU, causing OOM crashes (Issue #2).

```bash
# 1. Verify all services running
sudo systemctl status gemma0 gemma1 whisper media-indexer media-search

# 2. Verify GPU assignments are correct
ssh llm-server "llama-server --list-devices"
# Gemma instances must be on RX 580, not Pro 580X
# If wrong, update --device flags in service files and restart

# 3. Verify NAS mounted
ssh llm-server "mount | grep vault"

# 4. Health check LLM servers
ssh llm-server "curl http://localhost:8090/health && curl http://localhost:8091/health && curl http://localhost:8092/health"

# 5. Confirm pipeline is making progress
sudo journalctl -u media-indexer -n 20
```

---

## Monitoring Commands

```bash
# GPU temps (millidegrees — divide by 1000 for °C)
ssh llm-server "cat /sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"

# RX 580 fan RPMs (expect ~3,200 — always at max)
ssh llm-server "cat /sys/class/drm/card1/device/hwmon/hwmon8/fan1_input && cat /sys/class/drm/card2/device/hwmon/hwmon9/fan1_input"

# Full GPU status
ssh llm-server "python3 /home/mediaadmin/gpu-monitor.py"

# Disk space (thumbnails + DB grow continuously)
ssh llm-server "df -h /home/mediaadmin/media-index"

# NAS still mounted
ssh llm-server "mount | grep vault"

# Recent indexer activity
sudo journalctl -u media-indexer -n 50

# All services at once
sudo systemctl status gemma0 gemma1 whisper media-indexer media-search dashboard-agent
```

---

## Known Reliability Gaps

These are not yet solved. See each issue for detail.

| Gap | Issue | Risk |
|---|---|---|
| No static IP — DHCP can change | #25 | SSH and search API unreachable after IP lease change |
| No watchdog for pipeline stall | #26 | Indexer silently stops making progress, nothing alerts |
| No disk space monitoring | #27 | DB + thumbnails fill disk, writes silently fail |
| No alerting system | #28 | Nothing notifies when services are down or stalled |
| VaultSearch shows "Processing" when offline | #22 | Can't distinguish busy from unreachable |
| RED/.R3D files not indexed | #20 | Significant Projects Vault content permanently unsearchable |
| Scene detection timeouts on long files | #29 | GPU busy ~10% of the time; NAS bandwidth wasted streaming files that have no cuts |

---

## What Broken Looks Like

| Symptom | Likely Cause | First Check |
|---|---|---|
| Search API unreachable from network | IP changed (DHCP) or `media-search` service down | `systemctl status media-search`; verify IP with `ip addr` |
| Services show `active` but nothing is indexing | Silent stall — NAS mount dropped or pipeline hung | `mount | grep vault`; `journalctl -u media-indexer -f` |
| OOM crashes on GPU | Wrong GPU assignment after reboot (Issue #2) or `-c 2048` (Issue #11) | `llama-server --list-devices`; check service file ctx value |
| Disk write failures | Disk full (Issue #27) | `df -h /home/mediaadmin/media-index` |
| GPU temps >85°C sustained | Fan not at max or `gpu-fans-max.service` not running | Check fan RPMs; `systemctl status gpu-fans-max` |
| VaultSearch shows "Processing" for everything | Machine offline, not busy (Issue #22) | Check if `/status` also fails — if so, machine is unreachable |
| Specific files never indexed | RED/.R3D format (Issue #20) or corrupt file | Check `indexer-run.log` for error patterns |
| Only one GPU active during vision inference | Serialization present on Linux — GPU being wasted | Run `gpu-monitor.py --benchmark`; investigate Issue #24 |
| High NAS network + mid CPU + very low GPU | Scene detect timing out on long files (Issue #29) — not a stall, but GPU throughput severely reduced | `journalctl -u media-indexer -n 50` — look for `Scene detect timed out` warnings |
