# TOOLING-001: Install the code-review gate and its supporting rules

## Context

This repo had no engineering rules file, no ticket convention, and no review
gate. Work was pushed on judgment alone. Two defects that reached open PRs
would have been caught by a review: a client method whose HTTP verb and field
name contradicted the OpenAPI spec (broken on every call, with a passing unit
test), and a token cache whose cleanup left live credentials on disk.

The review harness itself already exists in a separate repo and is proven in
another codebase. What was missing here was the repo-specific half: rules to
review against, a ticket to verify intent against, a specialist suited to this
codebase, and a hook to make it fire.

## Goal

Make an independent code review run automatically before every push to this
repo, with a rules file and an API-contract yardstick specific to a Python SDK
that wraps a documented HTTP API.

## Acceptance criteria

1. A push triggers the review without the developer invoking it.
2. The review refuses to run without a resolvable ticket, and the refusal
   explains how to satisfy it.
3. The context pack inlines this repo's engineering rules — the rules section
   is not empty.
4. The pack inlines a machine-generated digest of the API contract, small
   enough not to crowd out the diff.
5. The digest is generated from the OpenAPI specs, never hand-maintained, and
   regenerating it twice produces identical output.
6. The third specialist slot reviews API-contract conformance rather than SQL
   privileges, which this repo has none of.
7. The vendored skill copy records the upstream commit it came from and every
   intentional divergence, and the staleness check is runnable.
8. Review reports are ignored by git; the skill itself is tracked.

## Non-goals

- Changing the review harness upstream. The roster swap is local to this repo;
  the canonical repo keeps its SQL specialist for the codebase that needs it.
- Blocking pushes on findings. Findings stay advisory (`CRA_BLOCKING=1` would
  change that); only the hook's own refusals stop a push.
- Reviewing the existing open PRs as part of this change.

## Verification

The hook was exercised against a real push rather than inspected:

- `CLAUDECODE=1 git push --dry-run` → exit 10, push blocked (foreground guard)
- push with no ticket → exit 5, with the three ways to satisfy it
- a full live review ran end to end, dispatched three parallel specialists,
  and produced four medium findings — two of which were confirmed as real
  defects by independent reproduction, not taken on the reviewer's word.

Digest determinism checked by regenerating and diffing. Staleness check and
divergence diff both run clean against upstream.
