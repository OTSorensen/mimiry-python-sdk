# A written release procedure

## Context

Releases to PyPI are manual and were undocumented. The first release after
the 0.4.0 merge cost an hour of back-and-forth: the fixed `__token__`
username was read as a placeholder, the token's label was pasted where its
value belonged, a chat renderer stripped `*` and underscores from the
upload command, and the pre-push review gate refused the version tag
because a tag carries no diff. None of that is knowledge the repo held.

## Goal

Anyone with a PyPI token that can upload `mimiry` can cut a release from a
clean clone by reading one file, and the public author contact on PyPI is
the company address.

## Acceptance criteria

1. `docs/releasing.md` exists and covers: what each tool and word means,
   how to obtain a correctly scoped token, build from a fresh clone, check
   the distribution, upload with the token entered via `read -rs` so it
   never reaches shell history, verify from an outside venv, and tag.
2. The tag step states that the review-gate hook must be bypassed for the
   tag push and why, and that the bypass is never used for a push with a
   diff.
3. `docs/releasing.md` maps `403`, `401`, and "file already exists" to
   their causes and the action for each.
4. `pyproject.toml` `authors` carries `oliver.thor@mimiry.com`.

## Non-goals

- Automating the release (a publish workflow with trusted publishing). Worth
  doing; not this ticket.
- Teaching the gate to accept tag pushes on its own.

## Verification

The 0.4.0 release was performed following the document as written, and
`pip install --upgrade mimiry` in a fresh venv reported 0.4.0.
