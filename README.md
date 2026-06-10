# mimiry — Python SDK for Mimiry GPU compute

**Status:** alpha — early access
**Backend:** softlaunch.mimiry.com (beta)

Python-native interface for running serverless cloud GPU jobs on Mimiry, with full control over locality and providers — a decorator-based SDK plus a full-featured CLI for managing sessions and volumes.

## Install

```bash
pip install mimiry
```

Or, for local development from a clone of this repo (editable install):

```bash
pip install -e .
```

## Auth

Running jobs requires a Mimiry account. The SDK authenticates with SSH-JWT — the same SSH key you register on your account at the [Mimiry portal](https://softlaunch.mimiry.com). The fastest way to get set up is the interactive wizard, which generates a key (if needed), walks you through registering it in the portal, saves the key path to `~/.config/mimiry/config.toml` so the SDK works right away (and in every future shell, no restart needed), and verifies the connection. It also exports `MIMIRY_SSH_KEY` to your shell profile for `curl`/shell use:

```bash
mimiry setup   # alias: mimiry init
```

This is a one-time step — you're set going forward.

To configure auth manually instead, point the SDK at your private key:

```bash
export MIMIRY_SSH_KEY=~/.ssh/mimiry
```

Or pass `ssh_key_path=` explicitly to `mimiry.configure()`.

## CLI

Installing the package adds the `mimiry` command. To see every command and its
options:

```bash
mimiry --help            # list all commands (also: mimiry help)
mimiry <command> --help  # options for one command, e.g. `mimiry session create --help`
```

The sections below cover the common ones; everything is discoverable via `--help`.

## GPU types and providers

Mimiry sources GPUs from both local datacenters and cloud providers across Europe and the US, spanning entry-level cards up to the latest high-end accelerators. You control locality and hardware requirements, as well as which providers to use.

Always check what's currently available before selecting hardware:

```bash
mimiry availability
```

Filter with `--gpu-family T4`, `--provider gcp`, `--location europe-west4-a`,
`--min-vram 16`, and/or `--available-only`.

## Managing sessions

Run and manage GPU sessions entirely from the CLI — no Python required:

```bash
# Launch a job (omit --command for an interactive box; --wait blocks until it starts)
mimiry session create --image nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04 \
    --gpu T4 --provider gcp --command "nvidia-smi" --wait

mimiry sessions                 # list recent sessions, newest first
mimiry sessions --active        # only running / provisioning (i.e. still billing)
mimiry session status <id>      # full detail (--events N for history, --wait to block until done)
mimiry session logs <id>        # container logs (--tail N, --timestamps, --follow to stream)
mimiry session ssh <id>         # interactive shell into a running session
mimiry session terminate <id>
```

`mimiry session list` is the long form of `mimiry sessions`; add `--json` for
machine-readable output. `session create` also accepts `--env KEY=VAL`,
`--volume NAME:MOUNT`, `--gpu-count`, and
`--auto-terminate {never,on_complete,on_success}`.

## Volumes

Persistent block storage that survives session termination:

```bash
mimiry volume create --name data --size-gb 100
mimiry volume list                       # hides deleted; --all to include them
mimiry volume status <id>
mimiry volume extend <id> --size-gb 200  # grow only (cannot shrink)
mimiry volume delete <id>
```

Attach one at launch: `mimiry session create … --volume data:/mnt/data`.

## Account

```bash
mimiry balance        # remaining credit
mimiry quota          # usage limits
mimiry transactions   # credit/debit history
mimiry whoami         # verify auth end-to-end
mimiry config         # show resolved key path + API base (no network)
```

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

**Python SDK**

- `@mimiry.function(gpu=..., image=...)` decorator
- `.remote(*args, **kwargs)` — sync call, returns the function's return value
- `.map(iterable)` — runs the function over an iterable, sequentially
- `Image.from_registry(uri).pip_install(...).apt_install(...)` — basic image customisation (installs at container start; no real Dockerfile build)
- `mimiry.run(image, gpu, command)` — raw bash entrypoint
- SSH-JWT auth via existing key

**CLI** (`mimiry --help`)

- Sessions: `session create` / `list` / `status` / `logs [--follow]` / `ssh` / `terminate`
- Volumes: `volume create` / `list` / `status` / `extend` / `delete`
- Account: `balance`, `quota`, `transactions`, `whoami`, `config`
- `availability` with `--gpu-family` / `--provider` / `--location` / `--min-vram` / `--available-only`

## Examples

See `examples/`:

- `01_hello.py` — minimal `nvidia-smi` on a GPU
- `02_cuda_probe.py` — probe the GPU (driver, CUDA, device count) and return a structured Python dict
- `03_bash_command.py` — run an arbitrary shell command with `mimiry.run()`
