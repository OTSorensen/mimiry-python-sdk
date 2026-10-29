# Default to the live environment; name no provider

## Context

The released SDK defaults its API base to `https://alpha.mimiry.com`, but users
register their SSH key on the current environment, `https://beta.mimiry.com`.
A fresh `pip install mimiry` therefore sends every command to an environment
where the user's key is unknown, and that environment answers an unknown key
with "SSH signature verification failed". The user debugs their signing
instead of the host. Every new user hits this on their first command unless
they already know to set `MIMIRY_API_BASE`.

Separately, the platform withholds provider names from users, but the CLI help
for `--provider`, the CLI module's examples, the `mimiry.run` docstring and the
README named one. Help text is the first thing a user reads, so it leaked the
name the platform is careful to hide.

## Goal

A fresh install reaches the environment users register on, with no extra
configuration; a user whose config file still names the retired host is told
how to move; and nothing the SDK prints or documents names a provider.

## Acceptance criteria

1. `DEFAULT_API_BASE` is `https://beta.mimiry.com`.
2. With no `MIMIRY_API_BASE`, no `--api-base` and no saved config, the resolved
   API base is `https://beta.mimiry.com`.
3. An explicit `--api-base`, `MIMIRY_API_BASE`, or the `api_base` saved in the
   config file still takes precedence over the default.
4. `mimiry --help`, `mimiry availability --help` and `mimiry session create --help`
   contain no provider name (checked by running the real CLI, not by reading
   source strings).
5. The `mimiry.run` docstring, the CLI module examples and the README name no
   provider.
6. A config file saved by 0.4.0's `mimiry setup` names the retired host. That
   value is kept (the user's key is registered there), and resolving it raises
   one `RetiredApiBaseWarning` saying how to move to the live host and how to
   stay. A retired host set through `MIMIRY_API_BASE` is a deliberate choice and
   does not warn; a saved live host does not warn.
7. The CLI prints that warning as a single `warning: ...` line, without
   Python's file, line and category prefix.
8. The changelog's 0.4.1 entry carries the upgrade note for setup users.
9. The package version is 0.4.1 in `pyproject.toml` and `mimiry.__version__`,
   and the changelog carries a 0.4.1 entry describing both fixes.

## Non-goals

- Removing the `--provider` flag. It stays until the platform decides whether
  provider filtering remains; only the example naming a provider goes.
- Publishing to PyPI. The release is a separate manual step
  (`docs/releasing.md`).
- Rewriting a saved retired host to the live one. The user's key is
  registered on the host they saved, so a silent switch would break them.
- Changing the test fixtures that use provider names or the old host; they
  exercise parsing and precedence, are not shipped help text, and the old host
  there is an arbitrary value proving an explicit setting wins.

## Verification

`tests/test_defaults.py` pins criteria 1, 2 and 4 (the help test asserts the
CLI exited 0 and printed usage before checking for provider names);
`tests/test_config.py` covers precedence (criterion 3) and the warning
(criteria 6 and 7). Each new test was shown to fail with its fix removed. Live check after release: in a fresh venv,
`pip install mimiry==0.4.1`, then with no `MIMIRY_API_BASE` set,
`mimiry whoami` must report `https://beta.mimiry.com` and succeed with a key
registered on beta.
