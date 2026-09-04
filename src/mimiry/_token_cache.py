"""On-disk JWT cache, keyed by SSH key fingerprint + API base.

Without this, every ``mimiry`` CLI invocation re-signs a fresh SSH challenge
and POSTs ``/api/v1/auth/token`` for a brand-new JWT. The live auth endpoint
rate-limits aggressively (observed tripping after ~3 exchanges in its window),
so a handful of ordinary sequential commands self-throttle into 429s. Tokens
are valid for an hour; there is no reason a sequence of CLI calls inside that
hour should mint more than one.

Security posture — this file holds **bearer tokens**, unlike ``config.toml``
which holds only a path and a URL:

* written ``0600`` in a ``0700`` directory, atomically (temp file + ``os.replace``)
* refused on read if group/other **readable or writable** — a leaked token is
  directly replayable, so the read check is stricter here than for the config
* keyed by ``sha256(fingerprint|api_base)`` so switching key or host can never
  return the wrong account's token, and the raw fingerprint is not used as a
  filename
* stored under ``$XDG_CACHE_HOME`` (not the config dir) since it is
  regenerable, machine-local state that should never be synced or committed

The cache is strictly an optimisation: every failure path (missing, corrupt,
stale, wrong permissions, unwritable dir) degrades to a normal exchange rather
than raising, because a broken cache must never make the SDK less usable than
having no cache at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path

# Treat a token as unusable this far ahead of its stated expiry. Matches the
# in-process refresh buffer in ``_auth`` so a cached token and a live one make
# the same call/skip decision.
_EXPIRY_MARGIN_SECONDS = 300

# Group/other readable OR writable. Stricter than the config file's
# write-only check: this file's contents are a replayable credential.
_INSECURE_BITS = (
    stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
)  # 0o066

# Temp-file naming for the atomic write. Named constants because three places
# must agree: store() creates them, _sweep_orphans() removes stale ones, and
# clear() must delete them too — a cleanup that knows only the final artifact
# leaves live credentials behind.
_TMP_PREFIX = ".tok-"
_TMP_SUFFIX = ".tmp"
# An orphan younger than this may belong to a store() running right now in
# another process; leave it alone rather than racing it.
_ORPHAN_MIN_AGE_SECONDS = 60


def _cache_root() -> Path:
    """The user's cache root (``$XDG_CACHE_HOME`` or ``~/.cache``).

    The boundary for permission tightening: components BELOW it are ours to
    lock down, the root itself belongs to the user's wider setup.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    return Path(base).expanduser() if base else Path.home() / ".cache"


def cache_dir() -> Path:
    """Directory holding cached tokens (honours ``$XDG_CACHE_HOME``)."""
    return _cache_root() / "mimiry" / "tokens"


def _cache_key(fingerprint: str, api_base: str) -> str:
    """Opaque filename stem for a (key, host) pair.

    Hashed rather than used raw so the filename leaks neither the fingerprint
    nor the host, and can never contain path separators.
    """
    material = f"{fingerprint}|{api_base.rstrip('/')}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


def cache_path(fingerprint: str, api_base: str) -> Path:
    return cache_dir() / f"{_cache_key(fingerprint, api_base)}.json"


def load(
    fingerprint: str, api_base: str, *, now: float | None = None
) -> tuple[str, float] | None:
    """Return ``(access_token, expires_at)`` if a still-valid entry exists, else ``None``.

    ``None`` covers every failure mode — absent, unreadable, malformed,
    insecurely permissioned, or too close to expiry. Never raises: the caller
    falls back to a normal exchange.
    """
    path = cache_path(fingerprint, api_base)
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None

    # A group/other-writable containing directory lets another local user
    # unlink or replace entries even when the file's own mode is correct.
    # The file check below cannot see that, so check the directory too.
    try:
        dir_mode = path.parent.stat().st_mode
    except OSError:
        return None
    if dir_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _discard(path)
        return None

    # A world-readable token file is a leaked credential. Refuse it and remove
    # it, rather than silently continuing to use something already exposed.
    if st.st_mode & _INSECURE_BITS:
        _discard(path)
        return None

    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        _discard(path)
        return None

    if not isinstance(payload, dict):
        _discard(path)
        return None

    token = payload.get("access_token")
    expires_at = payload.get("expires_at")
    if not isinstance(token, str) or not token:
        _discard(path)
        return None
    if not isinstance(expires_at, (int, float)):
        _discard(path)
        return None

    # Defence in depth: the filename already encodes the pair, but verify the
    # recorded identity too, so a collision or a hand-copied file can't hand
    # back a token minted for a different key or host.
    if payload.get("fingerprint") != fingerprint:
        return None
    if str(payload.get("api_base", "")).rstrip("/") != api_base.rstrip("/"):
        return None

    current = time.time() if now is None else now
    if current + _EXPIRY_MARGIN_SECONDS >= float(expires_at):
        _discard(path)
        return None

    return token, float(expires_at)


def _mkdir_chain_private(leaf: Path) -> None:
    """Create ``leaf`` and its components under the cache root at ``0700``.

    ``Path.mkdir(parents=True)`` applies its ``mode`` only to the final
    component; intermediates are created at ``0777 & ~umask``. Under umask
    002/000 — shared-group hosts, many container images — that leaves an
    ancestor group-writable, which is enough for another local user to swap a
    directory beneath us. Walk the chain and set each component explicitly.

    Only components at or below the cache root are touched: ``$XDG_CACHE_HOME``
    (or ``~/.cache``) belongs to the user's wider setup, and silently
    tightening it would be a surprising side effect of caching a token.
    """
    root = _cache_root()
    # The root itself may not exist yet. Create it (and anything above it) with
    # default permissions — it is the user's directory, not ours to tighten.
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    chain: list[Path] = []
    node = leaf
    while True:
        chain.append(node)
        if node.parent == node or node.parent == root:
            break
        node = node.parent

    for directory in reversed(chain):
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            # A component we do not own: leave it. The caller's own checks
            # decide whether the result is still safe to use.
            pass


def store(fingerprint: str, api_base: str, access_token: str, expires_at: float) -> Path | None:
    """Persist a token ``0600``. Returns the path, or ``None`` if it couldn't be written.

    Best-effort by design: an unwritable cache directory (read-only home, odd
    container) must not break authentication, so failures are swallowed.
    """
    path = cache_path(fingerprint, api_base)
    payload = json.dumps(
        {
            "access_token": access_token,
            "expires_at": float(expires_at),
            "fingerprint": fingerprint,
            "api_base": api_base.rstrip("/"),
        }
    )

    try:
        # Create every component under the cache root at 0700, not just the
        # leaf: Path.mkdir(parents=True) applies `mode` ONLY to the final
        # component, so intermediates land at 0777 & ~umask — 0775 under a
        # common umask, group-writable under umask 002/000 (shared-group
        # hosts, many container images).
        _mkdir_chain_private(path.parent)
        # mkdir's mode is IGNORED when the directory already exists, so the
        # 0700 guarantee holds only for a directory this call just created.
        # Enforce it explicitly; the mode here is a security control, not a
        # convenience.
        os.chmod(path.parent, 0o700)
        # Sweep orphaned temp files before writing. An abnormal termination
        # between mkstemp and os.replace leaves a 0600 file holding a live
        # JWT that nothing else would ever remove: it is not the canonical
        # name, so load()/_discard never touch it and it never expires.
        _sweep_orphans(path.parent)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX)
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic on POSIX
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except OSError:
        return None
    return path


def _sweep_orphans(directory: Path, *, now: float | None = None) -> int:
    """Remove stale temp files left by an interrupted ``store()``.

    Each holds a live JWT at ``0600`` under a non-canonical name, so nothing
    else in this module would ever reclaim it. Only files older than
    ``_ORPHAN_MIN_AGE_SECONDS`` are touched, so a concurrent write is not
    raced. Best-effort: failures are ignored.
    """
    current = time.time() if now is None else now
    removed = 0
    try:
        candidates = list(directory.glob(f"{_TMP_PREFIX}*{_TMP_SUFFIX}"))
    except OSError:
        return 0
    for entry in candidates:
        try:
            if current - entry.stat().st_mtime < _ORPHAN_MIN_AGE_SECONDS:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _discard(path: Path) -> None:
    """Remove an unusable cache entry, ignoring failure."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def clear() -> int:
    """Delete all cached tokens. Returns how many files were removed.

    Covers both the canonical ``*.json`` entries and any ``.tok-*.tmp``
    orphans: both hold a usable bearer token, and a logout that leaves one
    behind reports a security guarantee it did not deliver. Orphans are
    removed regardless of age here — an explicit logout is a deliberate act,
    not the opportunistic sweep in ``store()``.
    """
    removed = 0
    root = cache_dir()
    try:
        entries = list(root.glob("*.json")) + list(root.glob(f"{_TMP_PREFIX}*{_TMP_SUFFIX}"))
    except OSError:
        return 0
    for entry in entries:
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed
