"""Minimal Mimiry SDK example. Equivalent to a one-line nvidia-smi over GPU.

Run:
    export MIMIRY_SSH_KEY=~/.ssh/mimiry   # your registered SSH key
    python examples/01_hello.py
"""

import mimiry


@mimiry.function(
    # Uses default hardware; run `mimiry availability` to choose a GPU/provider.
    image="nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04",  # Python 3.12 for cloudpickle compat
)
def gpu_name() -> str:
    import subprocess

    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        text=True,
    )
    return out.strip()


if __name__ == "__main__":
    print("Submitting to Mimiry — expect ~2 min cold start...")
    print(gpu_name.remote())
