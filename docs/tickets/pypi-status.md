# The PyPI page shows the SDK's real status

## Context

PyPI renders the README as the project page and the classifiers in its
sidebar. Both still described the SDK as alpha ("Status: alpha — early
access", `Development Status :: 3 - Alpha`), left over from an early release.
The platform is in public beta, so the page told every visitor the product
was less mature than it is. A published release's page cannot be edited, so
the fix needs a new version.

## Goal

The PyPI page for the next release states the SDK is in public beta, in both
the README status line and the classifier.

## Acceptance criteria

1. README line 3 reads `**Status:** public beta`.
2. `pyproject.toml` classifies the package `Development Status :: 4 - Beta`
   and no other development status.
3. The version is 0.4.2 in `pyproject.toml` and `mimiry.__version__`, and the
   changelog has a dated 0.4.2 entry describing the change.
4. No other line in the README or the package metadata changes.

## Non-goals

- Any code change.
- Rewording the rest of the README.

## Verification

`twine check` on the built files, then after upload the project page at
`https://pypi.org/project/mimiry/0.4.2/` shows "Status: public beta" and the
"Development Status :: 4 - Beta" classifier.
