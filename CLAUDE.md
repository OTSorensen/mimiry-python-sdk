# mimiry-python-sdk — engineering rules

Python SDK + CLI wrapping the Mimiry compute API (GPU sessions, volumes,
account). Owner: Oliver (OTSorensen). Published to PyPI as `mimiry`.

This file is inlined into every code-review context pack, so it stays short.
Narrative and history belong in `CHANGELOG.md` and the bug reports, never here.

## Environment facts

- Live API host: `https://alpha.mimiry.com`. The OpenAPI specs'
  `softlaunch.mimiry.com` is **dead** — never reintroduce it as a default.
- Source layout is `src/`-based: `src/mimiry/`, tests in `tests/`.
- Supported Python: 3.10+. `tomllib` is 3.11+, so `_config.py` carries a
  3.10 fallback parser — keep both paths working.
- Dependencies are deliberately minimal (`httpx`, `cloudpickle`). Adding one
  is a decision, not a detail: this package is installed into users' own
  environments.
- No database, no SQL, no Supabase. Auth is SSH-signature → JWT.
- Integration testing happens in the separate `mimiry-alpha` repo against the
  live API; this repo holds unit tests only.

## Rules

1. **The OpenAPI spec is authoritative, not the SDK.** Every `MimiryClient`
   method must match the method, path, and request-body field names in
   `.claude/review-rules/api-contract.md`. A unit test that asserts the SDK's
   behaviour is not evidence of conformance: a client method has shipped
   completely broken while its own test passed, because the test encoded the
   same wrong HTTP verb and field name as the implementation.
2. **Regenerate the contract digest when the specs change:**
   `python3 scripts/gen_api_contract.py <mimiry-documentation> > .claude/review-rules/api-contract.md`.
   Never hand-edit the digest. It lives under `.claude/review-rules/` because
   that is what the context-pack builder inlines; in `docs/` the review
   specialist would never see it.
3. **Secrets never touch disk outside `0600` files.** The SSH key is the real
   credential; the JWT cache holds a replayable bearer token. Config
   (`~/.config/mimiry/config.toml`) holds only a path and a URL and is checked
   for group/other **writability**; the token cache is checked for group/other
   **readability** too, and a failing file is refused *and* deleted.
4. **A cache or convenience layer must degrade, never raise.** Missing,
   corrupt, stale, badly-permissioned, or unwritable state falls back to the
   normal path. A broken optimisation must never be worse than not having it.
5. **Auth verification paths never accept cached credentials.** `mimiry setup`
   exists to prove a key is registered; anything short of a real exchange
   makes it a false assurance.
6. **New behaviour ships with a test that would fail without it.** Assert the
   wiring, not the existence — a new CLI flag needs a test proving the flag
   reaches the call it changes. A test whose assertions sit under a condition
   the fixtures never satisfy is vacuous: assert the precondition, or assert
   unconditionally.
7. **No `TODO`/`FIXME` markers in `src/`.** Either fix it or file it in the
   bug reports.
8. **Public errors must be actionable.** Raise `AuthError`/`MimiryError` with
   what failed and what the user should do, never a bare status code.

## Code review

A review is required before pushing. The `.githooks/pre-push` hook delegates
to `.claude/skills/code-review-agent/scripts/pre-push-review.sh`
(`git config core.hooksPath .githooks` — already set in this clone; a fresh
clone must set it again).

**A resolvable ticket is a prerequisite** — the hook refuses the push (exit 5)
when none is found, because the review verifies the diff against the ticket's
acceptance criteria. Satisfy it in one of three ways:

- commit `docs/tickets/<slug>.md` alongside the work (preferred — the ticket
  ships with what it describes). A ticket committed inside the push range is
  matched by being in the diff, so its name is unconstrained; naming it after
  the branch is still worth doing, because that is what lets a *later*
  ticketless push resolve it; or
- pass `CRA_TICKET=docs/tickets/<slug>.md` when the ticket landed in an
  earlier push; or
- rely on branch-name matching for a ticket from an earlier push — the file
  stem must appear as a delimiter-bounded segment of the branch name
  (`docs/tickets/review-gate.md` resolves on `chore/review-gate`); or
- `CRA_REQUIRE_TICKET=0` to downgrade it to an in-report finding — for
  genuinely ticketless work only, never as a routine bypass.

Start from `docs/templates/ticket.md`. It deliberately lives **outside**
`docs/tickets/`, which the resolver globs: a template stored in the scanned
directory is resolved as a real ticket, and its placeholder acceptance criteria
then get graded against unrelated diffs. Acceptance criteria are graded
met / not-met / unclear against the diff, so write them as observable behaviour
rather than intentions.

Hook refusals (findings themselves stay advisory unless `CRA_BLOCKING=1`).
This table and the header of `.githooks/pre-push` carry the same six and must
be kept in step:

| exit | meaning |
|------|---------|
| 4  | a ticket was named but could not be read |
| 5  | no ticket resolvable — see above |
| 6  | scope gate: range far larger than a push candidate (>40 commits / >120 files); `CRA_FULL_REVIEW=1` overrides deliberately |
| 7  | the run reviewed **nothing** — a review that examined no code is not a review |
| 8  | a partial `CRA_HEAD` range on a push (a push sends HEAD) |
| 10 | an agent pushing in the foreground without `CRA_LONG_RUN_ACK=1` — the review outlives a 10-minute foreground shell |

Exit 3 is not in that list deliberately: it means the skill directory is
missing. That is an environmental abort, not a policy refusal — nothing about
the change is being judged.

**Specialist roster (this repo):** security, pattern-compliance, and
**api-contract**. The canonical `code-review-agent` repo ships a Supabase/RLS
specialist in the third slot; this repo has no SQL, so that slot is replaced
rather than left idle — an always-empty specialist silently weakens the
"≥2 specialists agree → high confidence" merge rule.

The skill is **vendored, not shared**: `.claude/skills/code-review-agent/` is a
copy of the canonical repo, so upstream fixes do not propagate automatically
and this repo's roster change does not leak upstream. The upstream commit it
was taken from, every intentional divergence, and the commands to check for
staleness are recorded in `.claude/skills/PROVENANCE.md` — read it before
re-copying from upstream, or the roster change will be silently reverted.

Reports land in `.claude/review-reports/` (gitignored — working artifacts).
