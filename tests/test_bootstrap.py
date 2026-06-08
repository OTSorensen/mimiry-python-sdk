"""Tests for image install-prefix rendering and bootstrap command ordering.

Regression cover for the `pip: command not found` (exit 127) failure: a bare
`pip` ran on a minimal CUDA image *before* the bootstrap installed python/pip.
"""

from __future__ import annotations

from mimiry._serialization import build_bootstrap_script
from mimiry.image import Image


# ────────────────────────── Image.install_prefix ──────────────────────────


def test_pip_install_uses_python3_m_pip_with_break_system_packages():
    pfx = Image.from_registry("x").pip_install("torch", "numpy").install_prefix()
    # Must go through `python3 -m pip` (bare `pip` is often absent) and pass
    # --break-system-packages (PEP 668 on Ubuntu 24.04+).
    assert "python3 -m pip install --break-system-packages" in pfx
    assert "torch" in pfx and "numpy" in pfx
    # Must NOT be a bare `pip install ...`.
    assert "pip install" not in pfx.replace("python3 -m pip install", "")


def test_apt_install_still_rendered():
    pfx = Image.from_registry("x").apt_install("ffmpeg", "git").install_prefix()
    assert "apt-get install -y -q ffmpeg git" in pfx


def test_empty_install_prefix_is_blank():
    assert Image.from_registry("x").install_prefix() == ""


def test_apt_before_pip_in_prefix():
    pfx = Image.from_registry("x").apt_install("libsndfile1").pip_install("torch").install_prefix()
    assert pfx.index("apt-get") < pfx.index("python3 -m pip")


# ────────────────────────── bootstrap ordering ──────────────────────────


def test_install_prefix_runs_after_python_and_pip_are_ensured():
    s = build_bootstrap_script(image_install_prefix="MARKER_INSTALL")
    assert "MARKER_INSTALL" in s
    # The regression: python3 + pip must be ensured BEFORE the image's pip install.
    assert s.index("command -v python3") < s.index("MARKER_INSTALL")
    assert s.index("python3 -m pip --version") < s.index("MARKER_INSTALL")


def test_bootstrap_without_prefix_is_still_wrapped_and_ensures_python():
    s = build_bootstrap_script()
    assert s.startswith("bash -c ")
    assert "command -v python3" in s
