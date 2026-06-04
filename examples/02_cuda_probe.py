"""Probe the GPU from inside a Mimiry function and return structured data.

Runs nvidia-smi, a ctypes libcuda init, and cuDeviceGetCount, returning the
result as a Python dict instead of stdout lines. Exercises the full pipeline:
auth, session lifecycle, function serialization, and result return.

Run:
    export MIMIRY_SSH_KEY=~/.ssh/mimiry   # your registered SSH key
    python examples/02_cuda_probe.py
"""

import time

import mimiry


@mimiry.function(
    # Uses default hardware; run `mimiry availability` to choose a GPU/provider.
    image="nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04",  # Python 3.12 for cloudpickle compat
    timeout=900,
)
def cuda_probe() -> dict:
    """Probe the GPU and return structured data."""
    import ctypes
    import os
    import socket
    import subprocess

    result: dict = {"hostname": socket.gethostname(), "os": None, "gpus": [], "cuda": None}

    # OS info
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    result["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except FileNotFoundError:
        pass

    # nvidia-smi
    try:
        smi = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in smi.stdout.strip().splitlines():
            name, driver, mem = (p.strip() for p in line.split(","))
            result["gpus"].append({"name": name, "driver": driver, "memory_total": mem})
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        result["gpus"] = [{"error": str(e)}]

    # libcuda ctypes probe — confirms the GPU is actually usable, not just listed.
    for lib in ("libcuda.so.1", "libcuda.so"):
        try:
            cuda = ctypes.CDLL(lib)
            rc = cuda.cuInit(0)
            count = ctypes.c_int(0)
            cuda.cuDeviceGetCount(ctypes.byref(count))
            result["cuda"] = {"lib": lib, "cu_init_rc": rc, "device_count": count.value}
            break
        except OSError:
            continue

    result["env_pythonpath"] = os.environ.get("PYTHONPATH", "")
    return result


if __name__ == "__main__":
    t0 = time.monotonic()
    print("Submitting probe to Mimiry — expect ~2 min cold start...")
    out = cuda_probe.remote()
    elapsed = time.monotonic() - t0

    print(f"\n── Result (wall clock {elapsed:.1f}s) ───────────────────────────────")
    import json

    print(json.dumps(out, indent=2))
