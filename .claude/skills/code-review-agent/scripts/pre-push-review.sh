#!/usr/bin/env bash
# Pre-push AI review gate. A resolvable ticket is required by default: the push
# is refused (exit 5) when none is found, because the review verifies the diff
# against the ticket's acceptance criteria. Findings themselves stay advisory:
# exit 0 unless CRA_BLOCKING=1 and the review was not both complete and clean —
# a high finding, a pack that could not be scored, or a path that was never
# reviewed (then exit 2).
# ONE THING IS NEVER ADVISORY: a run that reviewed no pack at all refuses the
# push with exit 7, in every mode. "Advisory" means findings do not delay a
# push, not that an EMPTY run may accompany one. The same gate fires early —
# after the FIRST wave of packs — so a broken run stops there instead of
# spending the whole branch's tokens to arrive at nothing (2026-08-20: every
# pack refused to build, this script exited 0, and 7 unreviewed commits reached
# the remote).
# Env: CRA_BASE (default: origin/<branch> if it exists, else origin/main, else
#      main — the push-candidate boundary; see below), CRA_BLOCKING (default 0),
#      CRA_DRY_RUN (default 0),
#      CRA_TICKET (explicit ticket path, highest precedence; exit 4 if unreadable),
#      CRA_REQUIRE_TICKET (default 1; 0 downgrades a missing ticket to a finding),
#      CRA_FULL_REVIEW (default 0; 1 ignores the incremental marker below AND
#      the scope gate), CRA_MAX_COMMITS / CRA_MAX_FILES (scope gate, defaults
#      40 / 120: a range exceeding EITHER cap refuses with exit 6 instead of
#      silently starting a review far bigger than intended).
#      CRA_HEAD (default HEAD): the commit the reviewed range ends at, so a
#      chosen range of commits can be reviewed while later work sits in the
#      checkout (CRA_BASE..CRA_HEAD). Must be an ancestor of HEAD (else exit
#      8); packs are then built from a temporary worktree at that commit, and
#      the marker lands on it so the next run continues from there. Refused
#      (exit 8) under a pre-push invocation when below HEAD — a push sends
#      HEAD. A push is recognised by CRA_VIA_HOOK=1 (the README's hook line
#      exports it) or by git's two pre-push arguments forwarded with "$@".
#      CRA_PACK_JOBS (default 3, clamped to 1..10 with a note): how many
#      path-scoped packs a batched review runs at once, as a ROLLING POOL — a
#      slot is refilled the moment it frees. Packs are independent, so this is
#      throughput only. 1 runs them one at a time — through the SAME code path,
#      not a separate one (a direct-call branch once diverged into a fail-open,
#      2026-08-20). Each pack's output is captured and replayed whole when that
#      pack finishes; the log shows `pack n/N: <spec>` at start and
#      `pack n/N done (...)` at finish.
#      CRA_RESUME / CRA_FRESH: every FINISHED pack is recorded in the ledger
#      <git-dir>/cra-packs.tsv, keyed by the sha256 of its spec's diff plus the
#      review rules; a later run reuses a recorded pack instead of reviewing it
#      again (automatic). CRA_RESUME=<stamp> restricts reuse to that run;
#      CRA_FRESH=1 disables it. An interrupted run (INT/TERM) says how far it
#      got and exits 130; rerunning resumes.
#      CRA_MAX_SPLIT_DEPTH (default 3) / CRA_MAX_SPLIT_PACKS (default 12): a
#      group that will not pack is SPLIT AGAIN rather than dropped, up to that
#      depth. A split that would fan out past CRA_MAX_SPLIT_PACKS refuses
#      loudly instead — a group that wide is a wrong base or a genuinely
#      broad change, and starting it silently is the same failure as
#      skipping it silently; raise the cap deliberately for the latter.
#      CRA_BUNDLE_FILES (default 4, read by build-context-pack.sh) bounds a
#      file bundle, the fallback when a scope holds no narrower directory.
#      CRA_TICKET_RELEVANCE (default 1): in a batched run with several range
#      tickets, each pack inlines only the tickets that mention its files (path,
#      file name, or a containing directory of 2+ segments) or whose own file
#      it changes; the rest are listed by name. 0 inlines every ticket into
#      every pack, as before. An explicit CRA_TICKET is never filtered.
#      CRA_CLIP_SINGLE_FILE_TICKET (default 1): a single file that will not
#      pack beside its ticket has nothing narrower to split into, so it is
#      retried once with the ticket clipped (in-band marker, coverage warning,
#      marker held) rather than recorded NOT REVIEWED. 0 restores the refusal.
#      CRA_DIFF_FLOOR_PCT / CRA_ALLOW_CLIPPED_DIFF / CRA_ALLOW_CLIPPED_TICKET /
#      CRA_ALLOW_STARVED / CRA_PACK_CAP reach the pack builder through the
#      environment (its header documents them); no plumbing here.
#
# Reviews are INCREMENTAL. The default range is merge-base(BASE,HEAD)..HEAD, which
# on a long-lived branch re-reviews every earlier commit on every push: measured
# 2026-08-17 on a 12-slice design branch, one three-commit push rebuilt eight packs
# over the whole branch and took ~30 min, and that cost grows with every slice.
# A marker records the commit up to which a review actually covered the range,
# and the next run starts there instead. No commit is ever skipped — the marker
# only moves past work a review saw, never past work it did not.
#
# The review runs with READ-ONLY tools. It must never be given Bash: the pack it
# reads embeds the branch's diff and file contents, which in a repo with an
# upstream codegen sync (or any untrusted contributor) is not exclusively
# author-written text. This script does the pack building and finding extraction
# itself, so the model's loop needs nothing but Read/Glob/Grep — plus the
# subagent tool, so the skill's designed parallel 3-specialist fan-out can run
# (Task is the live name; Agent is listed too so a CLI rename cannot silently
# disable it — unknown names are inert, same convention as eval/run_case.sh).
# Allowing subagents does NOT weaken the read-only invariant: subagents inherit
# this session's deny list, so a Task-spawned specialist still cannot
# Bash/Write/Edit/WebFetch/WebSearch. That inheritance is CLI behavior, not
# grep-able source — tests/test_live_deny_inheritance.sh probes it live; run
# it after CLI upgrades. Denying Task here (2654e3e did, 2026-08-04
# .. 2026-08-17) silently forced every pre-push review into the inline fallback
# and destroyed the ≥2-specialist independence signal — never re-add it.
#
# BOTH lists are required. `--allowedTools` is ADDITIVE to whatever the user's
# settings files already permit — omitting Bash there does not revoke a
# `Bash(...)` allow rule in settings.local.json. Deny wins over allow, so the
# invariant is enforced by --disallowedTools, never by omission.
REVIEW_TOOLS=("Read" "Glob" "Grep" "Task" "Agent")
REVIEW_DENY=("Bash" "Write" "Edit" "NotebookEdit" "WebFetch" "WebSearch")

set -euo pipefail
# The default base is the push-candidate boundary: what this branch has that its
# remote does not. origin/<branch> when it exists; origin/main for a branch that
# has never been pushed (its whole history is genuinely new); bare main when
# there is no origin at all. This logic lived in the consumer's pre-push hook
# first (roadbuddy .githooks/pre-push) while the script's own default stayed
# `main` — two defaults in two files, and on 2026-08-19 a hand-set CRA_BASE on
# one side wrote an incremental marker the other side could not trust, which
# re-reviewed a fully-reviewed 36-commit branch for over an hour. ONE default,
# owned here; CRA_BASE remains an override for exotic cases, not routine use.
if [ -n "${CRA_BASE:-}" ]; then
  BASE="$CRA_BASE"
else
  _branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [ -n "$_branch" ] && [ "$_branch" != "HEAD" ] \
     && git rev-parse --verify --quiet "origin/$_branch" >/dev/null 2>&1; then
    BASE="origin/$_branch"
  elif git rev-parse --verify --quiet "origin/main" >/dev/null 2>&1; then
    BASE="origin/main"
  else
    BASE="main"
  fi
  unset _branch
fi
DRY="${CRA_DRY_RUN:-0}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
echo "review base: $BASE ($(git rev-parse --short "$BASE" 2>/dev/null || echo 'unresolvable'))"
SKILL_DIR=".claude/skills/code-review-agent"
if [ ! -d "$SKILL_DIR" ]; then
  echo "code-review-agent skill not installed (expected $SKILL_DIR). See the code-review-agent README." >&2
  exit 3
fi

mkdir -p .claude/review-reports
STAMP="$(date +%Y%m%d-%H%M%S)"
SCRATCH="$(mktemp -d)"
# The run is DISCOVERABLE: cra-watch.py (the out-of-process observer) attaches
# through this pointer — stamp, scratch dir, this shell's pid and its kernel
# start time (so a reused pid after a hard kill is not mistaken for a live
# run). Written once, never read by this script. Every write on the observer's
# behalf is best-effort (`|| true`): the plumbing that lets a review be watched
# must never be able to end one.
{ printf '%s\t%s\t%s\t%s\t%s\n' "$STAMP" "$SCRATCH" "$$" \
    "$(sed 's/^.*) //' /proc/$$/stat 2>/dev/null | awk '{print $20}' || echo 0)" "$REPO_ROOT" \
    > "$(git rev-parse --absolute-git-dir)/cra-run-current"; } 2>/dev/null || true
# Deliberately NO `trap ... EXIT` for the cleanup. Bash runs inherited EXIT traps
# when a command- or process-substitution subshell finishes, so a trap here
# deletes the scratch directory in the middle of the run — measured 2026-08-04:
# `mapfile -t X < <(grep ... "$SCRATCH/err")` fired it before grep opened the
# file. ($BASHPID guards do not help; the trap ran with BASHPID == the main pid.)
# Cleanup is explicit instead; a missed temp dir beats a mid-run deletion.
cleanup() {
  if [ -d "$SCRATCH/wt" ]; then
    git worktree remove --force "$SCRATCH/wt" >/dev/null 2>&1 || true
  fi
  rm -rf "$SCRATCH"
  git worktree prune >/dev/null 2>&1 || true
}

# ---- The reviewed range ends at REVIEW_HEAD: HEAD, or CRA_HEAD ---------------
# Oliver, 2026-08-21: five commits wait for review in a repo where work on
# other subjects must continue meanwhile, and a 40-commit backlog is best
# reviewed in sequences. CRA_HEAD=<rev> ends the range there; the marker then
# lands on it, so the next run continues from it. Every range computation
# below — merge-base, range tickets, scope gate, marker, ledger key — uses
# REVIEW_HEAD, never bare HEAD.
# Because the pack builder reads the WORKING TREE for file excerpts, rules,
# dependents and tests, a range that ends below HEAD is built from a temporary
# detached worktree at REVIEW_HEAD (BUILD_REPO), or the specialists would see
# files that do not match the diff. It is removed in cleanup; a leftover from
# a hard kill is pruned at the next run's start.
# A pre-push invocation (git passes <remote> <url>) may not review less than
# HEAD: a push sends HEAD, and a partial review must not accompany it.
ACTUAL_HEAD="$(git rev-parse HEAD)"
git worktree prune >/dev/null 2>&1 || true
if [ -n "${CRA_HEAD:-}" ]; then
  REVIEW_HEAD="$(git rev-parse --verify --quiet "${CRA_HEAD}^{commit}" 2>/dev/null || true)"
  if [ -z "$REVIEW_HEAD" ]; then
    echo "CRA_HEAD='$CRA_HEAD' does not resolve to a commit in this clone." >&2
    cleanup; exit 8
  fi
  if ! git merge-base --is-ancestor "$REVIEW_HEAD" HEAD 2>/dev/null; then
    echo "CRA_HEAD='$CRA_HEAD' ($(git rev-parse --short "$REVIEW_HEAD")) is not an ancestor of HEAD — a range must end on this line of history." >&2
    cleanup; exit 8
  fi
else
  REVIEW_HEAD="$ACTUAL_HEAD"
fi
BUILD_REPO="$REPO_ROOT"
RH_LABEL="HEAD"
if [ "$REVIEW_HEAD" != "$ACTUAL_HEAD" ]; then
  RH_LABEL="$(git rev-parse --short "$REVIEW_HEAD")"
  # A push is recognised two ways: git's two pre-push arguments, forwarded by
  # a hook that passes "$@", or CRA_VIA_HOOK=1, which the README's hook line
  # exports. Argument count alone left the guard inert under any hook that
  # did not forward them — every copy older than 2026-08-21 — and an
  # exported CRA_HEAD then reviewed less than the push sent, marker advanced,
  # exit 0 (roadbuddy 20260822-140937). The variable deciding how much of a
  # push is examined must not depend on the hook's spelling.
  if [ "$#" -ge 2 ] || [ "${CRA_VIA_HOOK:-0}" = 1 ]; then
    echo "CRA_HEAD=$RH_LABEL is below HEAD, but this is a pre-push invocation and a push sends HEAD — a partial review must not accompany it. Review the range manually (CRA_HEAD is for manual runs), or push after the whole range is reviewed." >&2
    cleanup; exit 8
  fi
  if ! git worktree add --detach --quiet "$SCRATCH/wt" "$REVIEW_HEAD" >/dev/null 2>&1; then
    echo "could not check out $RH_LABEL into a temporary worktree for the pack builder." >&2
    cleanup; exit 8
  fi
  BUILD_REPO="$SCRATCH/wt"
  echo "review head: $RH_LABEL (CRA_HEAD) — $(git rev-list --count "$REVIEW_HEAD..HEAD") commit(s) after it are NOT reviewed by this run"
fi

# The branch point, computed once: it bounds the ticket search below and anchors
# the marker's ancestry check further down. Empty when BASE is unknown to this
# clone; its consumers guard for that. (Distinct from EFFECTIVE_BASE, which an
# incremental run narrows to the last reviewed commit.)
MERGE_BASE="$(git merge-base "$BASE" "$REVIEW_HEAD" 2>/dev/null || true)"

# Ticket resolution. Precedence: CRA_TICKET (explicit) > tickets committed in
# the push range > a ticket stem bounded in the branch name. Range before name
# is deliberate: a ticket committed WITH the work is direct evidence of what
# this push is for, while a filename resembling the branch is a heuristic that,
# on a long-lived branch, never stops matching. Name-first made a per-push
# ticket unusable without CRA_TICKET (roadbuddy-app 2026-08-19: a carry-over
# push on branch `design-overhaul` carried its own ticket, but
# docs/tickets/design-overhaul.md won by name and the review was judged against
# 23 unrelated criteria, nearly all "unclear" — noise that reads as coverage).
# The name heuristic remains the fallback for the case it was built for: a push
# that carries no ticket of its own. Explicit ifs throughout: errexit is armed,
# and a failing `[` at the tail of an `&&` chain would kill the script.
TICKET_ARG=""; TICKET_SHOW=""
TICKET_VIA=""
BRANCH_TICKET=""
RANGE_TICKET_LIST=""
# Harness state spliced into the reviewer prompt by review_pack — initialised
# here so a value inherited from the operator's shell can never reach the
# prompt (review 20260822-130734). Set per pack in run_one_pack.
PACK_TICKET_NOTE=""
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ -n "${CRA_TICKET:-}" ]; then
  if [ ! -r "$CRA_TICKET" ]; then
    echo "CRA_TICKET is set but not readable from $REPO_ROOT: $CRA_TICKET" >&2
    cleanup
    exit 4
  fi
  TICKET_ARG="$CRA_TICKET"
  TICKET_VIA="CRA_TICKET"
fi
# The branch-name candidate is computed unconditionally, not just when it wins:
# when range evidence outranks it, the run says so rather than dropping it
# silently, so a mis-resolution is visible instead of inferred from the report.
# The stem must be a delimiter-bounded segment of the branch name ("ai" must
# not match "main"); among bounded matches the longest stem wins, so the
# choice never depends on glob order.
best=""
for f in docs/tickets/*.md; do
  [ -f "$f" ] || continue
  stem="$(basename "$f" .md)"
  if [ "$stem" = "ticket-template" ]; then continue; fi
  case "/$BRANCH/" in
    *[/_.-]"$stem"[/_.-]*)
      if [ "${#stem}" -gt "${#best}" ]; then
        best="$stem"
        BRANCH_TICKET="$f"
      fi
      ;;
  esac
done
RANGE_TICKETS=""
TCOUNT=0
# Top level, not inside the MERGE_BASE branch: a helper defined inside a
# conditional is reachable only by an accident of a neighbouring variable
# (review 20260823-124523).
# A range ticket is read as the BLOB at the range end, never as a path: a
# committed symlink under docs/tickets would otherwise inline whatever it
# points at (review 20260822-122651), and the range's tickets are by
# definition committed there. A symlink blob holds only its target path;
# it is named and refused rather than quoted as ticket text.
ticket_blob() {  # <path> — prints the ticket's content at REVIEW_HEAD, or a note
  local mode
  mode="$(git ls-tree "$REVIEW_HEAD" -- "$1" 2>/dev/null | awk '{print $1}')"
  case "$mode" in
    "")     echo "_${1} not present at ${RH_LABEL}_" ;;
    120000) echo "_${1} NOT inlined: it is a symlink at ${RH_LABEL}. Report the missing task-level context as a finding._" ;;
    *)      git show "$REVIEW_HEAD:$1" ;;
  esac
}

if [ -z "$TICKET_ARG" ]; then
  # Tickets committed anywhere in merge-base(BASE,HEAD)..HEAD. That is the WHOLE
  # branch range — wider than the range the pack builder actually diffs on an
  # incremental run (it gets --base EFFECTIVE_BASE) — so a ticket committed in an
  # earlier, already-reviewed commit still resolves on later pushes.
  if [ -n "$MERGE_BASE" ]; then
    # quotePath=false: under git's default a non-ASCII name arrives quoted and
    # octal-escaped, the per-ticket diff below then matches nothing, and the
    # ticket is mistaken for a status-only edit and dropped (review
    # 20260821-142719; docs/tickets/kørelærer.md).
    RANGE_TICKETS_ALL="$(git -c core.quotePath=false diff --name-only --diff-filter=ACMR "$MERGE_BASE" "$REVIEW_HEAD" -- 'docs/tickets/*.md' \
      | grep -v 'ticket-template\.md$' || true)"
    # A ticket whose ONLY change in this range is its Status line was touched by
    # bookkeeping, not by the work. Carrying it makes an unrelated ticket's
    # acceptance criteria the yardstick for this push, and — because every
    # carried ticket is inlined whole — inflates the ticket bundle without
    # bound. roadbuddy-app 2026-08-20: normalising Status lines across 23
    # tickets produced a 192 KB bundle against a 128 KB cap, so EVERY pack
    # refused to build at every split depth, no specialist ran, and the
    # advisory push carried 7 unreviewed commits to the remote.
    RANGE_TICKETS=""
    STATUS_ONLY_TICKETS=""; NOTEXT_TICKETS=""
    while IFS= read -r _t; do
      [ -n "$_t" ] || continue
      # Added/removed lines in this range, minus the diff's own file headers,
      # minus BLANK lines, minus Status lines. Zero left = nothing but
      # bookkeeping changed. Blank lines matter: ADDING a status line to a
      # ticket that had none inserts a blank line with it, and counting that as
      # substance left 10 of 16 bookkeeping edits still resolving as criteria.
      # Two counts from one diff: every changed line, and the changed lines
      # that are substance. "Only the Status line changed" is ALL > 0 and
      # BODY = 0. ALL = 0 is something else — a mode flip, a diff that could
      # not be read — and excluding on BODY alone dropped such a ticket from
      # the push's criteria with a stdout note (roadbuddy 20260822-140937).
      # Fail closed: keep it, and say why.
      _lines="$(git -c core.quotePath=false diff --unified=0 "$MERGE_BASE" "$REVIEW_HEAD" -- "$_t" \
        | grep -E '^[+-]' \
        | grep -vE '^(\+\+\+|---)' || true)"
      _all="$(printf '%s' "$_lines" | grep -c . || true)"
      _body="$(printf '%s' "$_lines" \
        | grep -vE '^[+-][[:space:]]*$' \
        | grep -vcE '^[+-][[:space:]]*\*\*Status:\*\*' || true)"
      case "$_all" in ''|*[!0-9]*) _all=1 ;; esac
      case "$_body" in ''|*[!0-9]*) _body=1 ;; esac
      if [ "$_all" -eq 0 ]; then
        NOTEXT_TICKETS="$NOTEXT_TICKETS $_t"
        RANGE_TICKETS="$RANGE_TICKETS$_t
"
      elif [ "$_body" -eq 0 ]; then
        STATUS_ONLY_TICKETS="$STATUS_ONLY_TICKETS $_t"
      else
        RANGE_TICKETS="$RANGE_TICKETS$_t
"
      fi
    done <<RANGE_TICKET_EOF
$RANGE_TICKETS_ALL
RANGE_TICKET_EOF
    RANGE_TICKETS="$(printf '%s' "$RANGE_TICKETS" | grep -v '^$' || true)"
    if [ -n "$STATUS_ONLY_TICKETS" ]; then
      echo "note: status-line-only edits in this range are bookkeeping, not this push's criteria — excluded:"
      for _t in $STATUS_ONLY_TICKETS; do echo "  - $_t"; done
    fi
    for _t in $NOTEXT_TICKETS; do
      echo "note: $_t has no changed text lines in this range (a mode flip, or a diff that could not be read) — kept as criteria, not treated as bookkeeping"
    done
    TCOUNT="$(printf '%s' "$RANGE_TICKETS" | grep -c . || true)"
    if [ "$TCOUNT" -eq 1 ]; then
      # The BLOB at the range end, like the bundle below — never the working
      # tree path. One ticket is the common case, and it was the one branch
      # still dereferencing a branch-controlled path (roadbuddy
      # 20260822-140937): a committed symlink at docs/tickets/x.md had its
      # target inlined.
      TICKET_SHOW="$RANGE_TICKETS"
      TICKET_ARG="$SCRATCH/ticket-$STAMP.md"
      ticket_blob "$RANGE_TICKETS" > "$TICKET_ARG"
      TICKET_VIA="push-range"
    elif [ "$TCOUNT" -gt 1 ]; then
      # Several tickets shipped in one push: carry them all in one file. The
      # builder grows the ticket section into its surplus pool and refuses the
      # pack when the whole file still cannot be shown; the index up top keeps
      # every ticket at least named if a clipped pack is ever forced through
      # (CRA_ALLOW_CLIPPED_TICKET=1).
      TICKET_ARG="$SCRATCH/tickets-$STAMP.md"
      {
        echo "# Tickets in this push range"
        printf '%s\n' "$RANGE_TICKETS" | sed 's/^/- /'
        printf '%s\n' "$RANGE_TICKETS" | while IFS= read -r t; do
          printf '\n---\n'
          ticket_blob "$t"
        done
      } > "$TICKET_ARG"
      TICKET_VIA="push-range ($TCOUNT tickets)"
      # A batched run hands each pack its OWN bundle (see pack_tickets): the
      # tickets that mention the pack's files, the rest listed by name.
      RANGE_TICKET_LIST="$RANGE_TICKETS"
    fi
  fi
fi
# Fallback: no explicit ticket and nothing committed in the range — the branch
# name is the remaining evidence.
if [ -z "$TICKET_ARG" ] && [ -n "$BRANCH_TICKET" ]; then
  TICKET_ARG="$BRANCH_TICKET"
  TICKET_VIA="branch-match"
fi
if [ -n "$TICKET_ARG" ]; then
  echo "ticket: ${TICKET_SHOW:-$TICKET_ARG} (via $TICKET_VIA)"
  if [ "$TCOUNT" -gt 1 ]; then
    printf '%s\n' "$RANGE_TICKETS" | sed 's/^/  - /'
  fi
  # Range evidence won while a differently-named branch ticket also existed:
  # name it. Silence here is what made the old precedence hard to notice — the
  # run looked normal and only the report's "unclear" criteria hinted at it.
  if [ "$TICKET_VIA" != "branch-match" ] && [ "$TICKET_VIA" != "CRA_TICKET" ] \
     && [ -n "$BRANCH_TICKET" ] \
     && ! printf '%s\n' "$RANGE_TICKETS" | grep -qxF "$BRANCH_TICKET"; then
    echo "note: $BRANCH_TICKET matches the branch name but was not committed in this range — reviewing against the range ticket(s) above; CRA_TICKET=$BRANCH_TICKET uses it instead"
  fi
else
  echo "ticket: none resolved — the review will report missing task-level context"
  if [ "${CRA_REQUIRE_TICKET:-1}" = 1 ]; then
    echo "no ticket resolved: CRA_TICKET unset, no docs/tickets stem bounded in branch '$BRANCH', none committed in $BASE..$RH_LABEL." >&2
    echo "Create docs/tickets/<slug>.md from the ticket template and commit it with the work, or set CRA_TICKET=<path>." >&2
    echo "CRA_REQUIRE_TICKET=0 downgrades this to an in-report finding." >&2
    cleanup
    exit 5
  fi
fi

# ---- Incremental review base ------------------------------------------------
# The marker is one line: "<reviewed-head-sha> <base-ref> <iso-utc> <stamp>".
# A clean review writes a bare sha; a review advanced past advisory highs
# prefixes the sha with "advisory-highs:" (see the read side for why the mode
# lives in the sha field rather than an appended one — version-skew safety).
# It lives INSIDE the git directory (`$(git rev-parse --absolute-git-dir)`), NOT
# in the worktree. That location is deliberate and load-bearing: the marker gates
# which commits get reviewed at all, so it must be somewhere repo *content* can
# never write. A worktree path (an earlier design used .claude/review-reports/)
# is reachable by a crafted branch that commits a TRACKED file — or a symlink —
# at that path, which .gitignore does not prevent (ignore only blocks staging,
# not checkout of already-tracked paths). Such a planted marker would make the
# gate skip the very commits it names as "already reviewed" — a fail-open in a
# gate whose own threat model (above) is untrusted contributors. Under the git
# dir it is still per-clone (a shared marker would let one clone's review silence
# another's) and cannot be materialized by checkout.
#
# The recorded sha is trusted only when ALL of these hold, so a stale, foreign,
# malformed or rewritten marker degrades to a full review rather than skipping
# unseen code (unnumbered on purpose: a count in prose goes stale the moment a
# condition is added, which is how "all four" outlived the fourth condition):
#   - the line is well-formed — four fields, hex sha (see the read side),
#   - the object still exists and is a commit (survives a pruned/rewritten branch),
#   - it is an ancestor of HEAD (it is on the line of history about to be pushed),
#   - the merge-base is an ancestor of it (it is not older than the branch point,
#     which would silently widen — not narrow — the range),
#   - its base field resolves to the same COMMIT as this run's BASE (by sha,
#     not by string — two spellings of one commit must agree).
# (The timestamp is checked separately, at the read side: it never affects
# trust, only what is safe to print.)
MARKER_FILE="$(git rev-parse --absolute-git-dir)/cra-reviewed"
# ---- The pack ledger: finished packs, remembered across runs ----------------
# roadbuddy 20260820-184756: a batched review was killed with 8 of its packs
# finished and 2 never started, and the harness offered no way back — the next
# run would replan and review all of them again. Now every FINISHED pack
# (reviewer completed, report extracted, not UNDETERMINED) is one line here:
#   <key>\t<stamp>\t<high>\t<clipped>\t<report path>\t<group spec>
# keyed by the sha256 of that spec's diff over the reviewed range plus the
# review rules — content, not run identity, so HEAD moving on with unrelated
# commits keeps the finished packs. A later run replans fresh and reuses any
# planned pack whose key is on record, without calling the reviewer. Same
# home as the marker, for the same reason: a branch cannot plant it.
# CRA_RESUME=<stamp> restricts reuse to that run; CRA_FRESH=1 disables it.
# A dry run neither reads nor writes the ledger.
LEDGER_FILE="$(git rev-parse --absolute-git-dir)/cra-packs.tsv"
LEDGER_COUNT=0
if [ -n "${CRA_RESUME:-}" ]; then
  case "$CRA_RESUME" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) : ;;
    *) echo "note: CRA_RESUME='$CRA_RESUME' is not a run stamp (YYYYMMDD-HHMMSS) — reusing nothing"
       CRA_FRESH=1 ;;
  esac
fi
ledger_prune() {  # drop lines that are malformed or whose report is gone
  [ -f "$LEDGER_FILE" ] || return 0
  local kept=0 dropped=0 k s h c r g
  : > "$LEDGER_FILE.tmp"
  while IFS=$'\t' read -r k s h c r g; do
    if [ -n "$g" ] && printf '%s' "$k" | grep -qE '^[0-9a-f]{64}$' \
       && [ -f "$r" ]; then
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$k" "$s" "$h" "$c" "$r" "$g" >> "$LEDGER_FILE.tmp"
      kept=$((kept + 1))
    else
      dropped=$((dropped + 1))
    fi
  done < "$LEDGER_FILE"
  mv "$LEDGER_FILE.tmp" "$LEDGER_FILE"
  if [ "$dropped" -eq 1 ]; then
    echo "ledger: pruned 1 entry whose report no longer exists"
  elif [ "$dropped" -gt 1 ]; then
    echo "ledger: pruned $dropped entries whose report no longer exists"
  fi
  LEDGER_COUNT=$kept
  return 0
}
if [ "$DRY" != 1 ]; then ledger_prune; fi
HEAD_SHA="$REVIEW_HEAD"
HEAD_SHORT="$(git rev-parse --short "$REVIEW_HEAD")"
EFFECTIVE_BASE="$BASE"
INCREMENTAL=0
MARKER_WHY=""
if [ "${CRA_FULL_REVIEW:-0}" = 1 ]; then
  echo "review range: $BASE..$RH_LABEL (full — CRA_FULL_REVIEW=1)"
elif [ -r "$MARKER_FILE" ] && [ -n "$MERGE_BASE" ]; then
  # Field-at-a-time reads: `read a b c d` on a short line leaves the tail empty
  # rather than failing, and every field is validated before use anyway.
  #
  # Format, and why it is shaped this way: field 1 is the reviewed sha,
  # optionally prefixed "advisory-highs:", and a valid line has EXACTLY four
  # fields. These rules exist to survive VERSION SKEW. The marker is per-clone
  # state but the hook is per-checkout — consumers re-copy the skill at their
  # own pace, and an older branch in the same clone can still carry an older
  # hook — so markers and readers of different versions meet. Any reader that
  # parses the fields it knows and ignores the rest would accept an
  # advisory-highs marker as fully trusted and, in blocking mode, skip the
  # unresolved highs below it: the exact fail-open the mode tag exists to
  # prevent. So the mode is encoded where every version already validates. A
  # non-hex field 1 trips even a pre-change reader's own sha check
  # (`*[!0-9a-f]*`) and drops it to a full review; and any field count but four
  # is rejected here, which catches the intermediate version of this hook that
  # briefly wrote the mode as a FIFTH field, whose bare-hex field 1 would
  # otherwise read as clean. Clean markers keep the bare-sha four-field form
  # every version trusts; an unrecognized prefix is non-hex too. Fail-closed in
  # both skew directions.
  RAW_HEAD="$(awk 'NR==1 {print $1}' "$MARKER_FILE")"
  case "$RAW_HEAD" in
    advisory-highs:*) PRIOR_MODE="advisory-highs"; PRIOR_SHA="${RAW_HEAD#advisory-highs:}" ;;
    *)                PRIOR_MODE="clean";          PRIOR_SHA="$RAW_HEAD" ;;
  esac
  PRIOR_BASE="$(awk 'NR==1 {print $2}' "$MARKER_FILE")"
  PRIOR_WHEN="$(awk 'NR==1 {print $3}' "$MARKER_FILE")"
  PRIOR_NF="$(awk 'NR==1 {print NF}' "$MARKER_FILE")"
  MARKER_OK=0
  # A hex sha is required before the value reaches git or the terminal: git
  # cat-file would reject a malformed one anyway, but validating up front also
  # keeps control bytes out of the echoed range line below.
  #
  # The base is compared by RESOLVED COMMIT, not by string: `e016c9f9` and
  # `origin/design-overhaul` naming the same commit must agree. A string
  # comparison rejected exactly that on 2026-08-19 and silently re-reviewed a
  # fully-reviewed 36-commit branch for over an hour. The base field is
  # untrusted file content like the rest of the line, so it is charset-checked
  # before it reaches git, and only its RESOLVED sha (or a placeholder) is ever
  # echoed. Each check names its own failure in MARKER_WHY — "one of four
  # things went wrong" cost an hour of misdiagnosis; the message must say which.
  case "$PRIOR_SHA" in
    *[!0-9a-f]* | "") MARKER_WHY="malformed reviewed-commit field" ;;
    *)
      PRIOR_BASE_SHA=""
      # Revspec characters ~ ^ @ { } are legitimate (main~2, HEAD^, @{u}) and
      # must pass the charset check — rejecting ~ silently distrusted every
      # marker whose base was spelled relative, which is the default in tests.
      case "$PRIOR_BASE" in
        *[!A-Za-z0-9/_.~^@{}-]* | "") : ;;
        *) PRIOR_BASE_SHA="$(git rev-parse --verify --quiet "${PRIOR_BASE}^{commit}" 2>/dev/null || true)" ;;
      esac
      RUN_BASE_SHA="$(git rev-parse --verify --quiet "${BASE}^{commit}" 2>/dev/null || true)"
      if [ "$PRIOR_NF" != 4 ]; then
        MARKER_WHY="unexpected field count ($PRIOR_NF, want 4)"
      elif [ -z "$PRIOR_BASE_SHA" ] || [ -z "$RUN_BASE_SHA" ] || [ "$PRIOR_BASE_SHA" != "$RUN_BASE_SHA" ]; then
        MARKER_WHY="recorded against a different base (marker base resolves to ${PRIOR_BASE_SHA:-nothing}, this run's to ${RUN_BASE_SHA:-nothing})"
      elif ! git cat-file -e "${PRIOR_SHA}^{commit}" 2>/dev/null; then
        MARKER_WHY="reviewed commit no longer exists (pruned or rewritten branch)"
      elif ! git merge-base --is-ancestor "$PRIOR_SHA" "$REVIEW_HEAD" 2>/dev/null; then
        MARKER_WHY="reviewed commit is not an ancestor of $RH_LABEL (different line of history, or a range that ends before it)"
      elif ! git merge-base --is-ancestor "$MERGE_BASE" "$PRIOR_SHA" 2>/dev/null; then
        MARKER_WHY="reviewed commit predates the branch point (would widen the range, not narrow it)"
      else
        MARKER_OK=1
      fi ;;
  esac
  # Only a marker the hook itself wrote reaches the terminal; anything else is
  # shown as a fixed placeholder so a planted timestamp cannot carry escapes.
  case "$PRIOR_WHEN" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z) : ;;
    *) PRIOR_WHEN="an earlier run" ;;
  esac
  # A marker tagged advisory-highs is trusted only by advisory runs. A blocking
  # run must re-examine those highs, so it distrusts such a marker and falls
  # back to the full BASE..HEAD range. This closes the one fail-open the
  # advisory advance introduces: advisory advanced the marker past a high, then
  # the operator switched to blocking. Only "clean" is trusted by both modes —
  # and a prefix this version does not recognize never reaches here, because it
  # leaves field 1 non-hex and the sha check above has already rejected it.
  DEMOTED_BLOCKING=0
  if [ "$MARKER_OK" = 1 ] && [ "${CRA_BLOCKING:-0}" = 1 ]; then
    case "$PRIOR_MODE" in
      clean) : ;;
      *) MARKER_OK=0; DEMOTED_BLOCKING=1 ;;
    esac
  fi
  if [ "$DEMOTED_BLOCKING" = 1 ]; then
    echo "incremental marker was advanced by an advisory run past unresolved high findings — blocking mode reviews $BASE..$RH_LABEL in full so those highs are re-examined"
  elif [ "$MARKER_OK" = 1 ] && [ "$PRIOR_SHA" = "$HEAD_SHA" ]; then
    # Nothing has been committed since that review. Re-reviewing would build the
    # same packs from the same bytes; say so and let the push through.
    if [ "$PRIOR_MODE" = "advisory-highs" ]; then
      echo "already reviewed: HEAD $HEAD_SHORT was reviewed at ${PRIOR_WHEN:-an earlier run}; advisory high findings were reported then (see .claude/review-reports/)."
    else
      echo "already reviewed: HEAD $HEAD_SHORT was reviewed at ${PRIOR_WHEN:-an earlier run} with no high findings."
    fi
    echo "reports in .claude/review-reports/ — CRA_FULL_REVIEW=1 forces a re-review."
    cleanup
    exit 0
  elif [ "$MARKER_OK" = 1 ]; then
    EFFECTIVE_BASE="$PRIOR_SHA"
    INCREMENTAL=1
    NEWC="$(git rev-list --count "$PRIOR_SHA..$REVIEW_HEAD")"
    echo "review range: $(git rev-parse --short "$PRIOR_SHA")..$RH_LABEL ($NEWC commit(s) since the review at ${PRIOR_WHEN:-an earlier run}); earlier commits were reviewed then."
  elif [ -n "$PRIOR_SHA" ] || [ -n "$MARKER_WHY" ]; then
    echo "incremental marker ignored (${MARKER_WHY:-unrecognized}) — falling back to $BASE..$RH_LABEL in full"
  fi
fi

# ---- Scope gate --------------------------------------------------------------
# Refuse instead of silently widening. When the effective range is far larger
# than a normal push candidate, the cause is almost always a mistake upstream of
# this script — a wrong base, a distrusted marker — not a genuine intent to
# review everything again. On 2026-08-19 the fallback path above silently
# launched a ~26-pack, >1h review of a branch that was already fully reviewed;
# the only sign was one line in output nobody was watching. A refusal with the
# counts and the reason turns that hour into a question. CRA_FULL_REVIEW=1 is
# the deliberate answer "yes, review it all"; the caps are tunable via
# CRA_MAX_COMMITS / CRA_MAX_FILES. Exit 6, distinct from every other refusal.
if [ "${CRA_FULL_REVIEW:-0}" != 1 ] && [ -n "$MERGE_BASE" ]; then
  MAXC="${CRA_MAX_COMMITS:-40}"
  MAXF="${CRA_MAX_FILES:-120}"
  RANGE_COMMITS="$(git rev-list --count "$EFFECTIVE_BASE..$REVIEW_HEAD" 2>/dev/null || echo 0)"
  RANGE_FILES="$(git diff --name-only "$EFFECTIVE_BASE..$REVIEW_HEAD" 2>/dev/null | grep -c . || true)"
  if [ "$RANGE_COMMITS" -gt "$MAXC" ] || [ "$RANGE_FILES" -gt "$MAXF" ]; then
    echo "" >&2
    echo "*** SCOPE GATE — REVIEW REFUSED, NOTHING WAS REVIEWED." >&2
    echo "*** This run would cover $RANGE_COMMITS commit(s) / $RANGE_FILES file(s) ($(git rev-parse --short "$EFFECTIVE_BASE" 2>/dev/null || echo "$EFFECTIVE_BASE")..$RH_LABEL), over the cap of $MAXC commits / $MAXF files." >&2
    if [ -n "$MARKER_WHY" ]; then
      echo "*** An incremental marker existed but was not trusted: $MARKER_WHY" >&2
    fi
    echo "*** A range this size is usually a wrong base or a distrusted marker, not a real push candidate." >&2
    echo "*** To review it all anyway: CRA_FULL_REVIEW=1. To raise the cap: CRA_MAX_COMMITS / CRA_MAX_FILES." >&2
    cleanup
    exit 6
  fi
fi
# The range the builder diffs (its `git merge-base "$BASE" HEAD`), computed once
# here so the ledger key hashes exactly the diff a pack was built from.
MB_EFF="$(git merge-base "$EFFECTIVE_BASE" "$REVIEW_HEAD" 2>/dev/null || true)"

# build_pack <out> [paths] -> rc from the builder; its output lands in BUILD_ERR.
# Kept in a variable rather than a file so no later step depends on scratch state.
# Never toggle `set -e` in here either: errexit is global shell state, so a
# `set -e` inside a function silently re-arms it for the caller and the next
# non-zero return kills the script. Use `|| rc=$?`, which errexit exempts.
BUILD_ERR=""
build_pack() {  # <out> [paths] [ticket — defaults to the run's TICKET_ARG]
  local out="$1" paths="${2:-}"
  # EFFECTIVE_BASE, not BASE: on an incremental run it is a commit on this branch,
  # and the builder's own `git merge-base "$BASE" HEAD` resolves it to itself.
  # BUILD_REPO, not REPO_ROOT: a range ending below HEAD is built from the
  # detached worktree at REVIEW_HEAD, and a relative ticket path must point
  # into it too (the builder resolves --ticket against the caller's cwd).
  local args=(--repo "$BUILD_REPO" --base "$EFFECTIVE_BASE" --out "$out")
  local tk="${3:-$TICKET_ARG}"
  if [ -n "$tk" ]; then
    local t="$tk"
    case "$t" in
      /*) : ;;
      *) if [ "$BUILD_REPO" != "$REPO_ROOT" ] && [ -f "$BUILD_REPO/$t" ]; then t="$BUILD_REPO/$t"; fi ;;
    esac
    args+=(--ticket "$t")
  fi
  [ -n "$paths" ] && args+=(--paths "$paths")
  local rc=0
  BUILD_ERR="$(bash "$SKILL_DIR/scripts/build-context-pack.sh" "${args[@]}" 2>&1)" || rc=$?
  printf '%s\n' "$BUILD_ERR" >&2
  return $rc
}

# review_pack <pack> <report> -> sets PACK_HIGH. The count goes in a variable, not
# on stdout: capturing stdout would swallow the report and the dry-run notice.
# PACK_HIGH counts high findings; UNDETERMINED records that a review could not be
# scored at all. They are different states: "0 highs" must never be produced by a
# truncated report, a crashed extractor or a pack that was never reviewed, because
# in blocking mode that reads as "clean" and lets the push through — a fail-open
# safety net is worse than none.
PACK_HIGH=0
UNDETERMINED=0
# How many packs a specialist actually reviewed. Zero at the end is not a clean
# review, it is no review, and it must never exit 0 into a push.
PACKS_REVIEWED=0
# Packs whose reviewer COMPLETED and whose report extracted (or were reused
# from the ledger). The final gate reads this one: a run where every reviewer
# died has PACKS_REVIEWED > 0 and reviewed nothing (roadbuddy 20260822-140937).
PACKS_SCORED=0
CLIPPED=0
review_pack() {
  local pack="$1" report="$2"
  PACK_HIGH=0
  # A clipped pack can only arrive here via an explicit hatch
  # (CRA_ALLOW_CLIPPED_DIFF / CRA_ALLOW_CLIPPED_TICKET / CRA_ALLOW_STARVED), a
  # lowered CRA_DIFF_FLOOR_PCT, or a mild clip above the coverage floor — so
  # surfacing it is a warning, never
  # a block: blocking would negate the hatch the user deliberately set. The
  # specialists are told not to file findings about truncation; owning the
  # signal here is what keeps that rule honest.
  # Every marker is ANCHORED, and the cap marker is looked for only at the tail.
  # A plain substring search over the whole pack self-triggers: this skill's own
  # source quotes these marker strings, so any diff touching build-context-pack.sh
  # or pack_sections.py reported itself as truncated (seen 2026-08-17, when two
  # skill-sync commits made every review of them "clipped"). The builder emits
  # section markers at the start of a line and appends the cap marker last, while
  # diff and excerpt lines always carry a +/-/space or live inside a fence — so
  # anchoring separates a real clip from a quotation of one.
  local clip_note
  clip_note="$(grep -m1 -E '^_\[(diff|ticket) truncated: showing' "$pack" || true)"
  if [ -z "$clip_note" ]; then
    clip_note="$(grep -m1 -E '^_\[WARNING: file excerpts below floor' "$pack" || true)"
  fi
  if [ -z "$clip_note" ]; then
    clip_note="$(tail -3 "$pack" | grep -m1 -F '_[pack truncated at cap]_' || true)"
  fi
  if [ -n "$clip_note" ]; then
    CLIPPED=1
    echo "*** pack coverage warning: $pack — $clip_note" >&2
  fi
  if [ "$DRY" = 1 ]; then
    echo "DRY RUN: would invoke claude -p with pack of $(wc -c < "$pack") bytes -> $report"
    return 0
  fi
  # An incremental pack holds only the commits since the last review, so the
  # specialists must be told the branch is wider than the diff — otherwise every
  # run reports the earlier, already-reviewed work as missing. Section 4 still
  # carries the CURRENT state of each changed file, so the surrounding code is
  # visible; it is the absent EARLIER DIFF that has to be announced.
  local scope_note=""
  if [ "$REVIEW_HEAD" != "$ACTUAL_HEAD" ]; then
    scope_note=" The reviewed range is $(git rev-parse --short "$EFFECTIVE_BASE" 2>/dev/null || echo "$EFFECTIVE_BASE")..$RH_LABEL; the working tree is ahead of it — trust the pack over the checkout."
  fi
  scope_note="$scope_note${PACK_TICKET_NOTE:-}"
  if [ "$INCREMENTAL" = 1 ]; then
    scope_note="$scope_note INCREMENTAL REVIEW: the diff covers only the commits added since an earlier review of this same branch, not the whole branch. Judge what the diff changes. Do NOT report earlier branch work as missing, unreviewed or absent, and do not treat an acceptance criterion as unmet merely because its work is not in this diff — mark such criteria 'unclear'."
  fi
  # "Subagent dispatch is available and expected" is load-bearing, not padding.
  # REVIEW_TOOLS permits Task/Agent, but a review session that defaults to
  # avoiding subagents reads mere permission as no instruction and runs the three
  # specialist passes itself — which it did on 2026-08-19 (report
  # 20260819-155354), leaving every finding single-source with none of the
  # >=2-specialist agreement the skill's merge rules weigh. eval/run_case.sh has
  # carried this wording since it was written; the hook had only the
  # report-the-mode clause, which detects the degradation without preventing it.
  # stdin is /dev/null, deliberately. The hook inherits git's ref-list pipe, and
  # `claude -p` on a non-tty stdin waits 3 s for data before proceeding (measured
  # 2026-09-08: "no stdin data received in 3s, proceeding without it") — a
  # silent 3 s tax per pack and a warning line, for input the reviewer must
  # never consume anyway: the pack is the whole brief.
  if ! claude -p "Use the code-review-agent skill. A context pack is pre-built at $pack — review it per the skill's headless mode and print the full report. Subagent dispatch is available and expected: run the specialist passes as parallel subagents per the skill's execution-mode rule. Fall back to inline passes only if dispatch genuinely fails, and say so in the header. The report header MUST state 'Execution mode: parallel-specialists' or 'Execution mode: inline-sequential' per which mode step 4 actually ran. End with the machine-readable json block.${scope_note}" \
    --allowedTools "${REVIEW_TOOLS[@]}" --disallowedTools "${REVIEW_DENY[@]}" </dev/null | tee "$report"; then
    echo "review did not complete for $pack — scoring UNDETERMINED, not zero" >&2
    UNDETERMINED=1
    return 0
  fi
  # Warn-only mode visibility (never affects the exit code): the parallel
  # fan-out is the measured configuration, so a fallback to inline must be
  # recorded, not silent — that silence is exactly what hid the 2654e3e regression.
  if ! grep -q "Execution mode:" "$report"; then
    echo "warning: $report does not state its execution mode" >&2
  elif grep -q "Execution mode: inline-sequential" "$report"; then
    echo "note: $report ran the inline fallback, not parallel specialists" >&2
  fi
  local counted
  if ! counted="$(python3 "$SKILL_DIR/scripts/extract_findings.py" < "$report" 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(1 for f in d.get("findings",[]) if f.get("severity")=="high"))' 2>/dev/null)"; then
    echo "could not extract findings from $report — scoring UNDETERMINED, not zero" >&2
    UNDETERMINED=1
    return 0
  fi
  case "$counted" in
    ''|*[!0-9]*) echo "unparsable finding count from $report — scoring UNDETERMINED" >&2; UNDETERMINED=1 ;;
    *) PACK_HIGH="$counted" ;;
  esac
  # Trailer appended by the HOOK, not the review session (the session stays
  # read-only), and only after extraction — the json block must stay the last
  # element the extractor sees.
  if [ -n "$clip_note" ]; then
    printf '\n---\n**Coverage warning (added by pre-push hook):** %s\n' "$clip_note" >> "$report"
  fi
}

HIGH=0
UNREVIEWED=""

# ---- Once per run: specs and report names are CLAIMED, atomically ------------
# Packs run as concurrent subshells, so a bash array cannot be the run-wide
# record of what has been scheduled — `mkdir` is atomic and shared, and a second
# claim of the same name fails. On roadbuddy 20260820-184756 the builder's
# split plan answered a refusing group with a spec the run had already
# scheduled: the docs/tickets sub-groups were packed twice, reviewed
# concurrently as byte-identical packs, and — because the report name is
# derived from the spec — written by two `tee`s into the same file. The
# builder no longer repeats a spec; this guard holds even if a plan ever does.
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -c1-64
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 | cut -c1-64
  else python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
  fi
}
pack_key() {  # <spec> [ticket-file] — prints the ledger key, or nothing for an empty diff
  local d
  [ -n "$MB_EFF" ] || return 0
  # The spec is expanded UNQUOTED, word-split, exactly as the builder expands
  # $PATHS: that is how `:(exclude)` terms become separate pathspecs. Quoting
  # it would hash a wider diff than the pack holds. Globbing is off for the
  # expansion (the builder's allowlist admits no glob characters; belt and
  # braces), and IFS is pinned because the split loop below toggles it.
  d="$(set -f; IFS=$' \t\n'; git -c core.quotePath=false diff "$MB_EFF..$REVIEW_HEAD" -- $1 2>/dev/null || true)"
  [ -n "$d" ] || return 0
  # The rules are read from BUILD_REPO — the tree the builder reads — so under
  # CRA_HEAD the key describes the pack that was actually built, not the
  # checkout's rules (review 20260821-142719: this read from cwd).
  # Listed RELATIVE from inside BUILD_REPO: an absolute path would be
  # word-split by the loop, and a checkout under a directory with a space
  # would silently drop the rules from the key (review 20260821-165526).
  # The ticket bytes the pack is built against are part of the key: the
  # bundle varies between runs of the same commits (CRA_TICKET, relevance,
  # the status-only filter), and a pack reviewed against one set of
  # acceptance criteria reused as covered against another claimed coverage
  # no reviewer gave (roadbuddy 20260822-140937). A reuse key must hash every
  # input the cached artefact was derived from.
  { printf '%s' "$d"; printf '\0'
    ( cd "$BUILD_REPO" && for f in $(LC_ALL=C ls .claude/review-rules/*.md 2>/dev/null | LC_ALL=C sort); do cat "$f"; done )
    printf '\0'
    if [ -n "${2:-}" ] && [ -f "$2" ]; then cat "$2"; fi
  } | sha256
}
ledger_lookup() {  # <key> — sets LHIT_STAMP/HIGH/CLIPPED/REPORT; 0 on a usable hit
  local key="$1" line="" want="${CRA_RESUME:-}"
  [ "${CRA_FRESH:-0}" = 1 ] && return 1
  [ -f "$LEDGER_FILE" ] || return 1
  line="$(awk -F'\t' -v k="$key" -v want="$want" \
    'NF==6 && $1==k && (want=="" || $2==want) {l=$0} END{if (l!="") print l}' "$LEDGER_FILE")"
  [ -n "$line" ] || return 1
  IFS=$'\t' read -r _ LHIT_STAMP LHIT_HIGH LHIT_CLIPPED LHIT_REPORT _ <<<"$line"
  case "$LHIT_HIGH" in ''|*[!0-9]*) return 1 ;; esac
  case "$LHIT_CLIPPED" in 0|1) : ;; *) return 1 ;; esac
  case "$LHIT_REPORT" in .claude/review-reports/*.md) : ;; *) return 1 ;; esac
  # A case glob's * matches "/", so the line above is a prefix check, not a
  # containment check: ".claude/review-reports/../../x.md" passed it
  # (roadbuddy 20260822-140937). No ".." or "." component, no empty one.
  case "/$LHIT_REPORT/" in */../*|*/./*|*//*) return 1 ;; esac
  [ -f "$LHIT_REPORT" ] || return 1
  # The ledger cannot be planted, but the report it points at lives in the
  # worktree, where a crafted branch could commit a clean-looking file at that
  # path. A tracked report is therefore never trusted for reuse.
  if git ls-files --error-unmatch -- "$LHIT_REPORT" >/dev/null 2>&1; then
    echo "ledger: $LHIT_REPORT is tracked by git — not reused"
    return 1
  fi
  if ! python3 "$SKILL_DIR/scripts/extract_findings.py" < "$LHIT_REPORT" >/dev/null 2>&1; then
    echo "ledger: $LHIT_REPORT no longer parses — not reused"
    return 1
  fi
  return 0
}
ledger_append() {  # <key> <high> <clipped> <report> <spec> — one O_APPEND write
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$STAMP" "$2" "$3" "$4" "$5" >> "$LEDGER_FILE"
}
# ---- Each pack carries the range tickets that concern it -------------------
# Every range ticket used to be inlined whole into every pack. roadbuddy
# 20260821-174644: three tickets (28 KB) that never mention a 77 KB single file
# starved that file's pack into a refusal. A ticket is relevant to a pack when
# its own file is among the pack's changed paths, or its text mentions one of
# them — the path, its file name, or a containing directory of two or more
# segments (one segment, "src", would match everything). The others are listed
# by name so the reviewer knows what the push is for. An explicit CRA_TICKET
# is never filtered; a single range ticket is still carried whole.
# CRA_TICKET_RELEVANCE=0 restores the full bundle.
pack_tickets() {  # <n> <spec> — prints the ticket path to hand the builder
  local n="$1" spec="$2" out="$SCRATCH/tickets-$STAMP-$n.md"
  if [ -z "$RANGE_TICKET_LIST" ] || [ "${CRA_TICKET_RELEVANCE:-1}" = 0 ] \
     || [ -n "${CRA_TICKET:-}" ] || [ -z "$MB_EFF" ]; then
    printf '%s' "$TICKET_ARG"; return 0
  fi
  local paths cands="$SCRATCH/tickcand-$n" t body total=0 hit=0 hits="" p d
  paths="$(set -f; IFS=$' \t\n'; git -c core.quotePath=false diff --name-only "$MB_EFF..$REVIEW_HEAD" -- $spec 2>/dev/null || true)"
  : > "$cands"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    printf '%s\n' "$p" "${p##*/}" >> "$cands"
    d="$p"
    while [ "${d%/*}" != "$d" ]; do
      d="${d%/*}"
      case "$d" in */*) printf '%s\n' "$d" >> "$cands" ;; esac
    done
  done <<PATHS_EOF
$paths
PATHS_EOF
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    total=$((total + 1))
    if printf '%s\n' "$paths" | grep -qxF -- "$t" \
       || { [ -s "$cands" ] && git show "$REVIEW_HEAD:$t" 2>/dev/null | grep -qF -f "$cands"; }; then
      hit=$((hit + 1)); hits="$hits$t
"
    fi
  done <<TICKETS_EOF
$RANGE_TICKET_LIST
TICKETS_EOF
  {
    echo "# Tickets in this push range ($total)"
    printf '%s\n' "$RANGE_TICKET_LIST" | sed 's/^/- /'
    if [ "$hit" -gt 0 ]; then
      echo
      echo "Inlined below: the $hit that mention this pack's files. The others concern other packs of this push."
      printf '%s' "$hits" | while IFS= read -r t; do
        [ -n "$t" ] || continue
        printf '\n---\n'
        ticket_blob "$t"
      done
    else
      echo
      # Factual only. The instruction to the reviewer rides in the prompt the
      # harness controls (see review_pack), never inside this section, which
      # is quoted contributor content.
      echo "None of them mentions this pack's files."
    fi
  } > "$out"
  printf '%s' "$hit" > "$SCRATCH/tickhits-$n"
  echo "pack $n: tickets: $hit of $total range ticket(s) mention its files" >&2
  printf '%s' "$out"
}
is_single_file() {  # <spec> — one positive term naming a file in the build tree
  case "$1" in ''|*' '*|:*) return 1 ;; esac
  [ -f "$BUILD_REPO/$1" ]
}
claim_spec() {  # <spec> — 0 on first claim, 1 if this run already scheduled it
  mkdir -p "$SCRATCH/seen"
  mkdir "$SCRATCH/seen/$(printf '%s' "$1" | sha256)" 2>/dev/null
}
# The stem is BOUNDED, and a failure that is not "already claimed" ends the
# loop. Both halves of that sentence are the same bug (2026-09-08): pack 1 of a
# batched run carried `.claude` plus four :(exclude) terms, which flattens to a
# 270-character slug — past NAME_MAX (255). `mkdir` failed with ENAMETOOLONG,
# the loop answered by making the name LONGER, and the run spun forking mkdir
# forever. It hung inside the first command substitution of run_one_pack, so no
# builder and no reviewer was ever started: nothing to see in `pgrep`, no
# packlog, no report, and — as a pre-push hook — a `git push` that never
# returned. One occurrence sat wedged for 3d17h. Every existing net missed it:
# the INT/TERM trap needs an interrupt, and both exit-7 gates need the run to
# FINISH. A slug long enough to claim but too long for
# ".claude/review-reports/$STAMP-<slug>.md" fails closed at the `tee` instead —
# UNDETERMINED, not a hang — but it wastes a reviewer call, so the cap leaves
# room for the stamp, the "-<k>" suffix and the extension.
SLUG_STEM_MAX=180
claim_report() {  # <slug> — prints a slug no other pack in this run holds
  local base="$1" slug k=1
  # Truncating alone would collide two long specs onto one report — the very
  # thing this function exists to prevent — so the trimmed stem carries a hash
  # of the WHOLE slug.
  if [ "${#base}" -gt "$SLUG_STEM_MAX" ]; then
    base="${base:0:$SLUG_STEM_MAX}-$(printf '%s' "$1" | sha256 | cut -c1-8)"
  fi
  slug="$base"
  mkdir -p "$SCRATCH/slug"
  while ! mkdir "$SCRATCH/slug/$slug" 2>/dev/null; do
    # Not EEXIST: the name itself is unusable (a full disk, a lost scratch dir,
    # a length the cap above did not foresee). Looping cannot fix any of those.
    # Hand out a name unique to this call and let the pack proceed — a report
    # written under an odd name beats a push that never returns. $BASHPID, not
    # $$: every pool job is a subshell, and $$ is the parent's pid in all of
    # them (reviews 20260908-113713 and the mimiry review of the same commit);
    # $RANDOM separates the recursion's frames, which share a BASHPID. The
    # fallback is CLAIMED like any other name (review 20260908-183536: an
    # unclaimed one broke this function's contract) — three tries, bounded,
    # because an unbounded retry here is the loop this function just lost.
    if [ ! -d "$SCRATCH/slug/$slug" ]; then
      local fb
      for k in 1 2 3; do
        fb="$base-$BASHPID-$RANDOM"
        if mkdir "$SCRATCH/slug/$fb" 2>/dev/null; then printf '%s' "$fb"; return 0; fi
      done
      echo "claim_report: could not claim any name under $SCRATCH/slug — using $fb unclaimed" >&2
      printf '%s' "$fb"
      return 0
    fi
    k=$((k + 1)); slug="$base-$k"
  done
  printf '%s' "$slug"
}

# Never a silent cap: name what was not covered, every time. Defined once because
# it is printed from TWO places — the normal end of a run, and the early abort
# when a first wave reviews nothing. The abort used to skip it, which turned the
# loudest failure into the quietest output.
report_unreviewed() {
  if [ -n "$UNREVIEWED" ]; then
    echo "" >&2
    echo "*** INCOMPLETE REVIEW — these paths could not be packed even alone and were NOT reviewed:" >&2
    for g in $UNREVIEWED; do echo "***   $g" >&2; done
    echo "*** Run the skill interactively on them, or split further with --paths." >&2
  fi
}

PACK="$SCRATCH/pack-$STAMP.md"
rc=0
build_pack "$PACK" || rc=$?

if [ "$rc" -eq 0 ]; then
  REPORT=".claude/review-reports/$STAMP.md"
  review_pack "$PACK" "$REPORT"
  HIGH=$PACK_HIGH
  # The single-pack path reviews inline and never reaches fold_pack_status, so
  # it must count itself. Missing this made the zero-pack gate fire on every
  # ORDINARY push — caught by the upstream suite before it shipped.
  PACKS_REVIEWED=1
  [ "$UNDETERMINED" = 0 ] && PACKS_SCORED=1
elif [ "$rc" -eq 65 ]; then
  # Too large for one honest pack. Batch by the builder's own --paths suggestions
  # rather than skipping: the branches that cannot be packed are the ones with the
  # most room to hide a defect, so a silent skip fails open exactly where it matters.
  # The builder prints one `--paths "<spec>"` suggestion per batch: a solo line
  # for each file whose diff dominates, and one line per directory group for
  # the rest (with :(exclude) pathspecs for the solo files). Each suggestion is
  # one quoted string per line, so newline-split iteration is safe and avoids
  # the array/errexit edge cases that make `mapfile` fragile under `set -euo pipefail`.
  # ANCHOR the scrape to a genuine suggestion line — indentation then `--paths` —
  # never a `# cannot batch by name …` announcement, which echoes a rejected,
  # contributor-controlled path. A name that could smuggle a quoted --paths token
  # cannot currently reach an announcement (git quotes such a header and the
  # perfile parser skips it), but anchoring makes that guarantee structural
  # rather than a coincidence of two parsers (review 20260819-095054).
  GROUP_LIST="$(printf '%s\n' "$BUILD_ERR" | sed -n 's/^[[:space:]]*--paths "\([^"]*\)".*/\1/p')"
  GROUP_COUNT="$(printf '%s' "$GROUP_LIST" | grep -c . || true)"
  if [ -z "$GROUP_LIST" ] || [ "$GROUP_COUNT" -eq 0 ]; then
    echo "branch too large for one context pack and no batch suggestions were parsed — REVIEW DID NOT RUN" >&2
    cleanup
    # Nothing was reviewed. Advisory mode still returns 0; blocking mode must not
    # treat "no review happened" as "no findings".
    if [ "${CRA_BLOCKING:-0}" = 1 ]; then exit 2; fi
    exit 0
  fi
  # Packs are independent — own context pack, own report file, own `claude -p`
  # session — so running a few at once is throughput, not a semantic change.
  # What is NOT safe is letting them write to the terminal at once: interleaved
  # output from concurrent reviews is unreadable, and an unreadable log is how a
  # runaway review went unnoticed for 55 minutes on 2026-08-19. So a concurrent
  # pack's output is captured and replayed WHOLE when that pack finishes; the
  # only live lines are the parent's own "pack i/N" heartbeats, printed the
  # moment each pack starts and again when it finishes. Packs run in a rolling
  # pool (see the loop below): a freed slot is refilled at once, so one slow
  # pack never holds the others back.
  PACK_JOBS="${CRA_PACK_JOBS:-3}"
  case "$PACK_JOBS" in ''|*[!0-9]*) PACK_JOBS=1 ;; esac
  # 1..10, announced when clamped. Each pack is its own reviewer session with
  # three specialist subagents, so an unbounded value is a rate-limit incident;
  # a silently adjusted knob is a mystery the next reader has to solve.
  if [ "$PACK_JOBS" -lt 1 ]; then
    echo "note: CRA_PACK_JOBS=$CRA_PACK_JOBS is below 1 — clamped to 1"
    PACK_JOBS=1
  elif [ "$PACK_JOBS" -gt 10 ]; then
    echo "note: CRA_PACK_JOBS=$CRA_PACK_JOBS is above 10 — clamped to 10"
    PACK_JOBS=10
  fi
  IFS_SAVE="$IFS"
  IFS=$'\n'
  PACK_GROUPS=()
  # Claimed in the PARENT before any job starts, so a sub-plan inside a
  # concurrent pack can never race a top-level group for the same spec.
  for g in $(printf '%s\n' "$GROUP_LIST" | awk '!seen[$0]++'); do
    claim_spec "$g" || continue
    PACK_GROUPS+=("$g")
  done
  IFS="$IFS_SAVE"
  # NOT `GROUPS`: bash owns that name (the caller's OS group IDs) and assigning
  # to it is silently ignored, so a driver written against it reviews paths
  # named "1000" and "27" instead of the branch. That is not hypothetical — it
  # happened on 2026-08-19 and produced seven reviews of folders that do not
  # exist.
  PACK_TOTAL="${#PACK_GROUPS[@]}"

  # Bash background jobs are subshells, so HIGH/UNDETERMINED/CLIPPED/UNREVIEWED
  # cannot travel back in variables. Each pack writes one status line the parent
  # folds in afterwards. A pack that dies WITHOUT writing one scores
  # UNDETERMINED, never 0 — the same fail-closed rule review_pack applies to a
  # report it cannot score, and for the same reason: in blocking mode a phantom
  # zero reads as "clean" and lets the push through.
  # A group that will not pack is SPLIT AGAIN, not dropped. The builder already
  # prints narrower `--paths` suggestions when a build refuses; before
  # 2026-08-20 nothing consumed them at this level, so a group that failed was
  # recorded UNREVIEWED and never looked at. That is how the whole Kørelærer
  # terminology sweep (src/i18n, 17 files) shipped without a review: the group
  # refused because two tickets left 4096 B for a 16528 B ticket, and the run
  # said so honestly and moved on.
  #
  # Depth-bounded so this always terminates, and every level narrows: the
  # builder refuses to suggest a scope as wide as the one that just failed.
  # Exhausting the depth still ends in UNREVIEWED — loud, never silent.
  run_one_pack() {  # <n> <group> [depth]
    local n="$1" g="$2" depth="${3:-0}" brc=0
    local BPACK="$SCRATCH/pack-$STAMP-$n.md"
    # The slug is CLAIMED: two distinct specs that flatten to the same name
    # (delta/f1.ts and delta/f1_ts) get "-2", "-3" — never the same file.
    local BREPORT=".claude/review-reports/$STAMP-$(claim_report "$(echo "$g" | tr -c 'A-Za-z0-9' '-' | sed 's/-*$//')").md"
    # `local`, not bare assignment. Every call site is a subshell today (see the
    # wave loop), so these could not leak — but that is a property of the
    # CALLERS, and the 2026-08-20 high existed precisely because one caller was
    # not a subshell. Declared local, the function is correct however it is
    # called, including from the recursion below. bash's dynamic scoping means
    # review_pack still writes the copy this frame reads.
    local UNDETERMINED=0
    local CLIPPED=0
    # Reuse before building: a pack on the ledger needs neither the builder
    # nor the reviewer. A group that would refuse to build has no leaf entry,
    # so it always falls through to the split path below.
    local pt
    pt="$(pack_tickets "$n" "$g" 2>>"$SCRATCH/packlog-$n")"
    [ -s "$SCRATCH/packlog-$n" ] && cat "$SCRATCH/packlog-$n"
    local key=""
    if [ "$DRY" != 1 ]; then
      key="$(pack_key "$g" "$pt")"
      if [ -n "$key" ] && ledger_lookup "$key"; then
        echo "pack $n: $g — reused from run $LHIT_STAMP ($LHIT_REPORT)"
        printf '%s\t%s\t%s\t%s\n' "$n" "$g" "$LHIT_STAMP" "$LHIT_REPORT" >> "$SCRATCH/reused.list"
        printf 'high=%s undetermined=0 clipped=%s unreviewed=0 packs=1 scored=1\n' \
          "$LHIT_HIGH" "$LHIT_CLIPPED" > "$SCRATCH/packstat-$n"
        return 0
      fi
    fi
    # Seen by review_pack in this subshell: a pack no range ticket mentions
    # gets its instruction in the prompt, not in the pack.
    PACK_TICKET_NOTE=""
    if [ -f "$SCRATCH/tickhits-$n" ] && [ "$(cat "$SCRATCH/tickhits-$n")" = 0 ]; then
      PACK_TICKET_NOTE=" No range ticket mentions this pack's files: the ticket section lists them by name only. Mark acceptance criteria 'unclear' for this pack and judge the diff on its own merits."
    fi
    build_pack "$BPACK" "$g" "$pt" || brc=$?
    if [ "$brc" -ne 0 ]; then
      local subs=""
      if [ "$depth" -lt "${CRA_MAX_SPLIT_DEPTH:-3}" ]; then
        # Same anchored scrape as the top level: indentation then --paths, so a
        # `# cannot batch by name ...` announcement (which echoes a
        # contributor-controlled path) can never be read as a suggestion.
        subs="$(printf '%s\n' "$BUILD_ERR" | sed -n 's/^[[:space:]]*--paths "\([^"]*\)".*/\1/p')"
      fi
      # A split that would fan out into dozens of packs is not a split, it is a
      # wrong base wearing a disguise. Refuse it the way the scope gate refuses
      # an oversized range: loudly, with the count and the override, rather than
      # quietly starting hours of work. Without this the 2026-08-20 recursion
      # turned one unreviewable 200-file group into FIFTY packs and simply began.
      local sub_count=0
      [ -n "$subs" ] && sub_count="$(printf '%s\n' "$subs" | grep -c .)"
      local sub_cap="${CRA_MAX_SPLIT_PACKS:-12}"
      if [ -n "$subs" ] && [ "$sub_count" -gt "$sub_cap" ]; then
        echo "*** pack $n: \"$g\" will not pack, and splitting it would make $sub_count packs (cap $sub_cap)." >&2
        echo "***   NOT REVIEWED. A group this wide is a wrong base — or a genuinely broad change" >&2
        echo "***   (the scope gate has already accepted the range's size). Narrow the range if it is" >&2
        echo "***   wrong; if it is right, raise CRA_MAX_SPLIT_PACKS deliberately and rerun — finished" >&2
        echo "***   packs are reused." >&2
        printf 'high=0 undetermined=0 clipped=0 unreviewed=1 packs=0 scored=0\n' > "$SCRATCH/packstat-$n"
        return 0
      fi
      # A SINGLE FILE has nothing narrower to split into. Batching is impossible
      # for it, so the alternatives are a ticket-clipped pack — whose in-band
      # marker, coverage warning and held marker already exist — or no review
      # at all. roadbuddy 20260821-174644: a 77 KB single-file diff left 14 KB
      # for a 28 KB range-ticket bundle, and the file went NOT REVIEWED. Retry
      # once with the ticket clipped, loudly; a file whose DIFF cannot fit even
      # then refuses again and stays INCOMPLETE. CRA_CLIP_SINGLE_FILE_TICKET=0
      # opts out.
      if [ -z "$subs" ] && [ "${CRA_CLIP_SINGLE_FILE_TICKET:-1}" = 1 ] && is_single_file "$g"; then
        echo "pack $n: $g — a single file has nothing narrower to split; retrying with the ticket clipped (coverage gap recorded)"
        brc=0
        CRA_ALLOW_CLIPPED_TICKET=1 build_pack "$BPACK" "$g" "$pt" || brc=$?
        if [ "$brc" -eq 0 ]; then
          review_pack "$BPACK" "$BREPORT"
          if [ "$DRY" != 1 ] && [ "$UNDETERMINED" = 0 ] && [ -n "$key" ]; then
            ledger_append "$key" "$PACK_HIGH" "$CLIPPED" "$BREPORT" "$g"
          fi
          printf 'high=%s undetermined=%s clipped=%s unreviewed=0 packs=1 scored=%s\n' \
            "$PACK_HIGH" "$UNDETERMINED" "$CLIPPED" "$((1 - UNDETERMINED))" > "$SCRATCH/packstat-$n"
          return 0
        fi
        echo "pack $n: $g — still will not pack with the ticket clipped; the diff itself does not fit"
      fi
      if [ -n "$subs" ]; then
        local sub_i=0 sub_n="" IFS_SUB="$IFS"
        local acc_high=0 acc_und=0 acc_cl=0 acc_unrev=0 acc_packs=0 acc_scored=0 sub_ran=0
        echo "pack $n: \"$g\" will not pack — splitting further (depth $((depth + 1)))"
        IFS=$'\n'
        for sub in $subs; do
          IFS="$IFS_SUB"
          sub_i=$((sub_i + 1))
          sub_n="$n.$sub_i"
          # A spec this run has already scheduled — at the top level or in any
          # other subtree — is not reviewed again. It is covered where it was
          # first claimed, so it is neither a gap nor an unscoreable pack here.
          if ! claim_spec "$sub"; then
            echo "  pack $sub_n: $sub — already scheduled in this run, skipped"
            IFS=$'\n'
            continue
          fi
          echo "  pack $sub_n: $sub"
          sub_ran=1
          run_one_pack "$sub_n" "$sub" "$((depth + 1))"
          if [ -r "$SCRATCH/packstat-$sub_n" ]; then
            local sl kv
            sl="$(cat "$SCRATCH/packstat-$sub_n")"
            for kv in $sl; do
              case "$kv" in
                high=*)         acc_high=$((acc_high + ${kv#high=})) ;;
                undetermined=1) acc_und=1 ;;
                clipped=1)      acc_cl=1 ;;
                unreviewed=1)   acc_unrev=1 ;;
                packs=*)        acc_packs=$((acc_packs + ${kv#packs=})) ;;
                scored=*)       acc_scored=$((acc_scored + ${kv#scored=})) ;;
              esac
            done
          else
            acc_und=1
          fi
          IFS=$'\n'
        done
        IFS="$IFS_SUB"
        # "Covered where it was first claimed" holds for a sibling, not for an
        # ancestor. A plan that answers only with specs this run already holds
        # — sharpest, the group itself — ran nothing here, and writing
        # packs=0 unreviewed=0 made that a silent clean exit (roadbuddy
        # 20260822-140937). No sub-pack ran: this group is NOT reviewed.
        if [ "$sub_ran" -eq 0 ]; then
          echo "pack $n: \"$g\" — every suggested sub-pack was already scheduled in this run (the plan answered with its own scope); NOT REVIEWED" >&2
          acc_unrev=1
        fi
        printf 'high=%s undetermined=%s clipped=%s unreviewed=%s packs=%s scored=%s\n' \
          "$acc_high" "$acc_und" "$acc_cl" "$acc_unrev" "$acc_packs" "$acc_scored" > "$SCRATCH/packstat-$n"
        return 0
      fi
      printf 'high=0 undetermined=0 clipped=0 unreviewed=1 packs=0 scored=0\n' > "$SCRATCH/packstat-$n"
      return 0
    fi
    review_pack "$BPACK" "$BREPORT"
    # Finished means: reviewer completed AND the report extracted. An
    # UNDETERMINED pack is a coverage gap and must be reviewed again next time.
    if [ "$DRY" != 1 ] && [ "$UNDETERMINED" = 0 ] && [ -n "$key" ]; then
      ledger_append "$key" "$PACK_HIGH" "$CLIPPED" "$BREPORT" "$g"
    fi
    printf 'high=%s undetermined=%s clipped=%s unreviewed=0 packs=1 scored=%s\n' \
      "$PACK_HIGH" "$UNDETERMINED" "$CLIPPED" "$((1 - UNDETERMINED))" > "$SCRATCH/packstat-$n"
    return 0
  }

  fold_pack_status() {  # <n> <group>
    local n="$1" g="$2" line="" kv h="" u="" c="" ur="" pk="" sc=""
    # Reset before the early return below: the done line prints these, and a
    # status-less pack must not borrow the previous pack's count.
    FOLD_HIGH=0; FOLD_UNREVIEWED=0
    if [ ! -f "$SCRATCH/packstat-$n" ]; then
      echo "pack $n ($g) ended without a status — scoring UNDETERMINED, not zero" >&2
      UNDETERMINED=1
      return 0
    fi
    line="$(cat "$SCRATCH/packstat-$n")"
    for kv in $line; do
      case "$kv" in
        high=*)         h="${kv#high=}" ;;
        undetermined=*) u="${kv#undetermined=}" ;;
        clipped=*)      c="${kv#clipped=}" ;;
        unreviewed=*)   ur="${kv#unreviewed=}" ;;
        packs=*)        pk="${kv#packs=}" ;;
        scored=*)       sc="${kv#scored=}" ;;
      esac
    done
    case "$h" in
      ''|*[!0-9]*)
        echo "pack $n ($g) wrote an unreadable status — scoring UNDETERMINED" >&2
        UNDETERMINED=1; h=0 ;;
    esac
    HIGH=$((HIGH + h))
    FOLD_HIGH="$h"; FOLD_UNREVIEWED="${ur:-0}"
    if [ "$u" = 1 ]; then UNDETERMINED=1; fi
    if [ "$c" = 1 ]; then CLIPPED=1; fi
    if [ "$ur" = 1 ]; then UNREVIEWED="$UNREVIEWED $g"; fi
    # Counts packs that REACHED a specialist — built and sent. Deliberately NOT
    # "scored cleanly": a pack whose reviewer died mid-run is a coverage gap,
    # already handled (advisory, marker held, UNDETERMINED printed), and calling
    # that "nothing was reviewed" would abort runs that are working. The state
    # this counter exists to catch is different and structural: no pack could be
    # BUILT at all, so no specialist ever ran. The `packs=` key carries the
    # subtree's own count (a split parent sums its children; a child skipped as
    # a duplicate adds nothing); a status without it falls back to the old rule.
    case "$pk" in
      ''|*[!0-9]*) if [ "$ur" != 1 ]; then PACKS_REVIEWED=$((PACKS_REVIEWED + 1)); fi ;;
      *)           PACKS_REVIEWED=$((PACKS_REVIEWED + pk)) ;;
    esac
    # Counts packs whose reviewer FINISHED: completed and extracted, or reused.
    # The final gate reads this one — a counter named after the outcome must
    # count the outcome, not the attempt (roadbuddy 20260822-140937).
    case "$sc" in
      ''|*[!0-9]*) if [ "$ur" != 1 ] && [ "$u" != 1 ]; then PACKS_SCORED=$((PACKS_SCORED + 1)); fi ;;
      *)           PACKS_SCORED=$((PACKS_SCORED + sc)) ;;
    esac
    return 0
  }

  if [ "$PACK_JOBS" -gt 1 ]; then
    echo "branch too large for one context pack — batching into $PACK_TOTAL path-scoped reviews, up to $PACK_JOBS at a time"
  else
    echo "branch too large for one context pack — batching into $PACK_TOTAL path-scoped reviews"
  fi
  if [ "$DRY" != 1 ]; then
    echo "ledger: $LEDGER_FILE ($LEDGER_COUNT finished pack(s) on record; CRA_FRESH=1 ignores it, CRA_RESUME=<stamp> pins one run)"
  fi

  # A ROLLING POOL, not waves (2026-08-21). A wave is as slow as its slowest
  # pack: with three slots and one 40-minute pack, two slots idle for most of
  # it. Here a slot is refilled the moment it frees. `wait -n` reaps exactly one
  # child; the pid scan that follows finds which one (a reaped pid no longer
  # answers `kill -0`; children that finished meanwhile are zombies, still
  # answer, and are picked up next lap when `wait -n` returns at once). Pid reuse
  # between the reap and the scan is the theoretical hole — noted, not handled.
  # ONE path, whatever PACK_JOBS is. Sequential mode once had a direct-call
  # branch so its output streamed live; that second path is the one that
  # diverged (2026-08-20: each pack wiped the previous pack's coverage flags).
  # With one loop there is no parity to claim and no second place to forget.
  HAVE_WAIT_N=0
  if [ "${BASH_VERSINFO[0]}" -gt 4 ] \
     || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 3 ]; }; then
    HAVE_WAIT_N=1
  else
    echo "note: bash $BASH_VERSION lacks 'wait -n' — packs run in waves of $PACK_JOBS instead of a rolling pool"
  fi
  FIRST_N=$PACK_JOBS
  [ "$FIRST_N" -gt "$PACK_TOTAL" ] && FIRST_N=$PACK_TOTAL
  next=0; running=0; FINISHED=0
  PID_N=(); START_N=(); DONE_N=()
  # Stop every pack still running: TERM its subshell and, best effort, the
  # reviewer beneath it. Process-group control is out of scope (ticket
  # non-goal); an orphaned reviewer after a hard kill is the same exposure as
  # before.
  stop_packs() {
    local i
    for i in "${!PID_N[@]}"; do
      [ -z "${DONE_N[$i]:-}" ] || continue
      if command -v pkill >/dev/null 2>&1; then pkill -TERM -P "${PID_N[$i]}" 2>/dev/null || true; fi
      kill -TERM "${PID_N[$i]}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
  }
  # INT/TERM in the PARENT only (subshells reset signal traps, so the pack
  # jobs cannot run this twice): say how far the run got and how to pick it
  # up, stop the packs, clean up, exit 130. The finished packs are already on
  # the ledger — a closed session loses nothing but the sentence that says so,
  # and this is that sentence. Note: a job started with `&` from a
  # non-interactive shell ignores INT; that is why stop_packs sends TERM.
  on_signal() {
    trap - INT TERM
    echo "" >&2
    echo "*** run $STAMP interrupted: $FINISHED/$PACK_TOTAL packs finished; rerun to resume (ledger: $LEDGER_FILE)" >&2
    stop_packs
    cleanup
    exit 130
  }
  trap on_signal INT TERM
  while [ "$FINISHED" -lt "$PACK_TOTAL" ]; do
    while [ "$running" -lt "$PACK_JOBS" ] && [ "$next" -lt "$PACK_TOTAL" ]; do
      n=$((next + 1))
      # One line per pack, before the work starts: a tailed log must always show
      # which pack is running and how many remain — a silent hour is a fault.
      echo "pack $n/$PACK_TOTAL: ${PACK_GROUPS[$next]}"
      START_N[$n]=$SECONDS
      run_one_pack "$n" "${PACK_GROUPS[$next]}" > "$SCRATCH/packout-$n" 2>&1 &
      PID_N[$n]=$!
      # One manifest row per pack for cra-watch.py: number, pid, launch epoch,
      # spec. Append-only, best-effort; nothing here reads it back.
      printf '%s\t%s\t%s\t%s\n' "$n" "$!" "$(date +%s)" "${PACK_GROUPS[$next]}" \
        >> "$SCRATCH/pool.tsv" 2>/dev/null || true
      next=$((next + 1)); running=$((running + 1))
    done
    # `|| true`: a failed job must reach fold_pack_status (which fails closed
    # on a missing status), not kill the run through errexit.
    if [ "$HAVE_WAIT_N" = 1 ]; then wait -n || true; else wait || true; fi
    i=1
    while [ "$i" -le "$next" ]; do
      if [ -z "${DONE_N[$i]:-}" ] && ! kill -0 "${PID_N[$i]}" 2>/dev/null; then
        DONE_N[$i]=1; FINISHED=$((FINISHED + 1)); running=$((running - 1))
        # The pack's captured output is replayed WHOLE, here, so concurrent
        # packs never interleave on the terminal — an unreadable log is how a
        # runaway review went unnoticed for 55 minutes on 2026-08-19.
        if [ -f "$SCRATCH/packout-$i" ]; then
          cat "$SCRATCH/packout-$i"
        fi
        fold_pack_status "$i" "${PACK_GROUPS[$((i - 1))]}"
        if [ "$FOLD_UNREVIEWED" = 1 ]; then
          echo "pack $i/$PACK_TOTAL done (NOT REVIEWED, $(( (SECONDS - START_N[i]) / 60 )) min)"
        else
          echo "pack $i/$PACK_TOTAL done ($FOLD_HIGH high, $(( (SECONDS - START_N[i]) / 60 )) min)"
        fi
        # MANDATORY CHECK AFTER THE FIRST WAVE (Oliver, 2026-08-20). If not one
        # of the first PACK_JOBS packs to finish reached a specialist, the run is
        # not "finding nothing", it is broken — a bad base, an unpackable ticket,
        # a missing CLI. Continuing spends the whole branch's tokens to arrive at
        # the same nothing. Stop here, loudly, and refuse the push.
        if [ "$FINISHED" -eq "$FIRST_N" ] && [ "$PACKS_REVIEWED" -eq 0 ]; then
          stop_packs
          report_unreviewed
          echo "" >&2
          echo "*** REVIEW ABORTED AFTER THE FIRST WAVE — not one of the first $FIRST_N pack(s) to finish reached a specialist." >&2
          echo "*** That is a broken run, not a clean one. The remaining $((PACK_TOTAL - next)) pack(s) were NOT started; $running running pack(s) were stopped." >&2
          echo "*** Read the pack output above: the usual causes are an over-wide base, a ticket bundle" >&2
          echo "*** that will not fit the cap, or a missing reviewer CLI." >&2
          echo "*** THE PUSH IS REFUSED (exit 7). Nothing here was reviewed." >&2
          cleanup
          exit 7
        fi
      fi
      i=$((i + 1))
    done
  done
  trap - INT TERM
else
  cleanup
  exit "$rc"
fi

report_unreviewed

echo "high-severity findings: $HIGH (reports in .claude/review-reports/)"
if [ -s "$SCRATCH/reused.list" ]; then
  echo "reused $(grep -c . "$SCRATCH/reused.list") pack(s) from earlier runs (no reviewer call):"
  while IFS=$'\t' read -r _rn _rg _rs _rr; do
    echo "  - pack $_rn: $_rg <- run $_rs, $_rr"
  done < "$SCRATCH/reused.list"
fi
[ "$UNDETERMINED" = 1 ] && echo "*** at least one pack could not be scored — the count above is a FLOOR, not a total" >&2
[ "$CLIPPED" = 1 ] && echo "*** at least one reviewed pack was truncated or starved — findings cover only the shown portion" >&2

# A refusal precedes its side effects: this gate once sat after the marker
# advance, the one refusal in the file that did (roadbuddy 20260822-140937).
# A review that reviewed NOTHING is not a review, in any mode. Advisory means
# FINDINGS do not delay a push; it never meant an empty run may accompany one.
# On 2026-08-20 every pack refused to build, this script exited 0, and the hook
# — which is `exec bash <this>` — let 7 unreviewed commits reach the remote.
# Oliver, that day: "A review that reviews NOTHING/ZERO is not a review. That is
# just wasted tokens."
# No DRY exemption, deliberately, and the early gate above has none either: a
# dry run still BUILDS its packs, so "no pack could be built" is a real failure
# in that mode too. A dry run whose packs build fine counts them and exits 0.
# The two gates must agree — one exempting dry runs and the other not is the
# kind of split that hides a fail-open for months.
# Two counters, two failures: no pack could be BUILT (structural, and the
# first-wave gate above catches it early), or packs were built and sent but
# no reviewer FINISHED — a missing CLI, an expired session, a limit hit on
# pack 1. The second left PACKS_REVIEWED > 0 and exited 0 into a push
# (roadbuddy 20260822-140937): a safety net that counts attempts inherits
# the failure it was built to catch. A dry run sends nothing by design, so
# only the structural count applies to it.
if [ "$PACKS_REVIEWED" -eq 0 ]; then
  echo "" >&2
  echo "*** NO PACK WAS REVIEWED — this run examined nothing at all." >&2
  echo "*** This is NOT a clean review and must not be read as one." >&2
  echo "*** THE PUSH IS REFUSED (exit 7)." >&2
  cleanup
  exit 7
elif [ "$DRY" != 1 ] && [ "$PACKS_SCORED" -eq 0 ]; then
  echo "" >&2
  echo "*** NO PACK WAS SCORED — $PACKS_REVIEWED pack(s) were built and sent, but no reviewer completed." >&2
  echo "*** Nothing was examined. This is NOT a clean review and must not be read as one." >&2
  echo "*** THE PUSH IS REFUSED (exit 7)." >&2
  cleanup
  exit 7
fi

# ---- Advance the incremental marker -----------------------------------------
# The marker records COVERAGE (what a review actually saw), not the verdict.
# A review that fully covered its range advances it; the sha field records
# whether the range came back clean (bare sha) or carried advisory highs
# ("advisory-highs:<sha>"), so the read side can decide who may trust it — see
# there for why the mode rides in that field rather than an appended one. Each
# state below is a way the next run could otherwise start past code no review
# actually saw:
#   DRY=1        nothing was reviewed at all — a dry run must never look done
#   UNDETERMINED a pack could not be scored, so "0 highs" is not a real zero
#   UNREVIEWED   a path was never packed
#   CLIPPED      the specialists saw only part of the diff
# (UNDETERMINED/UNREVIEWED/CLIPPED are coverage gaps — crediting the reviewed
# portion and carrying the gap forward is a separate change, not done here.)
#   HIGH>0       ADVISORY (default): the range WAS fully reviewed, so record it
#                and tag the marker advisory-highs — freezing here would restore
#                full-history re-review in a mode that gates nothing. BLOCKING:
#                freeze, exactly as before, so the fix's own commit re-reviews.
if [ "$DRY" = 1 ]; then
  echo "dry run — incremental marker not advanced"
elif [ "$UNDETERMINED" = 1 ] || [ -n "$UNREVIEWED" ] || [ "$CLIPPED" = 1 ]; then
  echo "incremental marker not advanced (range not fully covered) — the next push re-reviews this range"
elif [ "$HIGH" -gt 0 ] && [ "${CRA_BLOCKING:-0}" = 1 ]; then
  echo "incremental marker not advanced (blocking mode, $HIGH high finding(s) unresolved) — the fix's own commit re-reviews this range"
elif [ "$HIGH" -gt 0 ]; then
  # Advisory mode, range fully covered, highs present: record coverage but
  # prefix the sha so a pre-change reader (and any reader that does not know
  # this token) rejects the marker rather than trusting it in blocking mode.
  printf 'advisory-highs:%s %s %s %s\n' "$HEAD_SHA" "$BASE" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STAMP" > "$MARKER_FILE"
  echo "incremental marker advanced to $HEAD_SHORT past $HIGH advisory high finding(s) — they stay in this run's report and are NOT re-reported by later advisory pushes; fix them or run CRA_BLOCKING=1 / CRA_FULL_REVIEW=1 to re-examine"
else
  # Clean: bare-sha form, trusted by every script version.
  printf '%s %s %s %s\n' "$HEAD_SHA" "$BASE" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STAMP" > "$MARKER_FILE"
  echo "incremental marker advanced to $HEAD_SHORT — the next push reviews only what follows it"
fi
cleanup
if [ "${CRA_BLOCKING:-0}" = 1 ]; then
  # Block on findings, on any pack that could not be scored, and on any path that
  # was never reviewed. Only a complete, clean review lets the push through.
  if [ "$HIGH" -gt 0 ] || [ "$UNDETERMINED" = 1 ] || [ -n "$UNREVIEWED" ]; then
    exit 2
  fi
fi
exit 0
