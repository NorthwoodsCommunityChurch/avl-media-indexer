#!/usr/bin/env python3
"""
Clean GPU parallelism diagnostic.

Run with indexer STOPPED. Tests:
  1. Text-only baseline: GPU0 alone, GPU1 alone
  2. Text-only simultaneous: both GPUs at once
  3. Vision baseline: GPU0 alone, GPU1 alone
  4. Vision simultaneous: both GPUs at once

While each test runs, polls GPU busy% from sysfs to show whether
both GPUs are actually active or taking turns.

Usage (on the server):
  python3 gpu-parallel-test.py
"""

import base64
import json
import struct
import sys
import threading
import time
import urllib.request
import zlib

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
GPU0_PORT = 8090   # gemma0 — Vulkan1 — RX 580 (0c:00.0)
GPU1_PORT = 8091   # gemma1 — Vulkan2 — RX 580 (0f:00.0)

GPU_BUSY_PATHS = {
    "GPU0 (RX580 0c)": "/sys/bus/pci/devices/0000:0c:00.0/gpu_busy_percent",
    "GPU1 (RX580 0f)": "/sys/bus/pci/devices/0000:0f:00.0/gpu_busy_percent",
    "GPU2 (580X 07)":  "/sys/bus/pci/devices/0000:07:00.0/gpu_busy_percent",
}

REQUEST_TIMEOUT = 300   # seconds


# ---------------------------------------------------------------------------
# Minimal synthetic media
# ---------------------------------------------------------------------------

def _make_png_b64():
    w, h = 16, 16
    sig  = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    raw  = (b"\x00" + b"\x80" * w) * h
    idat = _chunk(b"IDAT", zlib.compress(raw, 9))
    iend = _chunk(b"IEND", b"")
    return base64.b64encode(sig + ihdr + idat + iend).decode()

def _chunk(name, data):
    crc = zlib.crc32(name + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + name + data + crc.to_bytes(4, "big")

PNG_B64 = _make_png_b64()


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------

def post_json(port, body_dict):
    body = json.dumps(body_dict).encode()
    req  = urllib.request.Request(
        f"http://{HOST}:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        r.read()
    return time.time() - t0


def text_request(port):
    """Text-only inference — no image."""
    return post_json(port, {
        "messages": [{"role": "user", "content": "Reply with only the number 42."}],
        "max_tokens": 10,
        "cache_prompt": False,
    })


def vision_request(port):
    """Vision inference — 16×16 gray PNG."""
    return post_json(port, {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
                {"type": "text", "text": "One word: what color is this image?"},
            ],
        }],
        "max_tokens": 5,
        "cache_prompt": False,
    })


# ---------------------------------------------------------------------------
# GPU utilization monitor
# ---------------------------------------------------------------------------

_monitor_running = False
_monitor_log = []   # list of (timestamp, {label: pct})


def _read_busy():
    out = {}
    for label, path in GPU_BUSY_PATHS.items():
        try:
            with open(path) as f:
                out[label] = int(f.read().strip())
        except Exception:
            out[label] = -1
    return out


def start_monitor():
    global _monitor_running, _monitor_log
    _monitor_running = True
    _monitor_log = []

    def loop():
        while _monitor_running:
            _monitor_log.append((time.time(), _read_busy()))
            time.sleep(0.25)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def stop_monitor():
    global _monitor_running
    _monitor_running = False


def print_monitor_summary(label):
    if not _monitor_log:
        print("  (no monitor data)")
        return

    # Find peak utilization for each GPU during the test
    peaks = {lbl: 0 for lbl in GPU_BUSY_PATHS}
    avgs  = {lbl: [] for lbl in GPU_BUSY_PATHS}
    for _, snap in _monitor_log:
        for lbl, pct in snap.items():
            if pct >= 0:
                peaks[lbl] = max(peaks[lbl], pct)
                avgs[lbl].append(pct)

    print(f"  GPU utilization during '{label}':")
    for lbl in GPU_BUSY_PATHS:
        vals = avgs[lbl]
        avg = sum(vals) / len(vals) if vals else 0
        peak = peaks[lbl]
        bar = "█" * int(peak / 5) + "░" * (20 - int(peak / 5))
        print(f"    {lbl}: avg={avg:4.0f}%  peak={peak:3d}%  [{bar}]")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_solo(label, fn, port):
    print(f"\n  {label}... ", end="", flush=True)
    start_monitor()
    t0 = time.time()
    try:
        elapsed = fn(port)
        wall = time.time() - t0
        stop_monitor()
        print(f"{elapsed:.1f}s")
        print_monitor_summary(label)
        return elapsed
    except Exception as e:
        stop_monitor()
        print(f"ERROR: {e}")
        return None


def run_pair(label, fn_a, port_a, fn_b, port_b):
    results = [None, None]
    errors  = [None, None]

    def run(idx, fn, port):
        try:
            results[idx] = fn(port)
        except Exception as e:
            errors[idx] = str(e)

    print(f"\n  {label}...")
    print(f"    Firing simultaneously... ", end="", flush=True)

    start_monitor()
    t_wall = time.time()
    threads = [
        threading.Thread(target=run, args=(0, fn_a, port_a), daemon=True),
        threading.Thread(target=run, args=(1, fn_b, port_b), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=REQUEST_TIMEOUT + 10)
    wall = time.time() - t_wall
    stop_monitor()

    print("done")
    for idx, (port, result, err) in enumerate(
        [(port_a, results[0], errors[0]), (port_b, results[1], errors[1])]
    ):
        if err:
            print(f"    GPU{idx} (:{port}): ERROR — {err}")
        else:
            print(f"    GPU{idx} (:{port}): {result:.1f}s")
    print(f"    Wall clock: {wall:.1f}s")
    print_monitor_summary(label)
    return results[0], results[1], wall


def slowdown(elapsed, baseline):
    if elapsed is None or baseline is None or baseline < 1:
        return "N/A"
    ratio = elapsed / baseline
    if ratio < 1.1:
        return f"✓ PARALLEL  ({ratio:.2f}x)"
    elif ratio < 1.5:
        return f"~ PARTIAL   ({ratio:.2f}x slowdown)"
    else:
        return f"✗ SERIAL    ({ratio:.2f}x slowdown)"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  GPU Parallelism Diagnostic — Clean Benchmark")
    print("  (indexer must be stopped before running this)")
    print("=" * 60)

    print("\nChecking GPU servers...")
    for port, name in [(GPU0_PORT, "GPU0"), (GPU1_PORT, "GPU1")]:
        try:
            with urllib.request.urlopen(
                f"http://{HOST}:{port}/health", timeout=4
            ) as r:
                status = json.loads(r.read()).get("status", "?")
            print(f"  {name} :{port} — {status}")
        except Exception as e:
            print(f"  {name} :{port} — UNREACHABLE: {e}")
            sys.exit(1)

    print("\nChecking GPU sysfs monitor paths...")
    for label, path in GPU_BUSY_PATHS.items():
        try:
            with open(path) as f:
                val = f.read().strip()
            print(f"  {label}: {val}%")
        except Exception as e:
            print(f"  {label}: UNAVAILABLE ({e})")

    # ── Phase 1: Text-only ────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("PHASE 1: Text-only inference (no image)")
    print("─" * 60)

    b_text_0 = run_solo("Text baseline GPU0", text_request, GPU0_PORT)
    b_text_1 = run_solo("Text baseline GPU1", text_request, GPU1_PORT)

    t0, t1, t_wall = run_pair(
        "Text simultaneous (GPU0 + GPU1)",
        text_request, GPU0_PORT,
        text_request, GPU1_PORT,
    )

    print(f"\n  Text results:")
    print(f"    GPU0: {slowdown(t0, b_text_0)}  (solo={b_text_0:.1f}s  pair={t0:.1f}s)")
    print(f"    GPU1: {slowdown(t1, b_text_1)}  (solo={b_text_1:.1f}s  pair={t1:.1f}s)")
    if b_text_0 and b_text_1:
        expected_serial = b_text_0 + b_text_1
        print(f"    Wall {t_wall:.1f}s  (serial would be {expected_serial:.1f}s  parallel would be {max(b_text_0,b_text_1):.1f}s)")

    # ── Phase 2: Vision ───────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("PHASE 2: Vision inference (16×16 PNG)")
    print("─" * 60)

    b_vis_0 = run_solo("Vision baseline GPU0", vision_request, GPU0_PORT)
    b_vis_1 = run_solo("Vision baseline GPU1", vision_request, GPU1_PORT)

    v0, v1, v_wall = run_pair(
        "Vision simultaneous (GPU0 + GPU1)",
        vision_request, GPU0_PORT,
        vision_request, GPU1_PORT,
    )

    print(f"\n  Vision results:")
    print(f"    GPU0: {slowdown(v0, b_vis_0)}  (solo={b_vis_0:.1f}s  pair={v0:.1f}s)")
    print(f"    GPU1: {slowdown(v1, b_vis_1)}  (solo={b_vis_1:.1f}s  pair={v1:.1f}s)")
    if b_vis_0 and b_vis_1:
        expected_serial = b_vis_0 + b_vis_1
        print(f"    Wall {v_wall:.1f}s  (serial would be {expected_serial:.1f}s  parallel would be {max(b_vis_0,b_vis_1):.1f}s)")

    # ── Conclusion ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("CONCLUSION")
    print("=" * 60)

    if b_text_0 and b_text_1 and t0 and t1:
        avg_text = ((t0 / b_text_0) + (t1 / b_text_1)) / 2
        if avg_text < 1.2:
            print("  Text-only: PARALLEL ✓ — GPUs run independently for text")
        else:
            print(f"  Text-only: SERIALIZING ({avg_text:.2f}x) — problem exists even without vision")

    if b_vis_0 and b_vis_1 and v0 and v1:
        avg_vis = ((v0 / b_vis_0) + (v1 / b_vis_1)) / 2
        if avg_vis < 1.2:
            print("  Vision:    PARALLEL ✓ — no interference detected")
        elif avg_vis < 1.5:
            print(f"  Vision:    PARTIAL serialization ({avg_vis:.2f}x) — some interference")
        else:
            print(f"  Vision:    SERIALIZING ({avg_vis:.2f}x) — confirmed GPU interference for ViT workload")


if __name__ == "__main__":
    main()
