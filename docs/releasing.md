# Releasing the `mimiry` SDK to PyPI

The SDK has no publish workflow; every release is uploaded by hand from a
machine you control. This is the whole procedure. Nothing here needs to be
memorised; run it top to bottom each time.

## Words you will meet

- **PyPI** — the public package index `pip install` reads from.
- **build** — the tool that turns the repo into two files: a wheel (`.whl`)
  and a source archive (`.tar.gz`). Together they are "the distribution".
- **twine** — the tool that uploads those two files to PyPI. `pip` pulls
  from PyPI; `twine` pushes to it. It lives only in the venv you install it
  into; it is not part of the SDK.
- **API token** — the only credential PyPI accepts for uploads. Passwords
  stopped working for uploads in 2024. A token starts with `pypi-` and is
  shown once, when created.
- **`__token__`** — a fixed word, the same for every PyPI user, used as the
  username whenever you authenticate with a token. Two underscores each
  side. It is not something you look up; you type it as written.

## One-time: get a token that can upload `mimiry`

1. Log in at https://pypi.org with the account that owns the project
   (check at https://pypi.org/manage/project/mimiry/collaboration/ — your
   username must be listed as Owner or Maintainer).
2. Go to https://pypi.org/manage/account/token/ → "Add API token".
3. Name it (e.g. `mimiry-release-<machine>`), and under **Scope** choose
   **Project: mimiry**. An "Entire account" token also works but can upload
   to every project you own; the project-scoped one cannot leak further.
4. Copy the token now. It is shown once. Store it in a password manager.

If a token's scope is wrong, PyPI answers `403 Forbidden` — the same error
as a token from an account that is not a collaborator. Scope and account
are the two things to check on any 403.

## Every release

All commands from the SDK repo's default branch, on a clean checkout. The
release must be what is on `main`, not what is on your working tree.

### 1. Make sure `main` is what you want to ship

```bash
cd ~/projects/mimiry-python-sdk
git checkout main && git pull
git status            # must say "nothing to commit, working tree clean"
grep -n '^version' pyproject.toml
grep -n '__version__' src/mimiry/__init__.py
```

The two version lines must agree with each other and with the top entry of
`CHANGELOG.md`. If the version has not been bumped yet, bump it in both
files, date the changelog entry, commit, and push through the review gate
first. A release is never cut from an unpushed commit.

### 2. Build the distribution in a throwaway environment

Use a fresh venv so the build cannot pick up anything from your day-to-day
environment. Python 3.10 or newer.

```bash
rm -rf /tmp/mimiry-release
git clone https://github.com/OTSorensen/mimiry-python-sdk.git /tmp/mimiry-release
cd /tmp/mimiry-release
python3 -m venv .venv
.venv/bin/pip install --upgrade pip build twine
.venv/bin/python -m build
```

Cloning rather than copying guarantees the build matches `main` exactly.
`build` writes two files into `dist/`:

```
dist/mimiry-<version>-py3-none-any.whl
dist/mimiry-<version>.tar.gz
```

### 3. Check the files before uploading

```bash
.venv/bin/python -m twine check dist/*
```

Both lines must say `PASSED`. This checks the metadata PyPI will display
(description, classifiers); it does not talk to PyPI yet.

Optional but worth the minute — install the wheel into another fresh venv
and import it:

```bash
python3 -m venv /tmp/mimiry-smoke
/tmp/mimiry-smoke/bin/pip install dist/mimiry-*.whl
/tmp/mimiry-smoke/bin/python -c "import mimiry; print(mimiry.__version__)"
/tmp/mimiry-smoke/bin/mimiry --help | head -3
```

The version printed must be the one you are releasing.

### 4. Upload

The token must not end up in your shell history or in a file. `read -rs`
reads it from the keyboard without echoing and puts it only in the variable
`T`; `unset T` throws it away afterwards.

```bash
cd /tmp/mimiry-release
read -rs -p "PyPI token: " T; echo
TWINE_USERNAME=__token__ TWINE_PASSWORD="$T" .venv/bin/python -m twine upload dist/*
unset T
```

Line by line:

- `read -rs -p "PyPI token: " T` — prompts, you paste the token, nothing
  is displayed, it is stored in `T`. Press Enter.
- `TWINE_USERNAME=__token__` — the fixed username for token auth.
- `TWINE_PASSWORD="$T"` — the token itself, from the variable.
- `twine upload dist/*` — uploads both files.
- `unset T` — removes the token from the shell.

A good upload ends with:

```
View at:
https://pypi.org/project/mimiry/<version>/
```

### 5. Verify from the outside

PyPI can take a minute to serve a new version. Then, in a fresh venv with no
access to your repo:

```bash
python3 -m venv /tmp/mimiry-verify
/tmp/mimiry-verify/bin/pip install --upgrade mimiry
/tmp/mimiry-verify/bin/python -c "import mimiry; print(mimiry.__version__)"
```

Only when this prints the new version is the release done. Until then,
users running `pip install mimiry` still get the old one, whatever `main`
says.

### 6. Tag it

```bash
cd ~/projects/mimiry-python-sdk
git tag -a v<version> -m "mimiry <version>"
git -c core.hooksPath=/dev/null push origin v<version>
```

The tag is how a future reader finds the exact commit a PyPI version was
built from. The `-c core.hooksPath=/dev/null` skips the pre-push review
gate for this one push: a tag carries no diff and no ticket, so the gate
refuses it, and the commit it points at has already been reviewed on its
way into `main`. Never use that bypass for anything with a diff.

## When it goes wrong

| Message | Meaning | What to do |
|---|---|---|
| `403 Forbidden` | PyPI read the token and refused this upload | Check the token's **scope** (must be `Project: mimiry` or `Entire account`) and that the token's account is an Owner/Maintainer of the project. Mint a new one if in doubt; old tokens cannot be edited. |
| `401 Unauthorized` / `Invalid or non-existent authentication information` | Token not recognised at all | Usually a paste error (missing first character, trailing space). Re-run step 4. Also check the username is exactly `__token__`. |
| `400 File already exists` | This version is already on PyPI | PyPI never accepts the same version twice, even if the file differs. Bump the version, rebuild, upload again. There is no overwriting. |
| `twine check` fails | Metadata PyPI would reject or misrender | Read the message; it names the field. Fix `pyproject.toml` or `README.md`, commit, rebuild. |
| Upload succeeded but `pip install --upgrade` still gives the old version | PyPI's CDN has not caught up | Wait a minute and retry. If it persists past ten minutes, check https://pypi.org/project/mimiry/#history for the new version. |

Add `--verbose` to the `twine upload` line to see PyPI's full reason
instead of the one-word status; it can print request headers, so if you
share the output, first make sure the token is not in it.

## Housekeeping

- `pyproject.toml` `authors` is what PyPI shows on the project page. Keep
  it at the address you want public.
- Never build from a working tree with uncommitted changes, and never
  upload a version that is not tagged on the same commit. A user who reports
  a bug against "0.4.0" must be reading code you can find.
