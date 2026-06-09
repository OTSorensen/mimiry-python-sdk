# 0.3.0 Manual Test Sheet

A hands-on checklist to validate the expanded CLI before release. Work top to
bottom. **💸 = spends credit (launches a real GPU session)**; everything else is
a free read. A teardown section at the end removes anything that could linger.

## Setup

```bash
cd ~/projects/mimiry-test
source .venv/bin/activate
# Install the candidate build (either the local wheel or the editable branch):
pip install -U /home/olive/projects/ml-engineer/mimiry-sdk-python/dist/mimiry-0.3.0-py3-none-any.whl
#   …or, to track the branch:  pip install -e /home/olive/projects/ml-engineer/mimiry-sdk-python
mimiry --version          # expect: mimiry 0.3.0
```

- [ ] `mimiry --version` prints `mimiry 0.3.0`

---

## 1. Account & config (free)

| Step | Command | Expect |
|---|---|---|
| - [ ] | `mimiry config` | JSON with your `ssh_key_path` + `api_base`, **no network call** |
| - [ ] | `mimiry whoami` | "Authenticated against …" + your balance JSON |
| - [ ] | `mimiry balance` | balance JSON |
| - [ ] | `mimiry quota` | quota JSON |
| - [ ] | `mimiry transactions` | credit/debit history JSON |
| - [ ] | `mimiry transactions --limit 3` | at most 3 records |

## 2. Availability filters (free)

| Step | Command | Expect |
|---|---|---|
| - [ ] | `mimiry availability` | all GPU models |
| - [ ] | `mimiry availability --gpu-family T4` | only T4 |
| - [ ] | `mimiry availability --provider gcp --available-only` | only available gcp GPUs |
| - [ ] | `mimiry availability --min-vram 40` | only ≥40 GB cards |
| - [ ] | `mimiry availability --location europe-west4-a` | only that location |

## 3. Sessions — read commands (free)

| Step | Command | Expect |
|---|---|---|
| - [ ] | `mimiry sessions` | aligned table, newest first |
| - [ ] | `mimiry sessions --active` | `No active sessions.` (or only running ones) |
| - [ ] | `mimiry sessions --json` | raw JSON array |
| - [ ] | `mimiry session list --limit 3` | at most 3 rows |
| - [ ] | `mimiry session status <id>` | full JSON for a known session |
| - [ ] | `mimiry session status <bad-id>` | clean `error: …`, exit code 1 (`echo $?`) |

## 4. Sessions — create / logs / ssh / terminate 💸

> Uses a real T4. Pin `--provider gcp` (T4 is gcp-only). Cold start ~2 min.

```bash
mimiry session create \
  --image nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04 \
  --gpu T4 --provider gcp --location europe-west4-a \
  --command "nvidia-smi && echo DONE" --wait
```

- [ ] 💸 `create … --wait` prints `Created session <id>`, streams `state=…`, ends at `state=started`, prints the management hints
- [ ] `mimiry session logs <id>` shows the container output (eventually `nvidia-smi` + `DONE`)
- [ ] `mimiry session logs <id> --follow` streams live, then prints `-- session … --` when it ends (Ctrl-C to stop early)
- [ ] `mimiry sessions --active` shows it while running
- [ ] 💸 (interactive box) `mimiry session create --image …cuda…ubuntu24.04 --gpu T4 --provider gcp --wait` then **`mimiry session ssh <id>`** drops you into a shell (`nvidia-smi` works inside; `exit` to leave)
- [ ] `mimiry session terminate <id>` → `Terminated <id>.`
- [ ] after terminate, `mimiry session status <id>` shows a terminal state

## 5. Volumes (cheap — storage only, no GPU)

```bash
mimiry volume create --name test-vol --size-gb 50
```

- [ ] `volume create` returns JSON with a new `id`, `state: submitted`→`provisioned`
- [ ] `mimiry volume list` shows it (table); `--json` gives raw
- [ ] `mimiry volume status <id>` shows detail incl. `size_gb`, `attached_to`
- [ ] `mimiry volume extend <id> --size-gb 100` → size grows; shrinking (e.g. `--size-gb 10`) is rejected by the API
- [ ] 💸 (optional) attach at launch: `mimiry session create --image …ubuntu24.04 --gpu T4 --provider gcp --volume test-vol:/mnt/data --command "df -h /mnt/data" --wait` → logs show the mount
- [ ] `mimiry volume delete <id>` → `Delete requested…`; then `mimiry volume list --all` shows it `deleted`

## 6. Scriptability / ergonomics

- [ ] `mimiry session status <failed-id>; echo $?` → exit `1` on a failed session
- [ ] `mimiry --help`, `mimiry session --help`, `mimiry volume --help` all read cleanly
- [ ] `mimiry session` (no subcommand) → usage error, exit `2`

## 7. Teardown (avoid lingering cost)

```bash
mimiry sessions --active        # should be empty; terminate any stragglers:
mimiry session terminate <id>
mimiry volume list              # delete any test volumes:
mimiry volume delete <id>
```

- [ ] `mimiry sessions --active` → `No active sessions.`
- [ ] no `test-vol` left in `mimiry volume list`

---

### Notes / issues found
_(jot anything unexpected here as you go)_
