---
name: code-review-agent
description: Independent multi-specialist AI code review of a diff (staged or branch) against its ticket and the repo's engineering rules. Use before pushing, when asked to review changes, commits, or a PR, or when invoked by the pre-push/CI harness.
---

# Code Review Agent

Ensemble review: a deterministic context pack feeds three specialist reviewers
(security, pattern-compliance, api-contract); their findings are merged,
deduplicated, and risk-tiered. Findings are advice — the human retains final
judgment; never auto-apply fixes.

## Workflow

1. **Determine the diff.** Default: current branch vs `main`. If the user says
   staged/uncommitted, use staged mode. Record the changed-line count from
   `git diff --stat` (branch mode: `git diff $(git merge-base main HEAD)..HEAD --stat`).
2. **Locate the ticket.** Resolution order: (a) a ticket given explicitly —
   in the prompt, via `--ticket`, or via `CRA_TICKET`; (b) tickets added or
   modified in the diff range — the ticket that shipped with the work; review
   against all of them and name each in the report header; (c) a
   `docs/tickets/*.md` file whose stem appears as a delimiter-bounded segment
   of the branch name, the fallback when the push carries no ticket of its own. If none resolves,
   STOP and ask the user to create one from the ticket template (or point you
   at one): the ticket is what the review verifies intent against. Any ticket
   you write or amend, immediately post its file path in the chat — the owner
   must see that it exists and be able to review the file before it is used.
   Proceed
   ticketless only when the user explicitly says to (or the harness opted out
   with `CRA_REQUIRE_TICKET=0`) — and then add this mandatory finding to the
   merge set:
   `{"file": "", "line": 0, "category": "other", "severity": "medium",
   "confidence": "high", "specialists": [], "title": "No task-level context
   (ticket missing)", "evidence": "no docs/tickets entry matches this branch",
   "suggested_fix": "create one from the ticket template before merging"}`.
3. **Build the context pack:**
   `bash .claude/skills/code-review-agent/scripts/build-context-pack.sh
   --repo . --base main [--diff-mode staged] [--ticket <path>]
   --out <scratch>/context-pack.md`
   **Exit 65 means the change is too large for one honest pack** — file
   excerpts would fall below floor quality, the diff would be clipped
   below the coverage floor (`CRA_DIFF_FLOOR_PCT`, default 50%), or the
   ticket cannot be shown whole (unseen acceptance criteria would go
   unassessed). Stderr carries ready-made `--paths` suggestions: a solo pack
   per dominant file, the rest grouped at the shallowest directory depth
   whose whole group fits one pack, with `:(exclude)` pathspecs.
   **Do not batch by hand. Run the harness over the range and read the
   reports it names:**
   `[CRA_HEAD=<rev>] [CRA_TICKET=<path>] bash .claude/skills/code-review-agent/scripts/pre-push-review.sh`
   Leave the base to the script (it resolves the push boundary and the
   last reviewed commit itself); `CRA_HEAD` only ends the range early. A
   hand-set `CRA_BASE` writes a marker the next push cannot trust and
   re-reviews the range — it is an escape hatch for a base the script
   cannot resolve, not a way to pick a range.
   It is the one engine for batched reviews: it builds one pack per
   suggested batch, reviews up to `CRA_PACK_JOBS` (default 3, max 10) at a
   time in a rolling pool, never reviews a spec twice, records every finished
   pack in a ledger so an interrupted run resumes where it stopped, and writes
   one report per pack to `.claude/review-reports/<stamp>-<slug>.md`. Hand-run
   batches have none of that (no resume, no concurrency, no record of what
   finished). Do not reach for `CRA_ALLOW_STARVED=1`,
   `CRA_ALLOW_CLIPPED_DIFF=1`, or `CRA_ALLOW_CLIPPED_TICKET=1` unless a
   single degraded pack is genuinely better than batching (it almost never
   is).
   **When you review a single pack yourself, seed its `## 7. Orchestrator
   additions` with the known cross-batch seams** — guards, config entries,
   types, or fixes that live in a *different* batch — before dispatching
   specialists. A specialist scoped to one batch will otherwise correctly
   report an absence that another batch supplies (pilot 2026-08-01: happened
   twice; the `config.toml` and `queryKeys` seam notes prevented several
   false findings).
   You may deepen retrieval afterwards (read one more file, follow one more
   import) — append anything you learn to the pack text under a final section
   `## 7. Orchestrator additions`. Specialists see only the pack.
4. **Choose execution mode (hybrid rule).**
   - Changed lines ≥ 30 AND the Agent/Task tool is available → dispatch three
     subagents IN PARALLEL (one message, three tool calls), one per prompt file
     in `references/` (security-agent.md, pattern-agent.md,
     api-contract-agent.md). Each subagent prompt = that file's full text + the
     full pack text + "Return ONLY the fenced json block."
   - Otherwise → run the same three passes yourself, inline and sequentially,
     reading one prompt file at a time and producing the same json for each
     before moving to the next.
   - Either way, record which mode actually ran: the report header states it
     (see `references/report-format.md`). Degrading to inline is legitimate;
     degrading *silently* is not — the ≥2-specialist agreement signal of step 5
     only means what it claims when the passes were independent.
5. **Merge findings:**
   - First, tag every collected finding with the pass that produced it:
     set `specialists` to `["security"]`, `["pattern"]`, or
     `["api-contract"]` per the originating specialist — never inferred
     from the finding's `category`, which the pattern specialist
     legitimately varies ("testing", "other").
   - Dedupe on the **defect**, not the location: merge findings only when
     they describe the same root cause. Same file + overlapping line window
     (±5 lines) is the *candidate* signal that starts the comparison — two
     different defects at the same statement stay separate (pilot 2026-08-01:
     "cap misses cascades" vs "cap counts unsigned rows" share a line and are
     opposite bugs). Category differences never block a merge — specialists
     label the same bug differently. Keep the most severe copy's content and
     category; union the `specialists` lists.
   - Same defect found by ≥ 2 specialists → confidence "high".
   - Unique high/medium findings from any specialist → keep.
   - Low findings: count **instances, not roles** — a low reported by only a
     single specialist pass across all packs is dropped (precision guard);
     the same low found independently in ≥ 2 passes or packs is kept.
6. **Write the report** exactly per `references/report-format.md`, ending with
   the machine-readable json block (findings + acceptance_criteria +
   candidate_patterns). Evaluate each ticket acceptance criterion as
   met / not-met / unclear from the pack evidence.
7. **Invite challenge.** Close with the Q&A line. When the user challenges a
   finding, answer from pack evidence; downgrade or withdraw findings you
   cannot defend, and say so plainly.

## Candidate patterns

List generalizable lessons behind the findings ("candidate patterns"). If the
user validates one, append it to `.claude/review-rules/<topic>.md` in the
target repo as: rule statement, one-line example, one-line rationale. The
context pack builder injects these files into every future review.

## Headless mode

When invoked via `claude -p` (pre-push or CI), a pack may already exist —
if the prompt names a pack path, skip steps 1–3 and review that pack. The
pre-push harness allowlists the subagent tool (Task/Agent), so the parallel
fan-out of step 4 is expected headless too; the inline fallback covers
environments without it, and the header's `Execution mode:` line records
which one ran either way.

**Pre-push reviews are incremental.** The harness records the commit whose
review completed cleanly in `cra-reviewed` **inside the git directory**
(`$(git rev-parse --absolute-git-dir)/cra-reviewed`, so per-clone and outside
the worktree) and starts the next run there instead of at the merge-base, so a
long-lived branch stops re-reviewing every earlier commit on every push. The
location is deliberate and must not be moved into the worktree: `.gitignore`
prevents staging a path, not checking one out, so a crafted branch could commit
a tracked file or symlink where the marker is read from and make the gate skip
the very commits it names as reviewed. The
marker records what a review covered, not its verdict: it advances past any
review that covered its whole range, tagging whether the range came back clean
or carried advisory highs. A dry run, an unscoreable or clipped pack, or an
unreviewed path still leaves it where it is — those are coverage gaps, not
verdicts. A high finding freezes the marker only in blocking mode
(`CRA_BLOCKING=1`); in the default advisory mode it advances (tagged
`advisory-highs`) so an accepted finding cannot restore full-history
re-review, and a later blocking run distrusts that tag and re-reviews the range
in full so the high is re-examined. When the prompt says the review is
incremental, judge only what the diff changes: the branch is wider than the
pack, so absent earlier work is not a finding and its acceptance criteria are
`unclear`, not unmet. `CRA_FULL_REVIEW=1` forces the whole merge-base range —
use it for a pre-merge review of a complete branch.

**A review can cover a commit range.** `CRA_HEAD=<rev>` ends the reviewed
range at that commit (`CRA_BASE..CRA_HEAD`) so a chosen set of commits can be
reviewed while later work on other subjects sits in the checkout, and a long
backlog can be reviewed in sequences. The commit must be an ancestor of HEAD
(else exit 8). Packs are built from a temporary detached worktree at that
commit, so excerpts, rules and the ticket text match the reviewed diff, not
the working tree; the marker lands on it, so the next run continues from
there. A pre-push invocation refuses `CRA_HEAD` below HEAD — a push sends
HEAD. When the user says "review up to commit Y" (or "the next N commits"),
run `CRA_HEAD=Y bash .claude/skills/code-review-agent/scripts/pre-push-review.sh`
with the base left to the script: it starts at the last reviewed commit (or
the push boundary), and the push that follows reviews only what comes after
Y. Do not set `CRA_BASE` to pick the start — the marker records the base it
was measured against, and a push whose base differs distrusts the marker and
re-reviews the whole range. `CRA_BASE` is for a base the script cannot
resolve (no remote, no `main`), not for routine range reviews.

**Batched reviews resume.** Every finished pack (reviewer completed, report
extracted, not UNDETERMINED) is recorded in the ledger
`$(git rev-parse --absolute-git-dir)/cra-packs.tsv` — beside the marker,
outside the worktree, for the same reason — keyed by the sha256 of that
spec's diff over the reviewed range plus the review rules. The next run
replans fresh and reuses every recorded pack whose key still matches
(`pack n: <spec> — reused from run <stamp> (<report>)`), reviewing only the
rest; its summary lists what it reused. HEAD moving on with unrelated commits
keeps finished packs; a commit touching a group invalidates only that group.
`CRA_RESUME=<stamp>` restricts reuse to one run; `CRA_FRESH=1` disables it. A
report that git tracks is never trusted for reuse. Packs run `CRA_PACK_JOBS`
(default 3, clamped 1..10) at a time in a rolling pool; the log shows
`pack n/N: <spec>` when one starts and `pack n/N done (<high> high, <min>
min)` when it finishes. An interrupted run (Ctrl-C, closed session) prints
`run <stamp> interrupted: k/N packs finished; rerun to resume` and exits 130
— rerunning the same command continues it. A single file that will not pack
beside its ticket has nothing narrower to split into, so the harness retries
it once with the ticket clipped (in-band marker, coverage warning, marker
held) instead of recording it NOT REVIEWED; `CRA_CLIP_SINGLE_FILE_TICKET=0`
restores the refusal. When a batched run carries several range tickets, each
pack inlines only the tickets that mention its files (or whose own file it
changes) and lists the rest by name — a ticket that mentions none of a pack's
files is not that pack's yardstick, and inlining every ticket everywhere is
what starved the single-file pack above. `CRA_TICKET_RELEVANCE=0` inlines
everything everywhere; an explicit `CRA_TICKET` is never filtered.
