# Fail before the money is spent: Python mismatch and volume location

## Context

Two findings from the SDK bug reports (`SDK_BUG_REPORTS_2026-09-08.md`), both
the same shape: the SDK knows enough to refuse a doomed request, and instead
lets the platform discover the problem after a session exists.

The expensive one. A `.remote()` call from Python 3.14 against an image
shipping Python 3.11 provisioned a session, ran the bootstrap, and segfaulted
on arrival — the function travels as a cloudpickle blob, which does not load
across minor versions. The session was gone within two seconds. The SDK then
spent the full five-minute SSH budget connecting to a machine that no longer
existed and reported `SSHError: sshd not reachable`. Seven and a half minutes,
€0.56, and a diagnosis that sends the user to debug their network. The SSH
wait had no view of the session state, and the two places that did check for a
terminal state each carried their own hand-written copy of the state names —
copies that omit `exited` and `pull_failed`, which are exactly what the API
emits here.

The cheap one. `session create --volume` never reads the volume it is about to
mount. A volume created without `--location` landed in FIN-01; a session asked
for FIN-02 and was killed six seconds later by the platform. No charge, clear
error — but the SDK already runs a GPU preflight, and the volume's location is
one call away.

## Goal

A request the SDK can already tell will fail should fail locally, before a
session is created, naming both sides of the conflict. Where it cannot be
known locally, the failure should be diagnosed as what it is — a container
that died — rather than as a transport error, and without waiting out a
timeout for information that was available immediately.

## Acceptance criteria

1. A `.remote()` call whose image declares a Python version different from the
   caller's raises before `create_session` is called, and the error names both
   versions.
2. An image that declares no Python version is not refused locally.
3. The container receives the caller's Python version and refuses the payload,
   with a message naming both versions, before attempting to unpickle it.
4. A bootstrap failure message reaches the container's log as well as the
   error file, so it survives the container exiting.
5. `wait_for_sshd` accepts a terminal-state check and, when the session has
   ended, raises immediately instead of waiting out `max_wait_seconds`.
6. When the SSH wait fails on a session that has ended, the raised error
   carries the container's own log tail, not an SSH transport message.
7. `function.py` and `run.py` contain no literal list of terminal state names;
   both derive them from `_session.TERMINAL_STATES`.
8. The terminal-state check reports `exited` and `pull_failed`.
9. A failed state poll during a wait returns no state and does not end the
   call.
10. `session create --volume` with no `--location` creates the session in the
    volume's location.
11. `session create --volume` with a conflicting `--location` exits non-zero
    without creating a session, naming both locations.
12. `session create` with volumes from two different locations exits non-zero
    without creating a session.
13. A failure to read the volume list leaves the request unchanged and the
    session is still created.
14. `volume create --help` and `session create --help` describe location as
    binding on attachment rather than as a hint.
15. When a session ends before a result and the platform's payload carries an
    `error`, the raised message leads with that error and does not claim the
    command failed during startup.

## Non-goals

- Probing an undeclared image's Python version ahead of time. That needs a
  registry round-trip or a throwaway session; the container-side check covers
  the case at the cost of one session.
- Changing how `.map()` schedules work, or the decorator's default GPU and
  image (filed separately).
- Any platform-side change. Both fixes are local refusals of requests the
  platform would reject anyway.

## Verification

Unit suite green (203 tests). Each new behaviour was verified by mutation:
the fix was neutralised in the source one at a time and the suite re-run —
every one produced a failure (volume preflight 3, SSH terminal check 1, local
Python preflight 2, container Python check 1, shared terminal states 1) and
passed again on restore.

Not yet verified against the live API. The end-to-end proof is a `.remote()`
call from a Python that does not match the image, which must now fail in under
a second at no cost, and a `session create --volume` against a volume in
another location, which must be refused before a session ID is returned.
