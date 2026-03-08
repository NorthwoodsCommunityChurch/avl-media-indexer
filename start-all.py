#!/usr/bin/env python3
"""
ARCHIVE: This script is Windows-only. It uses schtasks, taskkill,
C:\\Users\\mediaadmin\\ paths, and Windows batch file generation.
On Ubuntu, servers are managed via systemd.
See SERVERS.md for current operations.

start-all.py - Start all LLM servers with correct GPU assignments.

Replaces start-servers.ps1. Uses crash-and-retry to find correct GPUs
(Vulkan device indices are non-deterministic between processes).
Kills existing processes, writes batch files, starts via schtasks, health checks.

Run: python start-all.py
"""

import subprocess
import time
import re
import sys
import os

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOME = r"C:\Users\mediaadmin"
LLAMA_DIR = os.path.join(HOME, "llama.cpp")
WHISPER_DIR = os.path.join(HOME, r"whisper.cpp\build\bin\Release")
MODELS = os.path.join(HOME, "models")
LLAMA_SERVER = os.path.join(LLAMA_DIR, "llama-server.exe")

GEMMA_MODEL = os.path.join(MODELS, "gemma-3-12b-it-Q3_K_S.gguf")
GEMMA_MMPROJ = os.path.join(MODELS, "mmproj-gemma-3-12b-it-f16.gguf")
WHISPER_MODEL = os.path.join(MODELS, "ggml-large-v3-turbo.bin")

# GPU name substrings for role matching
GEMMA_GPU_PATTERN = "RX 580"
WHISPER_GPU_PATTERN = "Pro 580X"

# Vulkan indices to try (3 GPUs in the system)
VULKAN_INDICES = [0, 1, 2]

SERVERS = [
    {
        "name": "Gemma-0",
        "task": "Gemma-GPU0",
        "port": 8090,
        "gpu_pattern": GEMMA_GPU_PATTERN,
        "type": "gemma",
        "bat": os.path.join(HOME, "start-gemma0.bat"),
        "log": os.path.join(HOME, "gemma0-log.txt"),
    },
    {
        "name": "Gemma-1",
        "task": "Gemma-GPU1",
        "port": 8091,
        "gpu_pattern": GEMMA_GPU_PATTERN,
        "type": "gemma",
        "bat": os.path.join(HOME, "start-gemma1.bat"),
        "log": os.path.join(HOME, "gemma1-log.txt"),
    },
    {
        "name": "Whisper",
        "task": "Whisper-GPU2",
        "port": 8092,
        "gpu_pattern": "",  # No GPU name requirement — Whisper works on any GPU
        "type": "whisper",
        "bat": os.path.join(HOME, "start-whisper.bat"),
        "log": os.path.join(HOME, "whisper-log.txt"),
    },
]

HEALTH_TIMEOUT = 90  # seconds to wait for server health
GPU_NAME_TIMEOUT = 5  # seconds to wait for GPU name in log

# Set True if testing shows GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM helps.
# See Issue #16 / workaround A2.
USE_DISABLE_HOST_VISIBLE_VIDMEM = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, timeout=30):
    """Run a command and return stdout+stderr. Swallows errors."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=True
        )
        return result.stdout + result.stderr
    except Exception as e:
        return str(e)


def kill_all_servers():
    """Kill all llama-server and whisper-server processes."""
    print("Killing existing servers...")
    run("taskkill /f /im llama-server.exe", timeout=10)
    run("taskkill /f /im whisper-server.exe", timeout=10)
    time.sleep(3)


def write_gemma_bat(bat_path, log_path, port, vulkan_index):
    """Write a batch file that starts a Gemma server on the given Vulkan device."""
    env_line = ""
    if USE_DISABLE_HOST_VISIBLE_VIDMEM:
        env_line = "set GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1\r\n"

    content = (
        "@echo off\r\n"
        "cd /d %s\r\n" % LLAMA_DIR
        + env_line
        + 'llama-server.exe -m "%s" --mmproj "%s"'
          % (GEMMA_MODEL, GEMMA_MMPROJ)
        + " --host 0.0.0.0 --port %d" % port
        + " -ngl 99 --device Vulkan%d" % vulkan_index
        + " -c 1024 --parallel 1 --fit off"
        + ' > "%s" 2>&1\r\n' % log_path
    )
    with open(bat_path, "w", newline="") as f:
        f.write(content)


def write_whisper_bat(bat_path, log_path, vulkan_index):
    """Write a batch file that starts a Whisper server on the given Vulkan device."""
    content = (
        "@echo off\r\n"
        "cd /d %s\r\n" % WHISPER_DIR
        + 'whisper-server.exe -m "%s"' % WHISPER_MODEL
        + " --host 0.0.0.0 --port 8092"
        + " --device %d" % vulkan_index
        + ' > "%s" 2>&1\r\n' % log_path
    )
    with open(bat_path, "w", newline="") as f:
        f.write(content)


def register_and_start_task(task_name, bat_path):
    """Register a scheduled task and run it. Survives SSH disconnect."""
    run(
        'schtasks /create /tn "%s" /tr "%s" /sc onlogon /ru mediaadmin /rl highest /f'
        % (task_name, bat_path),
        timeout=10,
    )
    run('schtasks /run /tn "%s"' % task_name, timeout=10)


def check_health(port):
    """Check if a server is healthy using curl.exe."""
    output = run(
        "curl.exe -s --max-time 2 http://localhost:%d/health" % port,
        timeout=5,
    )
    return '"status"' in output and '"ok"' in output


def read_log(log_path):
    """Read a log file, returning empty string if missing/locked."""
    try:
        with open(log_path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def get_gpu_name_from_log(log_path):
    """Extract the GPU name from the 'using device' line in the log.

    Returns (gpu_name, vulkan_index) or (None, None) if not found.
    """
    log = read_log(log_path)
    m = re.search(r"using device Vulkan(\d+) \(([^)]+)\)", log)
    if m:
        return m.group(2), int(m.group(1))
    return None, None


def wait_for_health_or_fail(port, log_path, require_gpu):
    """Wait for server to become healthy and verify GPU name.

    Returns (True, gpu_name) on success, or (False, reason) on failure.
    Fails fast on OOM or wrong GPU name.
    If require_gpu is empty, only checks health (no GPU name validation).
    """
    for _ in range(HEALTH_TIMEOUT):
        time.sleep(1)

        log = read_log(log_path)

        # Check for OOM crash
        if "ErrorOutOfDeviceMemory" in log or "GGML_ASSERT" in log:
            return False, "OOM crash"

        # Check GPU name early — if wrong GPU, bail fast (llama-server only)
        if require_gpu:
            gpu_name, _ = get_gpu_name_from_log(log_path)
            if gpu_name and require_gpu not in gpu_name:
                return False, "wrong GPU: %s" % gpu_name

        # Health check
        if check_health(port):
            if not require_gpu:
                # No GPU name validation needed (e.g. Whisper)
                return True, "ok"

            # Wait for GPU name to appear in log (I/O buffering — Issue #13)
            for _ in range(GPU_NAME_TIMEOUT * 2):
                gpu_name, _ = get_gpu_name_from_log(log_path)
                if gpu_name:
                    if require_gpu in gpu_name:
                        return True, gpu_name
                    else:
                        return False, "wrong GPU: %s" % gpu_name
                time.sleep(0.5)

            return False, "GPU name never appeared in log"

    return False, "timeout after %ds" % HEALTH_TIMEOUT


def kill_on_port(port, task_name):
    """Stop task and kill any process on the port."""
    run('schtasks /end /tn "%s"' % task_name, timeout=10)
    # Find and kill processes on the port
    output = run("netstat -ano | findstr :%d" % port, timeout=5)
    pids = set()
    for line in output.splitlines():
        parts = line.split()
        if parts:
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    for pid in pids:
        if pid > 0:
            run("taskkill /f /pid %d" % pid, timeout=5)
    time.sleep(2)


def try_start_server(server, vulkan_index):
    """Try to start a server on the given Vulkan index.

    Returns True if server started on the correct GPU, False otherwise.
    """
    name = server["name"]
    port = server["port"]
    require_gpu = server["gpu_pattern"]

    # Write batch file for this Vulkan index
    if server["type"] == "gemma":
        write_gemma_bat(server["bat"], server["log"], port, vulkan_index)
    else:
        write_whisper_bat(server["bat"], server["log"], vulkan_index)

    # Clear old log
    try:
        if os.path.exists(server["log"]):
            os.remove(server["log"])
    except Exception:
        pass

    # Register task and start
    register_and_start_task(server["task"], server["bat"])

    # Wait for health + GPU verification
    ok, detail = wait_for_health_or_fail(port, server["log"], require_gpu)

    if ok:
        print("    UP on %s" % detail)
        return True
    else:
        print("    %s" % detail)
        kill_on_port(port, server["task"])
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 50)
    print("  start-all.py - LLM Server Startup")
    print("=" * 50)

    # Step 1: Kill existing servers
    kill_all_servers()

    # Step 2: Delete old scheduled tasks that auto-start with wrong config (Issue #18)
    old_tasks = [
        "GemmaGPU0", "GemmaGPU1", "WhisperGPU2",
        "TestGPU0", "TestGPU1", "GPU-Monitor", "GPU-Z-Dump", "GPU-Z-Log",
    ]
    for task in old_tasks:
        run('schtasks /delete /tn "%s" /f' % task, timeout=5)

    # Delete old batch files that had wrong config
    old_bats = [
        os.path.join(HOME, "start-gpu0.bat"),
        os.path.join(HOME, "start-gpu1.bat"),
    ]
    for bat in old_bats:
        try:
            if os.path.exists(bat):
                os.remove(bat)
                print("  Deleted old %s" % os.path.basename(bat))
        except Exception:
            pass

    # Step 3: Start each server with crash-and-retry GPU assignment
    # Vulkan indices are non-deterministic (Issue #2). We try each index,
    # check the log for the actual GPU name, and retry if wrong.
    # Track claimed indices so two servers don't end up on the same GPU.
    claimed_indices = set()
    results = {}

    for server in SERVERS:
        name = server["name"]
        port = server["port"]
        print("\n--- %s (port %d) ---" % (name, port))

        ok = False
        # Try unclaimed indices first, then all indices as fallback
        indices_to_try = (
            [i for i in VULKAN_INDICES if i not in claimed_indices]
            + [i for i in VULKAN_INDICES if i in claimed_indices]
        )

        for vulkan_idx in indices_to_try:
            print("  Trying Vulkan%d..." % vulkan_idx)
            ok = try_start_server(server, vulkan_idx)
            if ok:
                claimed_indices.add(vulkan_idx)
                # Read actual index from log (may differ from requested)
                _, actual_idx = get_gpu_name_from_log(server["log"])
                if actual_idx is not None:
                    claimed_indices.add(actual_idx)
                break
            time.sleep(2)

        results[name] = ok
        if not ok:
            print("  %s FAILED on all Vulkan indices!" % name)

    # Step 4: Summary
    print("\n" + "=" * 50)
    print("  Summary")
    print("=" * 50)
    all_ok = True
    for server in SERVERS:
        name = server["name"]
        port = server["port"]
        status = "UP" if results.get(name) else "DOWN"
        print("  %s (port %d): %s" % (name, port, status))
        if not results.get(name):
            all_ok = False

    if all_ok:
        print("\nAll servers started successfully.")
    else:
        print("\nSome servers failed to start. Check logs in %s" % HOME)
        sys.exit(1)


if __name__ == "__main__":
    main()
