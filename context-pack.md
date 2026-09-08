# Context Pack

## 1. Diff
```diff
```

## 2. Ticket
_No ticket found — report the missing task-level context as a finding._

## 3. Engineering rules
# Mimiry API contract (generated — do not hand-edit)

Method, path, request-body fields and parameters distilled from the
OpenAPI specs. Regenerate with:

```
python3 scripts/gen_api_contract.py <path-to-mimiry-documentation> \
    -o .claude/review-rules/api-contract.md
```

This is the yardstick for the api-contract review specialist: every
`MimiryClient` method must match the method, path and field names below.

### auth

- `GET /api/v1/api-key`
- `POST /api/v1/auth/token` — optional: `expires_in` · params required: `X-SSH-Fingerprint(header)`, `X-SSH-Nonce(header)`, `X-SSH-Signature(header)`, `X-SSH-Timestamp(header)`
- `GET /api/v1/auth/token/limits`
- `GET /api/v1/me`

### compute

- `GET /api/compute/v1/availability` · params: `available_only(query)`, `detail(query)`, `form_factor(query)`, `gpu_family(query)`, `location(query)`, `max_age(query)`, `min_gpu_count(query)`, `min_vram_gb(query)`, `provider(query)`
- `GET /api/compute/v1/balance`
- `GET /api/compute/v1/balance/org/{id_or_name}` · params required: `id_or_name(path)`
- `GET /api/compute/v1/balance/user/{id_or_name}` · params required: `id_or_name(path)`
- `GET /api/compute/v1/catalog`
- `GET /api/compute/v1/quota`
- `GET /api/compute/v1/sessions` · params: `limit(query)`, `offset(query)`, `operation(query)`, `operation_not(query)`, `state(query)`, `state_not(query)`, `updated_after(query)`, `updated_before(query)`
- `POST /api/compute/v1/sessions` — required: `gpu`, `image`, `name` · optional: `auto_terminate`, `command`, `environment_vars`, `memory`, `org_id`, `result_storage`, `ssh_enabled`, `ssh_key_id`, `ssh_public_key`, `volume_mounts`
- `GET /api/compute/v1/sessions/{id}` · params required: `id(path)` · params: `events_tail(query)`
- `DELETE /api/compute/v1/sessions/{id}` · params required: `id(path)`
- `GET /api/compute/v1/sessions/{id}/logs` · params required: `id(path)` · params: `since(query)`, `tail(query)`, `timestamps(query)`
- `GET /api/compute/v1/transactions` · params: `limit(query)`, `offset(query)`
- `GET /api/compute/v1/volumes` · params: `limit(query)`, `offset(query)`, `operation(query)`, `operation_not(query)`, `org_id(query)`, `state(query)`, `state_not(query)`, `updated_after(query)`, `updated_before(query)`
- `POST /api/compute/v1/volumes` — required: `name`, `size_gb` · optional: `org_id`, `volume_type`
- `GET /api/compute/v1/volumes/{volume_id}` · params required: `volume_id(path)`
- `PUT /api/compute/v1/volumes/{volume_id}` — required: `new_size_gb` · params required: `volume_id(path)`
- `DELETE /api/compute/v1/volumes/{volume_id}` · params required: `volume_id(path)`

### organizations

- `GET /api/organizations/v1`
- `POST /api/organizations/v1` — required: `name`, `slug`
- `GET /api/organizations/v1/admin/orgs`
- `GET /api/organizations/v1/admin/orgs/{id}` · params required: `id(path)`
- `GET /api/organizations/v1/admin/orgs/{id}/members` · params required: `id(path)`
- `DELETE /api/organizations/v1/admin/orgs/{id}/members/{user_id}` · params required: `id(path)`, `user_id(path)`
- `GET /api/organizations/v1/by-slug/{slug}` · params required: `slug(path)`
- `POST /api/organizations/v1/invitations/{token}/accept` · params required: `token(path)`
- `POST /api/organizations/v1/invitations/{token}/decline` · params required: `token(path)`
- `GET /api/organizations/v1/operations/{request_id}` · params required: `request_id(path)`
- `GET /api/organizations/v1/operations/{request_id}/stream` · params required: `request_id(path)`
- `GET /api/organizations/v1/{id}` · params required: `id(path)`
- `PATCH /api/organizations/v1/{id}` — optional: `allow_member_invites`, `name`, `require_2fa` · params required: `id(path)`
- `DELETE /api/organizations/v1/{id}` · params required: `id(path)`
- `GET /api/organizations/v1/{id}/invitations` · params required: `id(path)`
- `GET /api/organizations/v1/{id}/members` · params required: `id(path)`
- `POST /api/organizations/v1/{id}/members/invite` — required: `email`, `role` · params required: `id(path)`
- `DELETE /api/organizations/v1/{id}/members/{user_id}` · params required: `id(path)`, `user_id(path)`
- `PATCH /api/organizations/v1/{id}/members/{user_id}/role` — required: `role` · params required: `id(path)`, `user_id(path)`

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

## 4. Changed files (current state)

## 5. Dependents

## 6. Related tests and history
Recent commits on touched files:

