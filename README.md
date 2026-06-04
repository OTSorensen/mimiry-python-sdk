# mimiry — Python SDK for Mimiry GPU compute

**Status:** alpha — early access
**Backend:** softlaunch.mimiry.com (beta)

Python-native interface for running serverless cloud GPU jobs on Mimiry, with full control over locality and providers.

## Install

```bash
pip install mimiry
```

Or, for local development from a clone of this repo (editable install):

```bash
pip install -e .
```

## Auth

Running jobs requires a Mimiry account. The SDK authenticates with SSH-JWT — the same SSH key you register on your account at the [Mimiry portal](https://softlaunch.mimiry.com). The fastest way to get set up is the interactive wizard, which generates a key (if needed), walks you through registering it in the portal, writes `MIMIRY_SSH_KEY` to your shell profile, and verifies the connection:

```bash
mimiry setup   # alias: mimiry init
```

This is a one-time step — you're set going forward.

To configure auth manually instead, point the SDK at your private key:

```bash
export MIMIRY_SSH_KEY=~/.ssh/mimiry
```

Or pass `ssh_key_path=` explicitly to `mimiry.configure()`.

## GPU types and providers

Mimiry sources GPUs from both local datacenters and cloud providers across Europe and the US, spanning entry-level cards up to the latest high-end accelerators. You control locality and hardware requirements, as well as which providers to use.

Always check what's currently available before selecting hardware:

```bash
mimiry availability
```

To filter to a single GPU family, add `--gpu-family <FAMILY>`.

## Python version

Your local Python `major.minor` must match the Python inside your container
image. The SDK ships your function to the GPU with `cloudpickle`, which can't
move code objects across Python versions — e.g. a function pickled on 3.12
won't load on 3.10.

You don't need to think about this with the default image: it ships Python
3.12, matching recent Ubuntu / Debian / Fedora. It only matters if you set
`image=` yourself — pick one whose `python3` matches your local interpreter.
Confirm with `python3 --version` locally and inside the image; a mismatch
shows up as a failure to deserialize your function.

## Quickstart — one-shot function

```python
import mimiry

@mimiry.function(
    # Uses default hardware; run `mimiry availability` to choose a GPU/provider.
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
    command="nvidia-smi",
)
print(result.logs)
```

## What works in this version

- `@mimiry.function(gpu=..., image=...)` decorator
- `.remote(*args, **kwargs)` — sync call, returns the function's return value
- `.map(iterable)` — runs the function over an iterable, sequentially
- `Image.from_registry(uri).pip_install(...).apt_install(...)` — basic image customisation (installs at container start; no real Dockerfile build)
- `mimiry.run(image, gpu, command)` — raw bash entrypoint
- SSH-JWT auth via existing key

## Examples

See `examples/`:

- `01_hello.py` — minimal `nvidia-smi` on a GPU
- `02_cuda_probe.py` — probe the GPU (driver, CUDA, device count) and return a structured Python dict
- `03_bash_command.py` — run an arbitrary shell command with `mimiry.run()`
