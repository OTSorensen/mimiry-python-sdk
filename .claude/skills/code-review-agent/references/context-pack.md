# The context pack

The deterministic floor of every review, built by `scripts/build-context-pack.sh`
(byte-precise truncation lives in `scripts/pack_sections.py`; needs python3).
Hard cap: 131,072 bytes (`CRA_PACK_CAP` overrides). Six sections. Each starts
from an initial budget; measured demand below a budget releases the surplus
into a pool that flows, in priority order, to (1) file excerpts up to their
floor, (2) the rules section to completion, (3) the diff, (4) the ticket,
(5) file excerpts toward their ceiling, (6) dependents, (7) tests/history.
`--paths "<pathspec …>"` scopes the whole pack (diff, files, dependents,
tests) to the given git pathspecs — the normal mode for batched review of a
large branch.

1. `## 1. Diff` — the change under review (initial 12,288 B, grows when the
   pool allows)
2. `## 2. Ticket` — task-level context, or an explicit missing-ticket note
   (initial 4,096 B, grows when the pool allows; granted after the rules and
   the diff so it can never crowd out the code under review. A ticket that
   still cannot be shown **whole** refuses the pack — a ticket is a
   checklist, so unseen criteria are unassessed, not partially covered)
3. `## 3. Engineering rules` — `.claude/review-rules/*.md` **always complete,
   never clipped**; CLAUDE.md fills the remainder, cut only at markdown
   heading boundaries with every omitted heading named in the marker
4. `## 4. Changed files (current state)` — post-change contents as
   **hunk-centered excerpts**: windows of context around each file's changed
   lines, `[lines A-B omitted]` between windows. Per file: floor 1,536 B;
   the 4,096 B ceiling caps this section's claim on the shared pool, but
   leftover pool afterwards deepens files past it (the ceiling is a fairness
   device under contention, not a wall). Waterfill allocation: small files
   keep only what they need, the surplus deepens large ones. Never an even
   split, never a silent drop — a diffed file missing on disk gets a loud
   stub.
5. `## 5. Dependents` — files referencing the changed files' name stems (5,120 B)
6. `## 6. Related tests and history` — matching test files + recent commits (3,072 B)

The builder **exits 65** instead of emitting a crippled pack on any of three
triggers: the floor for every changed file cannot be met under the cap, the
diff's allocation falls below `CRA_DIFF_FLOOR_PCT` % of its demand (default
50 — a pack missing most of its diff reviews a fraction of the change while
reporting success), or the ticket cannot be shown whole (no percentage floor:
2026-08-17/18, criteria 14–23 of a 9,557 B ticket were invisible on two
consecutive runs while ~84 KB of cap sat unused, and the in-band marker got
recorded and forgotten). The stderr split plan gives each dominant file (own
diff > 12,288 B) a solo `--paths` suggestion and groups the rest at the
shallowest directory depth whose whole group still fits one pack — descending
below the top level when a directory's group is too big — with `:(exclude)`
pathspecs for the solo files, so batching terminates in real reviews.
**Split-plan invariant (2026-08-21):** every suggestion is strictly narrower
than the scope that produced it (a scoped build never suggests its own scope
or anything wider), every directory group carries the scope's `:(exclude)`
terms, groups are pairwise disjoint (a group is emitted only when every file
under it is assigned to it; files sitting directly in the scope root, and
files of a leaky fallback group, go to file bundles instead), a refusing
multi-file bundle halves, and no spec appears twice. A single file that will
not pack alone yields no suggestion from the builder — there is nothing
narrower. The harness then retries that file once with the ticket clipped
(`CRA_CLIP_SINGLE_FILE_TICKET`, default on, the coverage gap recorded); only
with that opt-out, or when the diff itself does not fit, is the file recorded
INCOMPLETE, loudly. Before this, `docs :(exclude)docs/X.md` was answered with `docs`, and
the cycle reviewed the same sub-groups twice (roadbuddy 20260820-184756). Escape hatches: `CRA_ALLOW_STARVED=1`
forces a starved pack (loud starvation warning), `CRA_ALLOW_CLIPPED_DIFF=1`
forces a diff-clipped one, `CRA_ALLOW_CLIPPED_TICKET=1` a ticket-clipped one
(each with its in-band truncation marker). They are orthogonal — a pack
degraded in several ways needs each corresponding hatch.

## Truncation markers

Every cut is announced in-band; the absence of these strings means the
section is complete:

- `_[<label> truncated: showing X of Y bytes]_` — diff / ticket / dependents /
  tests+history (the diff variant appends `— split the review with --paths`)
- `_[CLAUDE.md truncated: showing X of Y bytes. Omitted sections: "…"]_` —
  a heading suffixed `(cut mid-section)` was rendered in part, the rest whole
- `_showing K of M lines (S of H changed hunks; X of Y bytes)_` — status
  line under a `### file` header whose excerpt is windowed; S < H means
  real changes are hidden in the gaps
- `_showing K of M lines (whole-file excerpt; X of Y bytes)_` — same, for a
  new file (the whole file is the change)
- `[lines A-B omitted]` — between windows, unchanged context only
- `[lines A-B omitted — contains N changed hunks]` — that gap hides real
  changes; treat the file as under-covered
- `_[file not present on disk — skipped]_` — diffed path with no working-tree
  file
- `_[WARNING: file excerpts below floor — pack is starved; split with --paths]_`
- `_[pack truncated at cap]_` — final hard-cap safety net (should not appear
  in normal operation; the allocator keeps the total under the cap)

Contract for specialists: **the pack is your entire visible world.** Do not
assume code you cannot see; if evidence is truncated, say so rather than guess.
The orchestrator may deepen retrieval agentically before dispatch, but whatever
it learns must be appended to the pack text it hands you — you never read the
repo yourself.
