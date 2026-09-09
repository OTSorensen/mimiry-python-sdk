"""Tests for the caller-vs-container Python compatibility guard.

A function is shipped to the container as a cloudpickle blob, which does not
load on a different Python minor version: the container dies on arrival, the
SDK then waits out its SSH timeout, and the user is billed for a session that
never ran and told their network is at fault. These cover both halves of the
guard — the local refusal before a session is created, and the version the
container is given so it can refuse for an image that declared nothing.
"""

from __future__ import annotations

import base64
import re

import pytest

from mimiry._serialization import (
    build_bootstrap_script,
    caller_python_env_var,
    caller_python_version,
)
from mimiry.exceptions import MimiryError
from mimiry.image import Image, preflight_python_version

# ────────────────────── local preflight ──────────────────────


def test_mismatch_raises_before_anything_is_created():
    img = Image.from_registry("docker.io/pytorch/pytorch:2.4.0").python_version("3.11")
    with pytest.raises(MimiryError) as exc:
        preflight_python_version(img, "3.14")
    msg = str(exc.value)
    # Both versions named, and a fix the user can act on.
    assert "3.14" in msg and "3.11" in msg
    assert "cloudpickle" in msg


def test_matching_version_passes():
    img = Image.from_registry("x").python_version("3.11")
    preflight_python_version(img, "3.11")  # no raise


def test_patch_level_is_ignored():
    # major.minor is the granularity that decides loadability.
    img = Image.from_registry("x").python_version("3.11.9")
    assert img.declared_python_version == "3.11"
    preflight_python_version(img, "3.11")  # no raise


def test_undeclared_image_is_not_refused_locally():
    # Nothing is known about the image, so the container does the checking.
    img = Image.from_registry("x")
    assert img.declared_python_version is None
    preflight_python_version(img, "3.14")  # no raise


def test_malformed_declaration_is_rejected_at_declaration_time():
    with pytest.raises(MimiryError):
        Image.from_registry("x").python_version("python3")


def test_python_version_returns_the_image_for_chaining():
    img = Image.from_registry("x")
    assert img.python_version("3.11") is img


def test_caller_python_version_is_major_minor():
    assert re.fullmatch(r"\d+\.\d+", caller_python_version())


# ────────────────────── container-side check ──────────────────────


def _decode_bootstrap(script: str) -> str:
    """Recover the python source the bootstrap pipes into the interpreter."""
    blob = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", script)
    assert blob, "bootstrap no longer embeds a base64 python payload"
    return base64.b64decode(blob.group(1)).decode()


def test_container_compares_versions_before_unpickling():
    src = _decode_bootstrap(build_bootstrap_script())
    assert caller_python_env_var() in src
    # The comparison must happen before the load that would crash.
    assert src.index(caller_python_env_var()) < src.index("cloudpickle.loads")


def test_container_mismatch_message_names_both_versions():
    src = _decode_bootstrap(build_bootstrap_script())
    assert "python version mismatch" in src
    assert "_caller_py" in src and "_container_py" in src


def test_bootstrap_errors_also_reach_the_container_log():
    # The result file is unreachable once the container exits, so the log is
    # the only channel left for explaining a startup failure.
    src = _decode_bootstrap(build_bootstrap_script())
    body = src[src.index("def _write_error") : src.index("import cloudpickle")]
    assert "sys.stderr" in body
