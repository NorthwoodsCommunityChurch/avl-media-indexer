> ⚠️ ARCHIVE — This document describes a previous phase of the project and is kept for historical reference only. Do NOT use for current operations. Current doc: `HARDWARE.md`

# Hardware Setup

## Machine
- **Name**: DC---Pro (Mac Pro)
- **CPU**: Intel Xeon W-3245
- **RAM**: 96 GB
- **GPUs**: 2x AMD Radeon RX 580 (8 GB each) + 1x AMD Radeon Pro 580X (8 GB) = 24 GB VRAM
- **OS**: Ubuntu Server 24.04.4 LTS
- **IP**: 10.10.11.157 (DHCP — may have changed, see Issue #21)
- **MACs**: e4:50:eb:ba:ce:6c (per SSH-ACCESS.md), cc:2d:b7:07:2a:ca (enp3s0)
- **SSH**: `ssh mediaadmin@10.10.11.157` (or `ssh llm-server`)

## Current State (2026-02-22)

Machine is online at 10.10.11.157. All 3 GPUs detected. Network came back after a full power cycle (previous WoL attempt left network down — see Issue #21).

## Ubuntu Setup

The machine was migrated from Windows/Atlas OS to Ubuntu Server 24.04.4 LTS in Feb 2026. Kernel: `6.12.74-1-t2-noble` (T2-patched for Mac Pro support).

### Key Packages
- **GPU driver**: Mesa Gallium 25.2.8 (`radeonsi`, Polaris10) — open-source, no proprietary driver needed on Linux
- **VAAPI**: `mesa-va-drivers` — hardware video decode on all 3 GPUs
- **Vulkan**: `mesa-vulkan-drivers` — GPU compute for llama.cpp/whisper.cpp
- **VDPAU**: `mesa-vdpau-drivers`

### User Groups
`mediaadmin` must be in `video` and `render` groups for GPU access:
```bash
sudo usermod -aG video,render mediaadmin
```

### PCIe GPU Layout
```
07:00.0 — AMD Radeon Pro 580X (rev c0)  → renderD128, card0, hwmon7
0c:00.0 — AMD Radeon RX 580 (rev e7)    → renderD129, card1, hwmon8
0f:00.0 — AMD Radeon RX 580 (rev e7)    → renderD130, card2, hwmon9
```

### VAAPI Hardware Decode
All 3 GPUs support VAAPI decode (H.264, HEVC, MPEG2, VC1). Used by `SceneDetectPool` in `media-indexer.py` for parallel scene detection:
```bash
# Test VAAPI on each GPU
vainfo --display drm --device /dev/dri/renderD128  # Pro 580X
vainfo --display drm --device /dev/dri/renderD129  # RX 580
vainfo --display drm --device /dev/dri/renderD130  # RX 580
```

## Fan Control

### Problem
Bursty GPU inference loads (35s vision encoder bursts) heat the GPUs faster than the automatic fan curve can respond. By the time fans ramp up, the load drops and the curve backs off — so temps ratchet up over sustained indexing runs.

### Solution: RX 580 Fans at Max
Both RX 580s are set to max fan speed (3,200 RPM) permanently. The Pro 580X has no fan sensor — it's cooled by the Mac Pro chassis fans.

| GPU | Fan Control | Max RPM | sysfs path |
|-----|-------------|---------|------------|
| Pro 580X (card0) | Mac Pro chassis fans (T2) | N/A | hwmon7 (no fan sensor) |
| RX 580 (card1) | Direct PWM control | 3,200 | hwmon8/pwm1 |
| RX 580 (card2) | Direct PWM control | 3,200 | hwmon9/pwm1 |

**Systemd service** (`gpu-fans-max.service`) runs on boot:
- Script: `/usr/local/bin/gpu-fans-max.sh`
- Sets `pwm1_enable=1` (manual) and `pwm1=255` (max) on every GPU that has a fan sensor
- Enabled: `sudo systemctl enable gpu-fans-max.service`

To restore automatic fan control: `echo 2 | sudo tee /sys/class/drm/card1/device/hwmon/hwmon8/pwm1_enable`

### Mac Pro Chassis Fans (T2 via applesmc)
The Mac Pro 2019 has 4 chassis fans controlled by the T2 chip, exposed via `applesmc` on Linux.

| Fan | Current | Min | Max | sysfs path |
|-----|---------|-----|-----|------------|
| Fan 1 (exhaust) | 500 RPM | 500 | 1,200 | APP0001:00/fan1_input |
| Fan 2 | 1,500 RPM | 500 | 2,500 | APP0001:00/fan2_input |
| Fan 3 | 1,500 RPM | 500 | 2,500 | APP0001:00/fan3_input |
| Fan 4 | 1,500 RPM | 500 | 2,500 | APP0001:00/fan4_input |

Fans 2-4 were manually set to 1,500 RPM. The sysfs base path is:
```
/sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0A08:00/device:1f/APP0001:00/
```

### Monitoring Temps
```bash
# All GPU temps (millidegrees — divide by 1000)
ssh llm-server "cat /sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"

# RX 580 fan RPMs
ssh llm-server "cat /sys/class/drm/card1/device/hwmon/hwmon8/fan1_input && cat /sys/class/drm/card2/device/hwmon/hwmon9/fan1_input"

# Mac Pro chassis fan RPMs
ssh llm-server "for i in 1 2 3 4; do cat /sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0A08:00/device:1f/APP0001:00/fan\${i}_input; done"
```

## Windows / Atlas OS (Historical)

> **Note:** The sections below are from the previous Windows/Atlas OS install. Kept for reference since the Vulkan/GPU research findings still apply, but the Windows-specific paths, batch files, and scheduled tasks are no longer relevant.

The Mac Pro previously ran Atlas OS for native Vulkan GPU access. This was replaced with Ubuntu to simplify management and avoid Windows-specific SSH/PowerShell issues (see Issues #3, #4, #8, #10, #12).

### Why Windows (originally)
- **MoltenVK on macOS serializes multi-GPU commands** at the system level — even separate processes on separate physical GPUs. This drops throughput from 12 t/s to 1.73 t/s when a second GPU is active.
- **Windows has native AMD Vulkan drivers** — each GPU gets its own independent Vulkan command queue with no translation layer.
- See `GPU-PIPELINE-PLAN.md` for the full analysis of the macOS-to-Windows journey.

## GPU Driver: BootCampDrivers.com (Windows — Historical)

The Pro 580X uses Apple's OEM subsystem ID (`SUBSYS_0206106B`). AMD's standard Adrenalin driver blocks this at **two levels**:
1. **INF level**: `ExcludeID` entries reject Apple subsystem IDs (bypassable)
2. **Kernel level**: `amdkmdag.sys` has an internal hardware whitelist (NOT bypassable)

**Solution**: Use [BootCampDrivers.com](https://www.bootcampdrivers.com/) Blue (Enterprise) Edition — Adrenalin 24.9.1 for Polaris. This driver has Apple OEM support built into both the INF and kernel driver.

**Install steps** (already done):
1. Download Blue Edition from `https://nc2.tomas-g.de/index.php/s/jZQ5LpemCgqnArq/download`
2. Extract with 7-Zip (the .exe isn't compatible with Windows 11 directly)
3. Trust the signing cert: extract from .cat file → import to `Cert:\LocalMachine\TrustedPublisher`
4. Install via: `pnputil /add-driver u0407052.inf /install`
5. All 3 GPUs show as Vulkan 1.3.260 with AMD proprietary driver

## Vulkan Device Mapping

**Vulkan device enumeration order is NON-DETERMINISTIC on this system.** The indices (Vulkan0, Vulkan1, Vulkan2) can change between reboots and even between process launches. Do NOT hardcode Vulkan device indices in batch files.

| GPU Name | Task | Port |
|----------|------|------|
| Radeon RX 580 (x2) | Gemma | 8090, 8091 |
| AMD Radeon Pro 580X | Whisper | 8092 |

**Solution**: `start-servers.ps1` queries `--list-devices` at startup and assigns GPUs by name, not index. This is the only reliable way to assign GPUs.

**Verify with**: `C:\Users\mediaadmin\llama.cpp\llama-server.exe --list-devices`

## SSH Access

Passwordless SSH is configured from the development Mac to the Mac Pro. See `SSH-ACCESS.md` for the centralized SSH guide.

### Connection
```bash
ssh llm-server                    # Named host alias
ssh mediaadmin@10.10.11.157       # Direct IP
```

### Running Remote Commands (Ubuntu)
```bash
# Single command
ssh llm-server "hostname && uptime"

# Check GPUs
ssh llm-server "lspci | grep -i vga"

# Deploy files
scp localfile.py llm-server:/home/mediaadmin/remotefile.py
```

## Windows Gotchas (Historical)
> These applied to the previous Windows/Atlas OS install. Kept for reference.

- **No heredoc over SSH**: Windows cmd.exe doesn't support `<<`. Use `scp` to deploy files, or write via PowerShell.
- **Admin SSH keys**: Go in `C:\ProgramData\ssh\administrators_authorized_keys` (not `~/.ssh/`)
- **Atlas OS strips WMIC**: Use PowerShell `Get-WmiObject` / `Get-PnpDevice` instead
- **winget source broken**: May need `winget source reset` before use
- **7-Zip available**: via Windows Apps path, use in PowerShell
