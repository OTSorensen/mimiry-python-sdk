# mimiry — Python SDK for Mimiry GPU compute

**Status:** v0.2.0 (alpha)
**Backend:** softlaunch.mimiry.com (early beta)

Python-native interface for running GPU jobs on Mimiry: decorate a function,
call `.remote()`, get the result back. v1 wraps the existing
`/api/compute/v1/sessions` API — no backend changes required.

## Install (editable, from this directory)

```bash
pip install -e .
```

## Auth

The SDK uses SSH-JWT auth — the same SSH key registered on your Mimiry
account. The fastest way to get set up is the interactive wizard, which
generates a key (if needed), walks you through registering it in the portal,
writes `MIMIRY_SSH_KEY` to your shell profile, and verifies the connection:

```bash
mimiry setup   # alias: mimiry init
```

To configure auth manually instead, point the SDK at your private key:

```bash
export MIMIRY_SSH_KEY=~/.ssh/mimiry
```

Or pass `ssh_key_path=` explicitly to `mimiry.configure()`.

## GPU types and providers

Not every GPU type is offered by every provider. Today (2026-05-31) T4 is only
available on `gcp` (europe-west4-a); Verda offers higher-end GPUs (V100_16G,
A100_80G, A100_160G, B200/B300/H200). Quota cap on the softlaunch account is 2
concurrent sessions.

Always check `/availability` before assuming a provider supports a given GPU:

```bash
mimiry availability --gpu-family T4
```

Pass `provider="gcp"` explicitly when the SDK's default routing is wrong for
the GPU you want. v2 will auto-resolve this via `/availability`.

## Python version compatibility

`cloudpickle` serializes the user's function as a Python code object. Code
objects are **not** portable across `major.minor` Python versions — a function
pickled by Python 3.12 cannot be unpickled by Python 3.10
(`TypeError: code expected at most 16 arguments, got 18`).

The SDK defaults `@mimiry.function` to an image based on
`nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04`, which ships Python 3.12 —
matching recent Ubuntu / Debian / Fedora local installs. If you override the
image, **the container's `python3` must be the same major.minor as your local
interpreter**, otherwise the bootstrap will write
`/tmp/mimiry_bootstrap_error: failed to unpickle payload` and exit non-zero.

The simplest way to confirm a match is `python3 --version` locally and inside
your image. v2 will auto-select a matching base image.

## Quickstart — one-shot function

```python
import mimiry

@mimiry.function(
    gpu="T4",
    provider="gcp",  # required for T4 today
    image="nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04",
)
def gpu_info():
    import subprocess
    return subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv"],
        text=True,
    )

print(gpu_info.remote())
```

## Quickstart — raw bash command

```python
import mimiry

result = mimiry.run(
    image="nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04",
    gpu="T4",
    provider="gcp",
    command="nvidia-smi",
)
print(result.logs)
```

## What works in v1

- `@mimiry.function(gpu=..., image=...)` decorator
- `.remote(*args, **kwargs)` — sync call, returns the function's return value
- `.map(iterable)` — sequential fanout (respects the 2-concurrent-session quota)
- `Image.from_registry(uri).pip_install(...).apt_install(...)` — basic image
  customisation (installs at container start; no real Dockerfile build)
- `mimiry.run(image, gpu, command)` — raw bash entrypoint
- SSH-JWT auth via existing key

## What's deferred to v2 (needs backend changes)

- `@mimiry.cls` + `@enter` + `@method` (warm container, persistent state)
- Sub-second warm-call latency (needs per-region image cache + warm pool)
- `.map()` with real parallelism > 2 (needs quota raise)
- Native Python-object result return (currently via stdout-sentinel, 64KB-ish limit)
- Pre-built image catalog / fast image layer caching

Every `.remote()` call today provisions a fresh VM. Measured cold start on
2026-05-31: **~3 min wall clock end-to-end** (submit → result returned), of
which ~2 min is VM provisioning and image pull, ~30 s is the bootstrap apt /
pip install layer inside the container.

## Examples

See `examples/`:

- `01_hello.py` — minimal nvidia-smi
- `02_cuda_probe.py` — port of `scripts/run_quick_test.sh` to SDK
- `03_bash_command.py` — `mimiry.run()` for ffmpeg-style jobs
