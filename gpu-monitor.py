#!/usr/bin/env python3
"""
GPU Activity Monitor
Polls llama.cpp /slots and indexer /status to show real GPU inference state.

Modes:
  default       Live dashboard (refreshes every 2s)
  --benchmark   Test parallelism across every GPU pair and exit
  --json        Print one snapshot as JSON and exit

Options:
  --interval N  Refresh interval in seconds (default: 2)
  --log-tail N  Show last N lines of each server log via SSH
"""

import argparse
import base64
import json
import os
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from datetime import datetime
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MACHINE_IP  = "10.10.11.157"
SSH_HOST    = f"mediaadmin@{MACHINE_IP}"
INDEXER_URL = f"http://{MACHINE_IP}:8081"
LOG_FILE    = os.path.expanduser("~/.gpu-monitor.log")

GPUS = [
    {
        "id":   0,
        "name": "RX 580",
        "port": 8090,
        "type": "gemma",
        "log":  "/home/mediaadmin/media-index/indexer-run.log",
    },
    {
        "id":   1,
        "name": "RX 580",
        "port": 8091,
        "type": "gemma",
        "log":  "/home/mediaadmin/media-index/indexer-run.log",
    },
    {
        "id":   2,
        "name": "Pro 580X",
        "port": 8092,
        "type": "whisper",
        "log":  "/home/mediaadmin/media-index/indexer-run.log",
    },
]

BAR_WIDTH    = 40   # characters wide for the 60 s activity bars
HISTORY_SECS = 60   # seconds of history kept in the sliding window


# ---------------------------------------------------------------------------
# Minimal test media (generated once at import time, no pip deps)
# ---------------------------------------------------------------------------

def _png_chunk(name, data):
    crc = zlib.crc32(name + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + name + data + crc.to_bytes(4, "big")


def _make_benchmark_image_b64():
    """Build a 16×16 solid-gray PNG entirely from stdlib."""
    w, h = 16, 16
    sig  = b"\x89PNG\r\n\x1a\n"
    # IHDR: width, height, bit-depth, color-type(0=gray), compress, filter, interlace
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    # Raw image: one filter byte (0=None) + 16 gray pixels per row, 16 rows
    raw  = (b"\x00" + b"\x80" * w) * h
    idat = _png_chunk(b"IDAT", zlib.compress(raw, 9))
    iend = _png_chunk(b"IEND", b"")
    return base64.b64encode(sig + ihdr + idat + iend).decode()


def _make_benchmark_wav_b64():
    """Build a 10-second silence WAV (16 kHz, mono, 16-bit) from stdlib.
    10s gives Whisper enough work to produce a measurable inference time."""
    sr, ch, bps = 16000, 1, 16
    n  = sr * 10                         # 10 s of samples
    ds = n * ch * (bps // 8)            # data bytes
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + ds, b"WAVE",
        b"fmt ", 16, 1, ch, sr,
        sr * ch * (bps // 8),           # byte rate
        ch * (bps // 8),                # block align
        bps,
        b"data", ds,
    )
    return base64.b64encode(hdr + b"\x00" * ds).decode()


# Pre-generate so benchmark starts instantly
_IMG_B64 = _make_benchmark_image_b64()
_WAV_B64 = _make_benchmark_wav_b64()


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _http_get(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post_json(url, payload, timeout=180):
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _http_post_multipart(url, wav_b64, timeout=120):
    """POST multipart/form-data for whisper.cpp /inference."""
    wav_bytes = base64.b64decode(wav_b64)
    boundary  = b"gpumon_boundary_xyz"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="test.wav"\r\n'
        b"Content-Type: audio/wav\r\n\r\n"
        + wav_bytes
        + b"\r\n--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
        b"json\r\n"
        b"--" + boundary + b"--\r\n"
    )
    ct  = "multipart/form-data; boundary=" + boundary.decode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ct})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Per-GPU state and rolling history
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# One entry per GPU — updated by poll_all()
GPU_STATE = [
    {
        "busy":            False,
        "busy_since":      None,   # float timestamp when became busy
        "last_duration":   None,   # float seconds of last completed task
        "reachable":       False,
        "slots_supported": None,   # None=unknown, True/False=confirmed
    }
    for _ in GPUS
]

# deque of (timestamp: float, busy_list: [bool, bool, bool])
HISTORY = deque()


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def _poll_slots(port):
    """
    Poll llama.cpp /slots (preferred) then fall back to /health.
    Returns dict: {reachable, busy, slots_supported}.
    """
    data = _http_get(f"http://{MACHINE_IP}:{port}/slots", timeout=4)

    if data is None:
        # /slots not available — try /health
        health = _http_get(f"http://{MACHINE_IP}:{port}/health", timeout=4)
        if health is None:
            return {"reachable": False, "busy": False, "slots_supported": False}
        # llama.cpp /health: {"status": "ok"} idle, {"status": "no slot available"} busy
        busy = health.get("status", "ok") == "no slot available"
        return {"reachable": True, "busy": busy, "slots_supported": False}

    if isinstance(data, list) and data:
        slot = data[0]
        busy = slot.get("is_processing", False) or slot.get("state", 0) == 1
        return {"reachable": True, "busy": busy, "slots_supported": True}

    return {"reachable": True, "busy": False, "slots_supported": True}


def _poll_whisper(transcribing_flag):
    """
    Poll whisper.cpp server.  Tries /slots first; falls back to /health
    and augments with the indexer's transcription heartbeat flag.
    Returns dict: {reachable, busy, slots_supported}.
    """
    port = GPUS[2]["port"]

    health = _http_get(f"http://{MACHINE_IP}:{port}/health", timeout=4)
    if health is None:
        return {"reachable": False, "busy": False, "slots_supported": False}

    # Try /slots — some whisper.cpp builds expose it
    slots_data = _http_get(f"http://{MACHINE_IP}:{port}/slots", timeout=2)
    if isinstance(slots_data, list) and slots_data:
        busy = slots_data[0].get("is_processing", False)
        return {"reachable": True, "busy": busy, "slots_supported": True}

    # Fall back: health status + indexer heartbeat
    status = health.get("status", "ok")
    busy   = (status == "no slot available") or transcribing_flag
    return {"reachable": True, "busy": busy, "slots_supported": False}


def _update_gpu_state(i, result):
    """Apply poll result to GPU_STATE[i], tracking busy start/end times."""
    now = time.time()
    with _lock:
        st       = GPU_STATE[i]
        was_busy = st["busy"]
        is_busy  = result["busy"]

        st["reachable"]       = result["reachable"]
        st["slots_supported"] = result.get("slots_supported")

        if is_busy and not was_busy:
            st["busy"]       = True
            st["busy_since"] = now
        elif not is_busy and was_busy:
            if st["busy_since"] is not None:
                st["last_duration"] = now - st["busy_since"]
            st["busy"]       = False
            st["busy_since"] = None
        # else: unchanged (still busy or still idle)


def poll_all(transcribing=False):
    """Poll all GPUs concurrently and append a snapshot to HISTORY."""
    results = [None] * len(GPUS)

    def worker(i, gpu):
        if gpu["type"] == "gemma":
            results[i] = _poll_slots(gpu["port"])
        else:
            results[i] = _poll_whisper(transcribing)

    threads = [
        threading.Thread(target=worker, args=(i, g), daemon=True)
        for i, g in enumerate(GPUS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=6)

    for i, res in enumerate(results):
        if res is not None:
            _update_gpu_state(i, res)

    now = time.time()
    with _lock:
        snapshot = [GPU_STATE[i]["busy"] for i in range(len(GPUS))]
    HISTORY.append((now, snapshot))

    # Trim entries older than HISTORY_SECS
    cutoff = now - HISTORY_SECS
    while HISTORY and HISTORY[0][0] < cutoff:
        HISTORY.popleft()


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------

def _render_bar(gpu_idx):
    """Return a BAR_WIDTH-char string showing busy (█) / idle (░) over 60 s."""
    now      = time.time()
    slot_dur = HISTORY_SECS / BAR_WIDTH
    bar      = []
    for s in range(BAR_WIDTH):
        t_end   = now - s * slot_dur
        t_start = t_end - slot_dur
        active  = any(
            t_start <= ts <= t_end and bl[gpu_idx]
            for ts, bl in HISTORY
        )
        bar.append("\u2588" if active else "\u2591")
    return "".join(reversed(bar))


def _overlap_summary():
    """Describe the worst-case concurrency observed over the last 60 s."""
    if not HISTORY:
        return "No data yet"
    max_sim = max(sum(bl) for _, bl in HISTORY)
    if max_sim == 0:
        return "All GPUs idle"
    elif max_sim == 1:
        return "Full 3-way serialization — only 1 GPU active at a time"
    elif max_sim == 2:
        return "Partial serialization — 2 GPUs active simultaneously observed"
    else:
        return "True parallelism — all 3 GPUs active simultaneously"


def _gpu_line(i, gpu, st, gq_by_server=None):
    name_str = gpu["name"].ljust(8)
    port_str = f":{gpu['port']}"

    if not st["reachable"]:
        state_str = "[ DOWN        ]"
    elif st["busy"] and st["busy_since"] is not None:
        elapsed   = time.time() - st["busy_since"]
        state_str = f"[BUSY  {elapsed:5.1f}s]"
    else:
        state_str = "[IDLE   -.-- ]"

    last_str = ""
    if st["last_duration"] is not None:
        last_str = f"  last: {st['last_duration']:5.1f}s"

    queue_str = ""
    if gq_by_server:
        server_key = f"http://{MACHINE_IP}:{gpu['port']}"
        if server_key in gq_by_server:
            qd        = gq_by_server[server_key].get("queue_depth", 0)
            processed = gq_by_server[server_key].get("processed", 0)
            queue_str = f"  queue:{qd}  done:{processed}"

    slot_flag = ""
    if st["slots_supported"] is False and st["reachable"]:
        slot_flag = "  (/slots N/A — using /health)"

    extra = "  (Whisper)" if gpu["type"] == "whisper" else ""

    return (
        f" GPU {i}  {name_str} {port_str}  {state_str}"
        f"{last_str}{queue_str}{extra}{slot_flag}"
    )


def _indexer_line(status):
    if status is None:
        return f" Indexer: unreachable ({INDEXER_URL}/status)"

    counts       = status.get("counts", {})
    indexed      = counts.get("indexed", 0)
    pending      = counts.get("pending", 0)
    gq           = status.get("gpu_queues", [])
    transcribing = status.get("scanner", {}).get("transcribing", False)

    worker_parts = []
    if gq:
        worker_parts.append(f"{len(gq)} Gemma")
    if transcribing:
        worker_parts.append("1 Whisper")
    workers_str = (
        "  Active workers: " + " + ".join(worker_parts)
        if worker_parts else ""
    )

    return f" Indexer: {indexed:,} indexed | {pending:,} pending{workers_str}"


def _write_log(status):
    with _lock:
        states = [dict(s) for s in GPU_STATE]

    now   = time.time()
    parts = []
    for i, st in enumerate(states):
        if st["busy"] and st["busy_since"] is not None:
            dur = now - st["busy_since"]
            parts.append(f"gpu{i}=BUSY:{dur:.1f}s")
        else:
            parts.append(f"gpu{i}=IDLE")

    if status:
        counts = status.get("counts", {})
        parts.append(f"indexed={counts.get('indexed', 0)}")
        parts.append(f"pending={counts.get('pending', 0)}")

    ts = datetime.now().isoformat(timespec="seconds")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{ts} {' '.join(parts)}\n")
    except OSError:
        pass


def _render_dashboard(interval, status, log_tails=None):
    # Move cursor to top-left and clear screen
    print("\033[2J\033[H", end="", flush=True)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== GPU Activity Monitor ===  {now_str}   (refresh: {interval}s)\n")

    with _lock:
        states = [dict(s) for s in GPU_STATE]

    # Build a port→queue-entry map from indexer status
    gq_by_server = {}
    if status:
        for entry in status.get("gpu_queues", []):
            srv = entry.get("server", "")
            gq_by_server[srv] = entry

    for i, gpu in enumerate(GPUS):
        print(_gpu_line(i, gpu, states[i], gq_by_server))

    print()
    print(_indexer_line(status))
    print()

    # Activity bars
    print(f" OVERLAP (last {HISTORY_SECS}s):")
    for i, gpu in enumerate(GPUS):
        bar = _render_bar(i)
        print(f"  GPU{i}: {bar}")
    print(f"        \u2191 {_overlap_summary()}")

    # Optional log tails
    if log_tails:
        print()
        print(" Server log tails:")
        for gpu_id, lines in sorted(log_tails.items()):
            gpu = GPUS[gpu_id]
            print(f"  GPU {gpu_id} ({gpu['name']} :{gpu['port']}):")
            for line in lines:
                print(f"    {line}")

    print(f"\n Log: {LOG_FILE}   Ctrl+C to exit", flush=True)


# ---------------------------------------------------------------------------
# Log tail (via SSH)
# ---------------------------------------------------------------------------

_TIMING_KEYWORDS = ("prompt eval time", "eval time", "total time", "encode time")


def _fetch_log_tail(gpu, n):
    """Fetch timing lines from the server log via SSH. Returns a list of strings."""
    log_path = gpu["log"]
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=4",
        SSH_HOST,
        f"tail -n {n} '{log_path}'",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        )
        lines  = result.stdout.splitlines()
        timing = [
            l.strip() for l in lines
            if any(kw in l.lower() for kw in _TIMING_KEYWORDS)
        ]
        return timing[-5:] if timing else lines[-3:]
    except Exception as e:
        return [f"(SSH error: {e})"]


def fetch_all_log_tails(n):
    """Fetch log tails for all GPUs concurrently. Returns {gpu_id: [lines]}."""
    results = {}

    def worker(i, gpu):
        results[i] = _fetch_log_tail(gpu, n)

    threads = [
        threading.Thread(target=worker, args=(i, g), daemon=True)
        for i, g in enumerate(GPUS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)

    return results


# ---------------------------------------------------------------------------
# JSON snapshot mode
# ---------------------------------------------------------------------------

def print_json_snapshot(status):
    transcribing = False
    if status:
        transcribing = status.get("scanner", {}).get("transcribing", False)
    poll_all(transcribing=transcribing)

    now = time.time()
    with _lock:
        states  = [dict(s) for s in GPU_STATE]
        max_sim = max((sum(bl) for _, bl in HISTORY), default=0)

    gpus_out = []
    for i, gpu in enumerate(GPUS):
        st    = states[i]
        entry = {
            "id":              i,
            "name":            gpu["name"],
            "port":            gpu["port"],
            "type":            gpu["type"],
            "reachable":       st["reachable"],
            "busy":            st["busy"],
            "slots_supported": st["slots_supported"],
        }
        if st["busy"] and st["busy_since"] is not None:
            entry["busy_for_secs"] = round(now - st["busy_since"], 1)
        if st["last_duration"] is not None:
            entry["last_duration_secs"] = round(st["last_duration"], 1)
        gpus_out.append(entry)

    indexer_out = None
    if status:
        counts      = status.get("counts", {})
        indexer_out = {
            "indexed":      counts.get("indexed", 0),
            "pending":      counts.get("pending", 0),
            "transcribing": transcribing,
            "gpu_queues":   status.get("gpu_queues", []),
        }

    _sim_labels = {0: "all_idle", 1: "full_serialization",
                   2: "partial_serialization", 3: "true_parallelism"}

    out = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "gpus":      gpus_out,
        "indexer":   indexer_out,
        "overlap_60s": {
            "max_simultaneous": max_sim,
            "summary":          _sim_labels.get(max_sim, "unknown"),
        },
    }
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------------------
# Benchmark mode
# ---------------------------------------------------------------------------

def _gemma_request(gpu):
    """
    Send one Gemma vision inference and return (elapsed_secs, error_or_None).
    Uses cache_prompt:false so the KV cache never masks serialization — each
    call forces a full fresh inference on the GPU.
    """
    url  = f"http://{MACHINE_IP}:{gpu['port']}/v1/chat/completions"
    body = json.dumps({
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type":      "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_IMG_B64}"},
                },
                {"type": "text", "text": "One word: what color is this image?"},
            ],
        }],
        "max_tokens": 5,
        "cache_prompt": False,
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            r.read()
        return time.time() - t0, None
    except Exception as e:
        return time.time() - t0, str(e)


def _whisper_request(gpu):
    """
    Send one Whisper transcription request and return (elapsed_secs, error_or_None).
    """
    url = f"http://{MACHINE_IP}:{gpu['port']}/inference"
    t0  = time.time()
    try:
        _http_post_multipart(url, _WAV_B64, timeout=120)
        return time.time() - t0, None
    except Exception as e:
        return time.time() - t0, str(e)


def _fire_pair(gpu_a, gpu_b):
    """
    Fire requests to gpu_a and gpu_b simultaneously.
    Returns (elapsed_a, err_a, elapsed_b, err_b, wall_clock).
    """
    results = [None, None]

    def run(idx, gpu):
        if gpu["type"] == "gemma":
            results[idx] = _gemma_request(gpu)
        else:
            results[idx] = _whisper_request(gpu)

    t_wall_start = time.time()
    threads = [
        threading.Thread(target=run, args=(0, gpu_a), daemon=True),
        threading.Thread(target=run, args=(1, gpu_b), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=210)
    wall = time.time() - t_wall_start

    elapsed_a, err_a = results[0] or (0.0, "timeout")
    elapsed_b, err_b = results[1] or (0.0, "timeout")
    return elapsed_a, err_a, elapsed_b, err_b, wall


def _slowdown_verdict(elapsed_a, base_a, elapsed_b, base_b):
    """
    Compare simultaneous GPU times to their single-GPU baselines.
    WDDM time-slicing causes BOTH GPUs to slow equally, so comparing
    elapsed_a vs elapsed_b doesn't reveal it — we need the baseline.

    slowdown_ratio = how much slower each GPU ran vs alone:
      ~1.0x → no interference (true parallel)
      ~1.7x → WDDM time-slicing across 2 GPUs (confirmed pattern on this hardware)
      ~2.0x → fully serialized (one GPU blocked entirely while other runs)
    """
    ratios = []
    if base_a and base_a > 2.0:   # only count if baseline was long enough to measure
        ratios.append(elapsed_a / base_a)
    if base_b and base_b > 2.0:
        ratios.append(elapsed_b / base_b)

    if not ratios:
        return "N/A (baseline too short to measure interference)", None

    avg = sum(ratios) / len(ratios)
    detail = " + ".join(f"{r:.2f}x" for r in ratios)

    if avg > 1.5:
        return f"SERIALIZING  ({avg:.2f}x slowdown vs single-GPU — WDDM kernel serialization)", avg
    elif avg > 1.2:
        return f"PARTIAL SERIALIZATION  ({avg:.2f}x slowdown)", avg
    else:
        return f"PARALLEL \u2713  ({avg:.2f}x — no significant overhead)", avg


def run_benchmark():
    print("=== Parallelism Benchmark ===\n")
    print("Phase 1: Single-GPU baselines (one GPU at a time)...")
    print("Phase 2: Simultaneous pairs (both GPUs at once)...\n")
    print("This will take several minutes.\n")

    # ── Phase 1: baselines ─────────────────────────────────────────────────
    baselines = {}
    for gpu in GPUS:
        label = f"GPU{gpu['id']} ({gpu['name']} :{gpu['port']})"
        print(f"  Baseline {label}... ", end="", flush=True)
        if gpu["type"] == "gemma":
            elapsed, err = _gemma_request(gpu)
        else:
            elapsed, err = _whisper_request(gpu)
        if err:
            print(f"ERROR: {err}")
            baselines[gpu["id"]] = None
        else:
            print(f"{elapsed:.1f}s")
            baselines[gpu["id"]] = elapsed

    print()

    # ── Phase 2: pairs ─────────────────────────────────────────────────────
    pairs = [
        (GPUS[0], GPUS[1], "GPU0 + GPU1 (both Gemma / RX 580)"),
        (GPUS[0], GPUS[2], "GPU0 + GPU2 (Gemma + Whisper)"),
        (GPUS[1], GPUS[2], "GPU1 + GPU2 (Gemma + Whisper)"),
    ]

    verdicts = []
    slowdowns = []

    for i, (gpu_a, gpu_b, label) in enumerate(pairs, 1):
        print(f"  Test {i}: {label}")
        print(f"    Firing simultaneously... ", end="", flush=True)

        elapsed_a, err_a, elapsed_b, err_b, wall = _fire_pair(gpu_a, gpu_b)
        print("done")

        if err_a:
            print(f"    GPU {gpu_a['id']} error: {err_a}")
        if err_b:
            print(f"    GPU {gpu_b['id']} error: {err_b}")

        base_a = baselines.get(gpu_a["id"])
        base_b = baselines.get(gpu_b["id"])

        base_a_str = f"{base_a:.1f}s" if base_a else "N/A"
        base_b_str = f"{base_b:.1f}s" if base_b else "N/A"

        print(
            f"    GPU {gpu_a['id']}: {elapsed_a:.1f}s (baseline {base_a_str}) | "
            f"GPU {gpu_b['id']}: {elapsed_b:.1f}s (baseline {base_b_str}) | "
            f"Wall: {wall:.1f}s"
        )

        verdict, ratio = _slowdown_verdict(elapsed_a, base_a, elapsed_b, base_b)
        print(f"    \u2192 {verdict}")
        print()

        verdicts.append((label, verdict))
        if ratio is not None:
            slowdowns.append(ratio)

    print("  Conclusion:")
    for label, v in verdicts:
        print(f"    {label}: {v}")

    print()
    if not slowdowns:
        print("  Could not determine serialization pattern (baselines too short).")
    elif sum(slowdowns) / len(slowdowns) > 1.5:
        print("  All AMD GPUs appear to be serialized by the WDDM kernel.")
        print("  No two GPUs can run heavy inference simultaneously.")
        print("  Effective throughput: ~1 GPU equivalent across all 3.")
    elif sum(slowdowns) / len(slowdowns) < 1.2:
        print("  All GPU pairs ran in parallel — no significant serialization detected.")
    else:
        print("  Mixed results — partial serialization observed on some pairs.")


# ---------------------------------------------------------------------------
# Main dashboard loop
# ---------------------------------------------------------------------------

def run_dashboard(interval, log_tail_n):
    print(f"Starting GPU Activity Monitor (polling every {interval}s)...")
    print(f"Log: {LOG_FILE}")
    print("Ctrl+C to exit\n")
    time.sleep(0.3)

    try:
        while True:
            # Fetch indexer status (gives us transcribing flag + queue depths)
            status       = _http_get(f"{INDEXER_URL}/status", timeout=5)
            transcribing = False
            if status:
                transcribing = status.get("scanner", {}).get("transcribing", False)

            poll_all(transcribing=transcribing)

            log_tails = None
            if log_tail_n > 0:
                log_tails = fetch_all_log_tails(log_tail_n)

            _render_dashboard(interval, status, log_tails)
            _write_log(status)

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nExiting.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GPU Activity Monitor — real-time view of GPU inference state"
    )
    parser.add_argument(
        "--benchmark", action="store_true",
        help="Test parallelism across all GPU pairs and exit",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print one status snapshot as JSON and exit",
    )
    parser.add_argument(
        "--interval", type=int, default=2, metavar="N",
        help="Dashboard refresh interval in seconds (default: 2)",
    )
    parser.add_argument(
        "--log-tail", type=int, default=0, metavar="N",
        help="Show last N lines of each server log via SSH",
    )
    parser.add_argument(
        "--host", default=None, metavar="IP",
        help="Override server IP (use 127.0.0.1 when running directly on the Mac Pro)",
    )
    args = parser.parse_args()

    if args.host:
        global MACHINE_IP, SSH_HOST, INDEXER_URL
        MACHINE_IP  = args.host
        SSH_HOST    = f"mediaadmin@{MACHINE_IP}"
        INDEXER_URL = f"http://{MACHINE_IP}:8081"

    if args.benchmark:
        run_benchmark()
    elif args.json:
        status = _http_get(f"{INDEXER_URL}/status", timeout=5)
        print_json_snapshot(status)
    else:
        run_dashboard(args.interval, args.log_tail)


if __name__ == "__main__":
    main()
