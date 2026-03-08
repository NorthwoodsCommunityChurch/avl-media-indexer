# Hardware

Current state of the Intel Mac Pro running Ubuntu Server. **CURRENT — Ubuntu Server 24.04.4 LTS.**

## Machine

| Component | Details |
|---|---|
| Model | 2019 Mac Pro (cheese grater tower, Mac Pro 7,1) |
| Hostname | `dc-macpro` |
| CPU | Intel Xeon W-3245 |
| RAM | 96 GB |
| GPUs | 2× AMD Radeon RX 580 (8 GB each) + 1× AMD Radeon Pro 580X (8 GB) = 24 GB VRAM |
| OS | Ubuntu Server 24.04.4 LTS |
| Kernel | `6.12.74-1-t2-noble` (T2-patched for Mac Pro hardware support) |
| IP | 10.10.11.157 (DHCP — may change; see Issue #21) |
| MAC (enp3s0, UP) | cc:2d:b7:07:2a:ca |
| MAC (enp4s0, DOWN) | cc:2d:b7:07:2a:ce |

> **Note on MACs**: The correct MAC for the primary interface (enp3s0) is `cc:2d:b7:07:2a:ca`. The value `e4:50:eb:ba:ce:6c` listed in SSH-ACCESS.md does not match any interface on this machine — SSH-ACCESS.md needs updating.

## SSH

```bash
ssh mediaadmin@10.10.11.157   # Direct IP
ssh llm-server                 # Named alias (if configured in ~/.ssh/config)
```

Key auth only. `mediaadmin` must be in `video` and `render` groups for GPU access:
```bash
sudo usermod -aG video,render mediaadmin
```

## GPU Layout

| PCIe Address | GPU | renderD | hwmon | Role |
|---|---|---|---|---|
| 07:00.0 | AMD Radeon Pro 580X (rev c0) | renderD128 | hwmon7 | Whisper (port 8092) |
| 0c:00.0 | AMD Radeon RX 580 (rev e7) | renderD129 | hwmon8 | Gemma (port 8090) |
| 0f:00.0 | AMD Radeon RX 580 (rev e7) | renderD130 | hwmon9 | Gemma (port 8091) |

**Vulkan device enumeration order is non-deterministic.** Vulkan indices change between reboots. The systemd services use `--device` flags set via a known-good assignment at setup time — verify with `llama-server --list-devices` if behavior seems wrong.

## GPU Driver

- **Mesa Gallium 25.2.8** (`radeonsi`, Polaris10) — open-source, no proprietary driver needed on Linux
- **VAAPI**: `mesa-va-drivers` — hardware video decode on all 3 GPUs
- **Vulkan**: `mesa-vulkan-drivers` — GPU compute for llama.cpp/whisper.cpp
- **VDPAU**: `mesa-vdpau-drivers`

## VAAPI Hardware Decode

All 3 GPUs support H.264, HEVC, MPEG2, VC1 via VAAPI. Used by `SceneDetectPool` in `media-indexer.py` for parallel video scene detection.

```bash
# Test VAAPI on each GPU
vainfo --display drm --device /dev/dri/renderD128  # Pro 580X
vainfo --display drm --device /dev/dri/renderD129  # RX 580 #1
vainfo --display drm --device /dev/dri/renderD130  # RX 580 #2
```

## Fan Control

### Why Max Fan Speed
Bursty GPU inference loads (35s vision encoder bursts) heat the GPUs faster than the automatic fan curve can respond. By the time fans ramp up, the load drops and the curve backs off — so temps ratchet up over sustained indexing runs. Fix: set RX 580 fans to max permanently.

### RX 580 Fans (Direct PWM)

Both RX 580s are set to max fan speed (3,200 RPM) by `gpu-fans-max.service` on boot.

| GPU | Fan | Max RPM | sysfs path |
|---|---|---|---|
| RX 580 (card1) | hwmon8/pwm1 | 3,200 | `/sys/class/drm/card1/device/hwmon/hwmon8/` |
| RX 580 (card2) | hwmon9/pwm1 | 3,200 | `/sys/class/drm/card2/device/hwmon/hwmon9/` |
| Pro 580X (card0) | **None — MPX module** | N/A | hwmon7 has no fan sensor. This GPU is in an Apple MPX module and is cooled entirely by the Mac Pro's 4 chassis fans — it has no fan of its own. |

Service: `gpu-fans-max.service`
Script: `/usr/local/bin/gpu-fans-max.sh`
Sets `pwm1_enable=1` (manual) and `pwm1=255` (max) on every GPU with a fan sensor.

To restore automatic fan control:
```bash
echo 2 | sudo tee /sys/class/drm/card1/device/hwmon/hwmon8/pwm1_enable
echo 2 | sudo tee /sys/class/drm/card2/device/hwmon/hwmon9/pwm1_enable
```

### Mac Pro Chassis Fans (T2 via applesmc)

The Mac Pro 2019 has 4 chassis fans controlled by the T2 chip, exposed via `applesmc` on Linux. These cool the Pro 580X MPX module (the Whisper GPU), which has no fans of its own.

sysfs base path: `/sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0A08:00/device:1f/APP0001:00/`

| Fan | Min | Max | Set to | Control |
|---|---|---|---|---|
| Fan 1 (exhaust) | 500 | 1,200 RPM | **T2 auto** | Left on automatic — exhaust only |
| Fan 2 | 500 | 2,500 RPM | **2,000 RPM** | Manual via `gpu-fans-max.sh` |
| Fan 3 | 500 | 2,500 RPM | **2,000 RPM** | Manual via `gpu-fans-max.sh` |
| Fan 4 | 500 | 2,500 RPM | **2,000 RPM** | Manual via `gpu-fans-max.sh` |

Fans 2–4 are set to 2,000 RPM manually on boot by `gpu-fans-max.sh`. Fan 1 (exhaust) is left under T2 automatic control.

To change the chassis fan target speed, edit `/usr/local/bin/gpu-fans-max.sh` and update the `echo 2000` lines in the applesmc section.

To restore all chassis fans to T2 automatic control:
```bash
BASE=/sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0A08:00/device:1f/APP0001:00
for fan in 2 3 4; do echo 0 | sudo tee $BASE/fan${fan}_manual; done
```

## Temperature Monitoring

```bash
# All GPU temps (millidegrees — divide by 1000 for °C)
ssh llm-server "cat /sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"

# RX 580 fan RPMs
ssh llm-server "cat /sys/class/drm/card1/device/hwmon/hwmon8/fan1_input && cat /sys/class/drm/card2/device/hwmon/hwmon9/fan1_input"

# Mac Pro chassis fan RPMs
ssh llm-server "for i in 1 2 3 4; do cat /sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0A08:00/device:1f/APP0001:00/fan\${i}_input; done"

# Full GPU status via gpu-monitor.py
ssh llm-server "python3 /home/mediaadmin/gpu-monitor.py"
```

## NAS Mount

| | |
|---|---|
| Source | `//10.10.11.185/Vault` |
| Mount point | `/mnt/vault/` |
| Type | CIFS (SMB), vers=3.0 |
| fstab flags | `_netdev,nofail` |
| Vaults | Videos Vault, Projects Vault, Weekend Service Vault, Stockfootage Vault |

Auto-mounted via `/etc/fstab`. Credentials embedded in fstab.
