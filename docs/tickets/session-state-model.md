# Align the session state model with what the API emits

## Problem

The SDK gated readiness on `state == "started"`. The live API never emits
`started`; a usable session reports `running`. Consequences:

- `mimiry session ssh` refused every healthy session, advising the user to
  wait for a state that never arrives.
- `--wait` polled past readiness until the session died, then exited 1 —
  every successful batch run looked like a failure to a calling script.
- `sessions --active` listed `exited` and `pull_failed` sessions as active.
- `--gpu` defaulted to `T4`, which no provider offers.

Root cause is spec drift: the OpenAPI `SessionState` enum lists five values
never observed live and omits seven that are. The SDK implemented the spec
faithfully; the spec does not describe the platform.

## Acceptance criteria

- [ ] Readiness is decided by the state the API actually emits, with no
      second accepted spelling for the same condition.
- [ ] `session ssh` connects to a session whose state is `running`.
- [ ] `session ssh` connects when a host is published under a state name the
      SDK does not recognise, rather than refusing a reachable paid instance.
- [ ] `session ssh` refuses only a terminal session or one with no endpoint,
      and never names a state the API cannot report.
- [ ] `--wait` returns when the session becomes usable, not when it ends.
- [ ] `--wait` exits 0 when the work completed, 1 only on genuine failure,
      naming the API's own error text.
- [ ] The `sessions --active` filter derives from one state definition, so a
      dead session cannot appear active.
- [ ] `--gpu` has no default; help text and examples name a real GPU,
      provider and location.
- [ ] A test fails if `== "started"` is reintroduced as a readiness check.
- [ ] Verified live: SSH into a running session and `--wait` exiting 0.

## Out of scope

The platform-side spec drift (filed separately). This ticket makes the SDK
match observed reality; the spec is fixed on the platform's own timeline.
