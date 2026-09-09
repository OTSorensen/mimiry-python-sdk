"""The decorator's and ``mimiry.run``'s defaults must describe a job the
platform can actually run. They used to default to a GPU no provider carries
and to an image the platform cannot pull, so a bare ``@mimiry.function()``
could never succeed — every user had to discover both facts by paying for a
failed session.
"""

from __future__ import annotations

import inspect
import sys

import mimiry

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
