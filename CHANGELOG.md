# Changelog

All notable changes to the `mimiry` SDK are documented here. This project
roughly follows [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **`@mimiry.function()` and `mimiry.run()` default to a job that can run.**
  The old defaults were a `T4`, which no provider carries, and an
  `nvcr.io/nvidia/cuda` image the platform cannot pull without registry
  credentials, so a bare decorator always failed after a paid session. The
  defaults are now the `A100` family and `nvcr.io/nvidia/pytorch:24.01-py3`
  (Python 3.10); docstrings, README, TESTING.md and `examples/` no longer suggest
  `provider="gcp"`, which does not exist.
- **A GPU family with several sizes is no longer refused as ambiguous.**
  `gpu="A100"` matches both the 40 GB and 80 GB A100 on the live catalog and
  used to raise. `gpu.types` is a preference list on the API, so the SDK now
  sends every matching type, cheapest first, and the platform picks.
- **A location hint is honoured even without a provider hint.** The
  availability preflight ignored `location` unless `provider` was also set,
  so a session adopting a volume's location could be submitted to a location
  with no matching GPU and die with "no GPU matches criteria" after it
  existed. It is now refused locally, naming the locations that do offer it.
- **A Python-version mismatch no longer costs a session.** Your function is
  shipped to the container as a cloudpickle blob, which does not load on a
  different Python minor version — the container crashed on arrival, the SDK
  then spent its full five-minute SSH budget on the dead host, and the user
  was billed and told sshd was unreachable. The caller's version is now sent
  to the container, which refuses the payload before unpickling it and names
  both versions. An image that declares its Python (new
  `Image.python_version("3.11")`) is checked locally instead, so the call is
  refused before a session is created.
- **A dead session ends the SSH wait immediately.** `wait_for_sshd` now takes
  the same terminal-state check `wait_for_remote_file` already had, and the
  resulting failure carries the container's own log tail rather than an SSH
  transport error.
- **`session create --volume` settles the location before creating anything.**
  With no `--location` the session adopts the volume's location; a conflicting
  one is refused locally, naming both, instead of the platform killing the
  session seconds after it exists. Volumes from two different locations are
  refused as well. If the volume list can't be read, the request goes through
  unchanged.

### Changed
- When a session ends before producing a result and the platform reports an
  `error` (no capacity, no matching candidate), that error is the message.
  The old text blamed the user's command for a failure that never reached a
  container.
- `function.py` and `run.py` no longer restate the terminal state names; both
  derive them from `_session.TERMINAL_STATES`, which is what makes them
  recognise `exited` and `pull_failed`.
- Bootstrap failures are written to the container's log as well as the error
  file, so the reason survives the container exiting.
- `volume create --location` and `session create --location` help text now say
  the location binds attachment rather than calling it a hint.

## [0.3.3] — 2026-06-15

### Fixed
- **Default `gpu="T4"` works again.** The compute API now accepts only concrete
  GPU catalog names (e.g. `T4_16G_PCIe`) and rejects family aliases such as `T4`
  at session-create. `@mimiry.function`, `mimiry.run`, and the CLI now resolve a
  GPU family alias to the single available concrete name before submitting; an
  ambiguous family (several available variants) raises an actionable error
  listing them instead of a backend 400. Falls back to the original value when
  availability can't be consulted (unchanged best-effort behaviour).

### Added
- **Integrity-checked remote results.** A function's return value now travels in
  an HMAC-signed envelope that the SDK verifies before deserializing it; a
  payload whose signature doesn't match is rejected with the new
  `ResultIntegrityError` (exported from the top-level package). `ResultParseError`
  continues to signal a verified-but-unparseable payload.
- **Payload size guard.** `pack_call` now warns when the encoded call payload
  exceeds the 256 KB soft limit (it is transmitted as a container environment
  variable, which has practical size limits) and points to volumes/buckets for
  large data.

### Changed
- The distribution name declared in `pyproject.toml` is now lowercase `mimiry`,
  matching the import package and the published PyPI project. No functional
  change.

### Removed
- Dead `wait_for_marker` log-polling helper (was defined but unused).

## [0.3.2] — 2026-06-10

### Documentation
- README: simplify the "Managing sessions" intro wording (drop the inaccurate
  "no Python required" — the CLI is itself a Python package).

## [0.3.1] — 2026-06-10

### Added
- `mimiry help` as a friendly alias for `mimiry --help`; running bare `mimiry`
  now prints full help instead of an "invalid choice" error.

### Documentation
- README: add a top-level **CLI** section (between Auth and GPU types) that
  points users at `mimiry --help` / `mimiry <command> --help` for command
  discovery, and document the CLI in the "What works" summary and the tagline.

## [0.3.0] — 2026-06-08

A major CLI expansion: the `mimiry` CLI now covers the full session lifecycle
plus volumes and account insight — enough to replace the external `mirc` helper
for day-to-day use. (Supersedes the never-released 0.2.4, which introduced the
first read-only session commands.)

### Added
- **Full session lifecycle in the CLI:**
  - `mimiry session create --image … --gpu … [--provider --location --command --env KEY=VAL --volume NAME:MOUNT --auto-terminate … --no-ssh --wait]` — launch a GPU/shell job
  - `mimiry sessions [--active] [--limit N] [--json]` / `mimiry session list`
  - `mimiry session status <id> [--events N] [--wait]`
  - `mimiry session logs <id> [--tail N] [--timestamps] [--follow]` — `--follow` streams until the session ends
  - `mimiry session ssh <id>` — interactive shell into a running session
  - `mimiry session terminate <id>`
- **Volume management:** `mimiry volume create|list|status|extend|delete`, backed by new client methods (`create_volume`, `list_volumes`, `get_volume`, `extend_volume`, `delete_volume`). Attach at launch with `mimiry session create --volume NAME:MOUNT`.
- **Account/insight:** `mimiry transactions` (credit history), `mimiry whoami` (verify auth + balance), `mimiry config` (resolved settings, no network).
- **Richer `availability` filters:** `--provider`, `--location`, `--min-vram`, `--available-only` (alongside `--gpu-family`).
- `mimiry --version`.

### Changed
- Scriptability: `session status` exits non-zero on `failed`/`provision_failed`/`stopped`; `--json` available on list commands.

## [0.2.3] — 2026-06-08

### Fixed
- **`@mimiry.function` / `mimiry.run` bootstrap now works with `Image.pip_install`
  on minimal and default images.** Previously a function that added pip
  dependencies on the default Ubuntu-24.04 CUDA image died during startup with
  `bash: line 1: pip: command not found` (exit 127). Two causes, both fixed:
  - The image install prefix now runs **after** the bootstrap ensures
    `python3`/`pip` exist, not before.
  - `Image.install_prefix()` now invokes pip as
    `python3 -m pip install --break-system-packages …` — a bare `pip` is absent
    on minimal images, and Ubuntu 24.04+ blocks system installs under PEP 668.
- **Fail fast on premature container exit.** When a session reaches a terminal
  state before producing a result (e.g. a failed install), the SDK now raises
  `SessionFailed` with the tail of the container logs, instead of blundering
  into a 300-second SSH timeout that masked the real cause.

### Added
- **Pre-create GPU/provider/location validation.** Requesting a GPU from a
  provider that doesn't offer it (e.g. `T4` from `verda`) now fails immediately
  with an actionable `SessionError` naming the providers that *do* offer it,
  instead of failing only after a provisioning round-trip. The check is
  best-effort: a flaky `/availability` endpoint never blocks a valid job.

### Changed
- `mimiry.__version__` is now kept in sync with the package version (was stale
  at `0.2.0`).

Verified end-to-end on real T4 hardware (gcp `europe-west4-a`): a
`@mimiry.function` PyTorch training job runs on the GPU and returns its metrics
to the local process.

## [0.2.2] — 2026-06-08

### Added
- **Config-file persistence for auth.** `mimiry setup` now saves the SSH key
  *path* to `~/.config/mimiry/config.toml` so the SDK works immediately and in
  every future shell, with no restart. Key-path resolution precedence:
  explicit `configure()` > `MIMIRY_SSH_KEY` > config file. The file is written
  `0600` and is refused if group/other-writable (key-redirection guard); it
  stores only the path, never key material. The `MIMIRY_SSH_KEY` shell export
  remains as a secondary convenience.
- Test coverage for the setup wizard and config persistence.
