"""Wrap a user function + args for remote execution, and parse the result back.

# Trust model

cloudpickle is used in both directions: we serialize the user's function +
args (going out) and the function's return value (coming back). This is the
same pattern Ray and Dask use. The serialized blobs travel only
between the local Python process and a Mimiry session created by the same
authenticated account — i.e., code we authored running on a container our
own credentials provisioned. There is no untrusted-source deserialization
path here.

Do not use this module's ``parse_result`` on payloads from other origins.

# Wire format (SSH transport, v1)

The softlaunch ``/logs`` endpoint is currently unusable for live stdout
retrieval, so the SDK fetches results via SSH instead. The container:

  1. Reads the base64-encoded cloudpickle of ``(fn, args, kwargs)`` from
     the ``MIMIRY_FN_PAYLOAD_B64`` env var.
  2. Calls ``fn(*args, **kwargs)``.
  3. Cloudpickles ``{"ok": True, "result": value}`` (or
     ``{"ok": False, "error": {...}}``) and writes the base64-encoded blob
     to ``/tmp/mimiry_result.b64``.
  4. Blocks until the SDK creates ``/tmp/mimiry_done`` (or a long hard
     timeout passes — bounds runaway cost).
  5. Exits, triggering ``auto_terminate: on_complete``.

If the bootstrap can't even decode the payload (e.g., pip install of
cloudpickle failed), it writes an error message to
``/tmp/mimiry_bootstrap_error`` so the SDK's SSH poller can surface it.
"""

from __future__ import annotations

import base64
import textwrap

import cloudpickle

from mimiry._ssh import CONTAINER_HOLD_TIMEOUT_SECONDS, DONE_FLAG, ERROR_FILE, RESULT_FILE
from mimiry.exceptions import ResultParseError

_PAYLOAD_ENV = "MIMIRY_FN_PAYLOAD_B64"

# Soft size limit before we warn. Mimiry's command/env limits aren't publicly documented;
# 256 KB has been safe in practice.
PAYLOAD_SOFT_LIMIT_BYTES = 256 * 1024


def pack_call(fn, args: tuple, kwargs: dict) -> str:
    """Cloudpickle ``(fn, args, kwargs)`` and return a base64 string suitable for
    embedding in an environment variable.
    """
    blob = cloudpickle.dumps((fn, args, kwargs))
    return base64.b64encode(blob).decode("ascii")


def build_bootstrap_script(image_install_prefix: str = "") -> str:
    """Return the shell command the container executes.

    ``image_install_prefix`` (from :class:`mimiry.image.Image`) is run BEFORE
    the Python bootstrap so that pip/apt deps land first.
    """
    py_bootstrap = textwrap.dedent(
        f'''
        import base64, os, subprocess, sys, time, traceback

        def _write_error(msg: str) -> None:
            try:
                with open("{ERROR_FILE}", "w") as f:
                    f.write(msg)
            except Exception:
                pass

        try:
            import cloudpickle
        except ImportError:
            # Fallback: shell bootstrap should have installed this already, but
            # belt+suspenders. Fixed argv (no user input) — using subprocess.run
            # with a list avoids spawning a shell. --break-system-packages is
            # required on Ubuntu 24.04+ (PEP 668).
            rc = subprocess.run(
                [
                    "python3", "-m", "pip", "install",
                    "--quiet", "--no-input", "--break-system-packages",
                    "cloudpickle",
                ],
                check=False,
            ).returncode
            if rc != 0:
                _write_error("failed to install cloudpickle (pip exit %d)" % rc)
                sys.exit(4)
            import cloudpickle  # noqa: E402

        _b64 = os.environ.get("{_PAYLOAD_ENV}", "")
        if not _b64:
            _write_error("{_PAYLOAD_ENV} not set")
            sys.exit(2)

        try:
            fn, args, kwargs = cloudpickle.loads(base64.b64decode(_b64))
        except Exception:
            _write_error("failed to unpickle payload:\\n" + traceback.format_exc())
            sys.exit(3)

        try:
            result = fn(*args, **kwargs)
            payload = {{"ok": True, "result": result}}
        except BaseException as exc:
            payload = {{
                "ok": False,
                "error": {{
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }},
            }}

        try:
            wire = base64.b64encode(cloudpickle.dumps(payload)).decode("ascii")
        except Exception:
            wire = base64.b64encode(
                cloudpickle.dumps({{
                    "ok": False,
                    "error": {{
                        "type": "ResultSerializationError",
                        "message": "return value was not cloudpickle-serializable",
                        "traceback": traceback.format_exc(),
                    }},
                }})
            ).decode("ascii")

        # Write atomically: rename(tmp, final) so an SSH poller never sees a partial file.
        _tmp = "{RESULT_FILE}.partial"
        with open(_tmp, "w") as f:
            f.write(wire)
        os.replace(_tmp, "{RESULT_FILE}")

        # Block until the SDK signals done, or a hard timeout passes (bounds runaway cost).
        _deadline = time.time() + {CONTAINER_HOLD_TIMEOUT_SECONDS}
        while time.time() < _deadline:
            if os.path.exists("{DONE_FLAG}"):
                break
            time.sleep(1)
        '''
    ).strip()

    py_b64 = base64.b64encode(py_bootstrap.encode()).decode("ascii")

    # NB: ``set -e`` means any non-zero exit (apt-get update timeout, pip resolver
    # failure, etc.) aborts the container before our error-file handler can run.
    # We keep ``set -e`` because partial bootstraps that silently proceed produce
    # confusing failures downstream — but we use --no-install-recommends to keep
    # the install footprint small. Empirically, ``python3 python3-pip`` pulls
    # perl-base + ~50 transitive packages and takes 3+ minutes on a T4 VM;
    # ``--no-install-recommends python3-minimal python3-pip`` lands in ~30 s.
    #
    # ``--break-system-packages`` on pip: Ubuntu 24.04+ marks the system Python
    # environment as PEP 668 "externally managed" and blocks ``pip install`` by
    # default. The flag tells pip to proceed anyway. We accept the risk because
    # this is a single-purpose container with a fresh root filesystem — there's
    # no system Python to break.
    parts: list[str] = ["set -e"]
    # Ensure python3 + pip FIRST — before the image's install prefix, which often
    # needs pip (e.g. Image.pip_install renders ``python3 -m pip install ...``).
    # Running the prefix before this defeats the whole purpose: on a minimal image
    # (nvidia/cuda runtime) the prefix's pip wouldn't exist yet and `set -e` would
    # abort the container with exit 127 before anything else ran.
    parts.append(
        'if ! command -v python3 >/dev/null 2>&1; then '
        'DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
        'DEBIAN_FRONTEND=noninteractive apt-get install -y -q '
        '--no-install-recommends python3-minimal python3-pip ca-certificates '
        '>/dev/null 2>&1; '
        'fi'
    )
    parts.append(
        'if ! python3 -m pip --version >/dev/null 2>&1; then '
        'DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
        'DEBIAN_FRONTEND=noninteractive apt-get install -y -q '
        '--no-install-recommends python3-pip >/dev/null 2>&1; '
        'fi'
    )
    # Now the image's own deps (apt/pip) — pip is guaranteed to exist by here.
    if image_install_prefix:
        parts.append(image_install_prefix)
    # Install cloudpickle eagerly in the shell so we can fail fast with apt-cache
    # already warm. ``--break-system-packages`` is harmless on older Ubuntu (pip
    # ignores it) and required on 24.04+.
    parts.append(
        'if ! python3 -c "import cloudpickle" 2>/dev/null; then '
        'python3 -m pip install --quiet --no-input --break-system-packages '
        'cloudpickle >/dev/null 2>&1; '
        'fi'
    )
    parts.append(f'echo {py_b64} | base64 -d | python3 -')
    body = " && ".join(parts)
    return f'bash -c {_shell_quote(body)}'


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe embedding in a bash command."""
    if "'" not in s:
        return f"'{s}'"
    return "'" + s.replace("'", "'\\''") + "'"


def parse_result(b64_payload: str) -> object:
    """Decode the base64 blob fetched from the remote ``mimiry_result.b64`` file
    and return the deserialized result value.

    Raises :class:`ResultParseError` if the payload can't be decoded.

    If the function raised inside the container, that exception is re-raised
    locally as a :class:`RemoteFunctionError` carrying the remote traceback.
    """
    b64 = b64_payload.strip()
    if not b64:
        raise ResultParseError("empty result payload")
    try:
        payload = cloudpickle.loads(base64.b64decode(b64))
    except Exception as e:
        raise ResultParseError(f"failed to decode result payload: {e}") from e

    if not isinstance(payload, dict) or "ok" not in payload:
        raise ResultParseError(f"unexpected payload shape: {payload!r}")

    if payload["ok"]:
        return payload["result"]

    err = payload.get("error") or {}
    raise RemoteFunctionError(
        err.get("message", "remote function raised"),
        remote_type=err.get("type", "Exception"),
        remote_traceback=err.get("traceback", ""),
    )


class RemoteFunctionError(Exception):
    """Raised locally when the remote function raised inside the container."""

    def __init__(self, message: str, *, remote_type: str, remote_traceback: str) -> None:
        super().__init__(message)
        self.remote_type = remote_type
        self.remote_traceback = remote_traceback

    def __str__(self) -> str:
        base = super().__str__()
        return f"{self.remote_type}: {base}\n\nRemote traceback:\n{self.remote_traceback}"


def payload_env_var() -> str:
    """The env-var name the container reads the pickled payload from."""
    return _PAYLOAD_ENV
