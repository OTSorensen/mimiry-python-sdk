# One session for `.map()`, a volume on the decorator, and the bill on the result

## Context

`Function.map()` was `[fn.remote(x) for x in items]`: one full session per
item, each paying provision + boot + image pull (five to eight minutes today
and a ten-minute billing block) for a call that ran in under a second. Every
item was a fresh chance to hit a capacity blip, and when one did, the
exception threw away the results of the items that had finished. Three map
runs in a row failed that way on the live platform with nothing returned.

Separately, attaching a volume from Python meant dropping to the CLI: the
decorator had no way to say which volume to mount, so the persistence feature
the platform already delivers was unreachable from the SDK's main surface.

## Goal

`.map()` creates one session and streams every item through it, returning
results in order, and never discards finished work. `@mimiry.function` can
attach a volume with the same location safety the CLI has.

## Acceptance criteria

1. `Function.map(items)` creates exactly one session for any number of items.
2. The worker-mode payload env var carries the function alone; each item's
   `(args, kwargs)` is pushed to `/tmp/mimiry_calls/<n>.b64` over SSH and its
   result read from `/tmp/mimiry_results/<n>.b64`.
3. The container's worker loop answers calls in index order, keeps running
   after an item raises, and exits on the done flag or after the hold timeout
   with no new call.
4. An item that raises inside the container is recorded and the remaining
   items still run; `MapError` is raised at the end with `results` (the
   full-length list, `None` at failed indices), `failures` as
   `[(index, exception)]`, and `total`.
5. When the session dies part-way, `MapError` carries every result that had
   arrived and a failure at the first unfinished index.
6. When the session dies before any result, the underlying `SessionError` is
   raised unchanged.
7. `.remote()` still uses the single-call protocol (`(fn, args, kwargs)` in
   the env var, one result file).
8. `@mimiry.function(volume="name")` mounts the volume at `/data`;
   `volume={"a": "/x", "b": "/y"}` mounts each at its path; both appear as
   `volume_mounts` in the session payload.
9. A session with a volume adopts the volume's location when none was given;
   a conflicting `location=` is refused before `create_session` is called.
10. The volume-location preflight lives in `_session.py` and raises
    `SessionError`; the CLI and the decorator share it.
11. `MapError` and `RemoteFunctionError` are importable from `mimiry`.
12. Each `_ssh_cmd` retry and the new `push_remote_file` write atomically
    (`.partial` then rename) so the container never reads a half-written call.
13. After `.remote()` or `.map()`, `Function.last_run` is a `RunInfo` with
    the session id, `gpu_type`, `hourly_rate`, `currency`, per-state
    seconds, wall-clock `duration`, and the platform's `final_cost` once it
    has settled (polled briefly after release; `None` if it has not).
14. `RunResult.info` from `mimiry.run()` and `MapError.run` carry the same
    `RunInfo`; a map that died part-way still reports its session's cost.
15. Reading the billing figures never raises; a missing field is `None`.

## Non-goals

- Parallel items. The account allows two concurrent sessions; using both is
  a later change. Items run one after another on the single session.
- Streaming results back to the caller as they arrive; `.map()` still
  returns once at the end.
- A warm pool or session reuse across separate `.map()` / `.remote()` calls.

## Verification

Unit: `tests/test_map_and_volume.py` drives the real `_run_map` /
`_run_remote` against a fake platform and an in-memory container that
answers the SSH protocol; each criterion above was neutralised in the source
and produced a failing test. The worker bootstrap was also executed as a real
subprocess with the paths redirected, serving three calls including a
raising one, and exited cleanly on the done flag.

Live: `embed.map([...])` with three items on the alpha platform must create
one session, return three results, and cost one billing block; a
`volume="..."` decorator call must mount `/data` and the next call on the
same volume must find what the first wrote.
