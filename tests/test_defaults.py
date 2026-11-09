"""The decorator's and ``mimiry.run``'s defaults must describe a job the
platform can actually run. They used to default to a GPU no provider carries
and to an image the platform cannot pull, so a bare ``@mimiry.function()``
could never succeed — every user had to discover both facts by paying for a
failed session.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path

import mimiry
import mimiry._cli  # registers mimiry._cli in sys.modules
from mimiry import _config

# ``mimiry.function`` and ``mimiry.run`` are shadowed by the callables the
# package exports under the same names; reach the modules via sys.modules.
function_mod = sys.modules["mimiry.function"]
run_mod = sys.modules["mimiry.run"]


def test_decorator_and_run_share_one_gpu_default():
    fn_default = inspect.signature(mimiry.function).parameters["gpu"].default
    run_default = inspect.signature(run_mod.run).parameters["gpu"].default
    cfg_default = function_mod.FunctionConfig().gpu
    assert fn_default == run_default == cfg_default == function_mod.DEFAULT_GPU


def test_default_gpu_is_not_a_type_no_provider_offers():
    # T4 was the old default: absent from every provider's catalog.
    assert function_mod.DEFAULT_GPU != "T4"
    assert function_mod.DEFAULT_GPU in {"A100", "H100"}


def test_default_image_is_the_spec_example_image():
    # The one image known to pull without registry credentials on the
    # platform is the OpenAPI spec's own example; NGC's ``nvidia/cuda``
    # images need authentication the platform does not yet supply.
    img = inspect.signature(mimiry.function).parameters["image"].default
    assert img == function_mod.DEFAULT_IMAGE
    assert img == "nvcr.io/nvidia/pytorch:24.01-py3"
    assert "nvidia/cuda" not in img


def test_docstring_examples_do_not_name_a_provider_that_does_not_exist():
    for doc in (mimiry.function.__doc__ or "", run_mod.run.__doc__ or ""):
        assert doc
        assert "gcp" not in doc
        assert 'gpu="T4"' not in doc


def test_default_api_base_is_the_live_environment():
    # A fresh install must reach the environment users register on; beta and alpha are retired.
    assert _config.DEFAULT_API_BASE == "https://platform.mimiry.com"


def test_unconfigured_api_base_resolves_to_platform(monkeypatch, tmp_path):
    # Start from a fresh process-global config and an empty config dir, so neither an earlier test's
    # configure() call nor the developer's own ~/.config/mimiry decides the result.
    monkeypatch.setattr(_config, "_config", _config.Config())
    monkeypatch.delenv("MIMIRY_API_BASE", raising=False)
    monkeypatch.delenv("MIMIRY_SSH_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not _config.config_path().exists()
    assert _config.get_config().api_base == "https://platform.mimiry.com"


def test_cli_help_names_no_provider():
    # Provider names are withheld from users; help text must not leak one.
    for args in (["availability", "--help"], ["session", "create", "--help"], ["--help"]):
        proc = subprocess.run(
            [sys.executable, "-m", "mimiry._cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        # A CLI that fails to start prints no help, and empty output would pass the check below.
        assert proc.returncode == 0, (args, proc.stderr)
        assert "usage: mimiry" in proc.stdout, args
        assert "verda" not in proc.stdout.lower() and "nebius" not in proc.stdout.lower(), args


def test_cli_help_names_the_default_host():
    # The help text reads the host from DEFAULT_API_BASE, so it cannot name a retired one.
    proc = subprocess.run(
        [sys.executable, "-m", "mimiry._cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage: mimiry" in proc.stdout
    assert "platform.mimiry.com" in proc.stdout
    assert "beta.mimiry.com" not in proc.stdout


def test_package_is_marked_stable():
    # Version 1 of the platform: PyPI renders the README and classifiers and they can never be
    # edited after upload, so both must say stable before a release is built.
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    statuses = re.findall(r'"Development Status :: ([^"]+)"', pyproject)
    assert statuses == ["5 - Production/Stable"]
    readme = (root / "README.md").read_text()
    assert "**Status:** stable" in readme
    assert "beta" not in readme.lower()


def test_cli_help_reads_the_host_from_default_api_base(monkeypatch, capsys):
    # Swap the constant for a sentinel: only help text built from DEFAULT_API_BASE can show it.
    cli_mod = sys.modules["mimiry._cli"]
    monkeypatch.setattr(cli_mod, "DEFAULT_API_BASE", "https://sentinel.invalid")
    try:
        cli_mod.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "usage: mimiry" in out
    assert "(default: sentinel.invalid)" in out
