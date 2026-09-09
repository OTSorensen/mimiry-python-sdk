# Defaults that describe a job the platform can run

## Context

`@mimiry.function()` and `mimiry.run()` defaulted to `gpu="T4"` and to an
`nvcr.io/nvidia/cuda` image. No provider on the platform carries a T4, and
the `nvidia/cuda` images on NGC need registry credentials the platform does
not supply, so the pull fails. The CLI already had its `--gpu` default
removed for the same reason; the decorator did not, so a bare
`@mimiry.function()` could never succeed and every new user discovered both
facts by paying for a failed session. The docstrings and README examples
compounded it by suggesting `provider="gcp"`, which does not exist.

The `nvcr.io/nvidia/pytorch:24.01-py3` image, the OpenAPI spec's own example,
pulls and runs on every provider today, and did in every prior test round.
The registry allowlist and pull credentials are a platform concern (a Harbor
registry is planned); the SDK's job is to default to what works now.

## Goal

The defaults name a GPU family every provider offers and an image the
platform can pull, and every example in docstrings, README and `examples/`
is one a user can paste and run.

## Acceptance criteria

1. `@mimiry.function()` with no arguments and `mimiry.run(image=..., command=...)`
   with no `gpu` resolve to the same GPU family, which is not `T4`.
2. The default image is `nvcr.io/nvidia/pytorch:24.01-py3`, not an
   `nvidia/cuda` image.
3. Neither `mimiry.function`'s nor `mimiry.run`'s docstring names `gcp` or
   `gpu="T4"`.
4. README and `examples/*.py` use the same image and no longer suggest
   `--gpu T4` or `--provider gcp`.
5. The comment beside the default image states the Python it ships and that
   the caller's Python minor must match, so a mismatch is not a surprise.

## Non-goals

- Probing the registry or the image's Python ahead of time.
- Any change to GPU alias resolution; `A100` resolves through the existing
  availability preflight exactly as a user-supplied alias does.

## Verification

Unit tests in `tests/test_defaults.py` pin each criterion. Live verification
is the real proof: `@mimiry.function()` with no arguments must provision,
pull, run, and return on the live API. Recorded in the alpha test log with
wall-clock and cost.
