# Default to version 1 of the platform; mark the SDK stable

## Context

The platform has moved from its public beta to version 1, served at
`https://platform.mimiry.com`. Beta is no longer supported. The released SDK
still defaults its API base to `https://beta.mimiry.com`, so a fresh
`pip install mimiry` sends every command to the retired environment. SSH keys
are registered per environment, so a user who registered on the platform gets
"SSH signature verification failed" from beta and debugs their key instead of
the host.

The PyPI page also still says "public beta", in the README status line and in
the `Development Status :: 4 - Beta` classifier. PyPI pages cannot be edited
after upload, so the release that moves the host must also carry the new
status.

## Goal

A fresh install reaches version 1 of the platform with no extra configuration,
and the package presents itself as stable.

## Acceptance criteria

1. `DEFAULT_API_BASE` is `https://platform.mimiry.com`.
2. With no `MIMIRY_API_BASE`, no `--api-base` and no saved config, the resolved
   API base is `https://platform.mimiry.com`.
3. An explicit `--api-base`, `MIMIRY_API_BASE`, or the `api_base` saved in the
   config file still takes precedence over the default.
4. `mimiry --help` names `platform.mimiry.com` as the default host and does not
   name `beta.mimiry.com`; the help text reads the host from
   `DEFAULT_API_BASE` instead of repeating it.
5. `pyproject.toml` carries exactly one development-status classifier,
   `Development Status :: 5 - Production/Stable`, and the README status line
   says stable and no longer mentions beta.
6. The README links the platform manager at `https://platform.mimiry.com`.
7. The package version is 0.5.0 in `pyproject.toml` and `mimiry.__version__`,
   and the changelog carries a 0.5.0 entry that tells beta users to register
   their key on the platform and re-run `mimiry setup`.
8. `CLAUDE.md` names `platform.mimiry.com` as the live host and records beta as
   retired; `TESTING.md` expects `mimiry config` to show
   `api_base: https://platform.mimiry.com`.

## Non-goals

- A migration or warning for a config file that `mimiry setup` saved with the
  beta host. No users of significance are on beta; a saved host keeps winning
  over the default, as before, and the changelog says how to move.
- Moving the token request to the auth path the version 1 spec now documents.
  The path the SDK uses still answers on the platform; changing it is separate
  work with its own contract-digest regeneration.
- Publishing to PyPI. The release is a separate manual step
  (`docs/releasing.md`).
- Changing test fixtures that use old hosts as arbitrary values to prove an
  explicit setting wins.

## Verification

`tests/test_defaults.py` pins criteria 1, 2, 4 and 5 (the help test asserts the
CLI exited 0 and printed usage before any negative check, and a second test
swaps `DEFAULT_API_BASE` for a sentinel to prove the help text is derived from
it). Criterion 3 is unchanged code, covered by the existing `test_precedence_*`
tests in `tests/test_config.py` (explicit beats env beats saved file, for both
the key path and `api_base`), which pass on this branch. Each of the default host, the classifier and
the README status line was reverted in turn and the suite failed each time.
Live check: with an empty config directory and no `MIMIRY_API_BASE`,
`mimiry whoami` reports `https://platform.mimiry.com` and succeeds with a key
registered there.
