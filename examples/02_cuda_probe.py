"""SDK port of scripts/run_quick_test.sh.

Same probe (nvidia-smi + ctypes libcuda init + cuDeviceGetCount), but the
result comes back as a Python dict instead of stdout lines. Validates the
entire v1 pipeline: auth, session lifecycle, function serialization,
result return.

Run:
    export MIMIRY_SSH_KEY=~/.ssh/mimiry_oliver_new
    python examples/02_cuda_probe.py
"""

import time

import mimiry


@mimiry.function(
    gpu="T4",
    provider="gcp",  # T4 is GCP-only today; query /availability before assuming otherwise
    image="nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04",  # Python 3.12 for cloudpickle compat
    timeout=900,
)
def cuda_probe() -> dict:
    """Run the same probe as run_quick_test.sh and return structured data."""
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
    print("Submitting probe to Mimiry T4 — expect ~2 min cold start...")
    out = cuda_probe.remote()
    elapsed = time.monotonic() - t0

    print(f"\n── Result (wall clock {elapsed:.1f}s) ───────────────────────────────")
    import json

    print(json.dumps(out, indent=2))
