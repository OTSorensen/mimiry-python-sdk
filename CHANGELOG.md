# Changelog

All notable changes to the `mimiry` SDK are documented here. This project
roughly follows [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

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
