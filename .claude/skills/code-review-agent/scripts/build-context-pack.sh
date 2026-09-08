#!/usr/bin/env bash
# Deterministic context pack builder. No API calls, no index, no deps beyond
# bash + git + ripgrep + python3 (pack_sections.py does the byte-precise
# truncation). Same repo state -> byte-identical pack.
# Exit codes: 0 ok · 64 usage · 65 change too large to pack honestly — the
#             per-file excerpt floor cannot be met (CRA_ALLOW_STARVED=1
#             forces a pack), diff coverage would fall below
#             CRA_DIFF_FLOOR_PCT % (default 50; CRA_ALLOW_CLIPPED_DIFF=1
#             forces), or the ticket cannot be shown whole
#             (CRA_ALLOW_CLIPPED_TICKET=1 forces). Rerun with --paths per
#             the stderr split plan.
set -eu
# pipefail intentionally omitted: section pipelines tolerate SIGPIPE producers

REPO="."; DIFF_MODE="branch"; BASE="main"; TICKET=""; TICKET_SHOW=""; OUT="context-pack.md"; PATHS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --diff-mode) DIFF_MODE="$2"; shift 2 ;;   # staged | branch
    --base) BASE="$2"; shift 2 ;;
    --ticket) TICKET="$2"; TICKET_SHOW="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --paths) PATHS="$2"; shift 2 ;;           # space-separated git pathspecs; scopes the whole pack
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
case "$OUT" in /*) : ;; *) OUT="$(pwd)/$OUT" ;; esac
case "$TICKET" in ""|/*) : ;; *) TICKET="$(pwd)/$TICKET" ;; esac
HELPER="$(cd "$(dirname "$0")" && pwd)/pack_sections.py"
cd "$REPO"
REPO_LOGICAL="$(pwd)"; REPO_REAL="$(pwd -P)"

# The containment rule for every file the pack inlines BY NAME (the readers
# are enumerated once, above guard_inline below; the harness's range-ticket
# reads go through ticket_blob for the same reason), the same rule
# pack_sections.py applies to the excerpts: a
# symlink leaf is never followed (a branch under review can commit one under
# docs/tickets or .claude/review-rules pointing at .env), and a path inside
# the repo must also RESOLVE inside it (a symlinked parent directory is local
# tampering — git never records a path through one). A file handed in from
# outside the repo — the hook's scratch bundle — passes on the leaf rule
# alone. The excerpt guard alone left three read paths open (roadbuddy
# 20260822-140937): a hardening fix applied to one branch of four is a
# quarter of a fix.
contained() {  # <path> — 0 when the file may be inlined
  [ -f "$1" ] || return 1
  [ -L "$1" ] && return 1
  local logical real
  logical="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1")"
  real="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1")"
  case "$logical" in
    "$REPO_LOGICAL"/*|"$REPO_REAL"/*)
      case "$real" in "$REPO_REAL"/*) return 0 ;; *) return 1 ;; esac ;;
    *) return 0 ;;
  esac
}
# The by-name readers — ticket, review rules, CLAUDE.md — all go through this
# one call, and reach it the same way: anything that EXISTS OR IS A LINK is
# offered (a dangling symlink must be refused with its reason, not fall
# through as "missing"). The first cut routed two readers of four and gated
# them differently (review 20260823-124523): a guard is complete only when
# every reader of its class is enumerated behind it.
guard_inline() {  # <path> <label> — 0 when the file may be inlined; else says why on stderr
  if contained "$1"; then return 0; fi
  echo "$2 refused: a symlink (possibly dangling), or it resolves outside the repo — NOT inlined" >&2
  return 1
}
offered() { [ -e "$1" ] || [ -L "$1" ]; }
TICKET_NOTE=""
if [ -n "$TICKET" ] && offered "$TICKET" && ! guard_inline "$TICKET" "ticket $TICKET_SHOW"; then
  TICKET_NOTE="_Ticket ${TICKET_SHOW} NOT inlined: it is a symlink (possibly dangling), or resolves outside the repo. Report the missing task-level context as a finding._"
  TICKET=""
fi
CLAUDE_OK=0; CLAUDE_NOTE=""
if offered CLAUDE.md; then
  if guard_inline CLAUDE.md "CLAUDE.md"; then CLAUDE_OK=1
  else CLAUDE_NOTE="_CLAUDE.md NOT inlined: it is a symlink (possibly dangling), or resolves outside the repo._"; fi
fi

# Initial per-section budgets (bytes). These are floors of intent, not walls:
# after each section's demand is measured, unused budget pools and flows to
# starved sections (files first). B_RULES raised 6144 -> 20480 and the whole
# model reworked after the 2026-08-01/03 truncation defects: fixed budgets
# starved section 4 to ~250 B/file on 40-file batches and silently dropped
# the alphabetically-last files entirely.
B_DIFF=12288; B_TICKET=4096; B_RULES=20480; B_FILES=10240
# Largest total diff a single directory group may carry before the split
# plan descends a level. Half the cap: the other half is ticket, rules and
# per-file excerpts, so a group at this size still packs with room to spare.
GROUP_MAX=$(( ${CRA_PACK_CAP:-131072} / 2 ))
B_DEPS=5120; B_TESTS=3072
CAP="${CRA_PACK_CAP:-131072}"   # hard total cap; env-overridable for tests
[ "$CAP" -ge 1024 ] || { echo "CRA_PACK_CAP=$CAP is unusably small (min 1024)" >&2; exit 64; }
DIFF_FLOOR_PCT="${CRA_DIFF_FLOOR_PCT:-50}"  # min % of the diff that must fit; 0 disables
case "$DIFF_FLOOR_PCT" in
  *[!0-9]*|'')  # validated here like the cap: bad config is exit 64 with a
                # message, never an undocumented set -e abort mid-allocation
    echo "CRA_DIFF_FLOOR_PCT must be a non-negative integer percent, got '${CRA_DIFF_FLOOR_PCT-}'" >&2
    exit 64 ;;
esac
if [ "$CAP" -lt 56064 ]; then
  echo "warning: cap $CAP is below the section budgets (56,064) — the final hard clip will do the truncating" >&2
fi
BUNDLE_FILES="${CRA_BUNDLE_FILES:-4}"   # files per bundle in the split plan's fallback
case "$BUNDLE_FILES" in
  ''|*[!0-9]*)  # validated here like the others: bad config is exit 64 with
                # a message, never an arithmetic abort in the middle of a plan
    echo "CRA_BUNDLE_FILES must be a positive integer, got '${CRA_BUNDLE_FILES-}'" >&2
    exit 64 ;;
esac
# The bound is checked arithmetically, like the cap: "00" is zero too.
[ "$BUNDLE_FILES" -ge 1 ] || { echo "CRA_BUNDLE_FILES must be a positive integer, got '${CRA_BUNDLE_FILES-}'" >&2; exit 64; }
OVERHEAD=768                    # pack skeleton + marker slack reserved off CAP
FLOOR_FILE=1536                 # min useful excerpt per changed file
CEIL_FILE=4096                  # max excerpt per changed file

if command -v rg >/dev/null 2>&1; then HAVE_RG=1; else HAVE_RG=0; fi

refs_search() {
  # Files referencing $1, excluding VCS/dep dirs. rg globs match paths;
  # grep --include matches basenames — accepted v1 divergence.
  if [ "$HAVE_RG" = 1 ]; then
    rg -l --glob '!node_modules' --glob '!.git' -e "$1" . 2>/dev/null
  else
    grep -rl --exclude-dir=node_modules --exclude-dir=.git -e "$1" . 2>/dev/null
  fi
}

tests_search() {
  if [ "$HAVE_RG" = 1 ]; then
    rg -l --glob '*test*' --glob '*spec*' -e "$1" . 2>/dev/null
  else
    grep -rl --include='*test*' --include='*spec*' --exclude-dir=node_modules --exclude-dir=.git -e "$1" . 2>/dev/null
  fi
}

get_diff() {
  # $PATHS expands unquoted by design: space-separated pathspecs.
  # quotePath=false keeps non-ASCII paths raw so pack_sections.py and the
  # FILES list agree on them.
  if [ "$DIFF_MODE" = "staged" ]; then
    if [ -n "$PATHS" ]; then git -c core.quotePath=false diff --cached -- $PATHS; else git -c core.quotePath=false diff --cached; fi
  else
    MB="$(git merge-base "$BASE" HEAD)"
    if [ -n "$PATHS" ]; then git -c core.quotePath=false diff "$MB"..HEAD -- $PATHS; else git -c core.quotePath=false diff "$MB"..HEAD; fi
  fi
}

balance_fences() {
  # Guarantee a trailing newline and an even number of ``` fence lines,
  # appending a closing fence when a clip cut one off. Only the final
  # hard-cap clip still needs this; sections close their own fences.
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  printf '\n' >> "$tmp"
  if [ $(( $(grep -c '^```' "$tmp" || true) % 2 )) -eq 1 ]; then
    echo '```' >> "$tmp"
  fi
  cat "$tmp"
  rm -f "$tmp"
}

# ---- Stage 1: render raw section bodies once -------------------------------
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

get_diff > "$SCRATCH/diff.raw"
FILES="$(grep '^+++ b/' "$SCRATCH/diff.raw" | sed -e 's|^+++ b/||' -e 's/\t$//' | LC_ALL=C sort -u || true)"

RULES_TXT="$(
  for f in .claude/review-rules/*.md; do
    offered "$f" || continue
    if guard_inline "$f" "rules file $f"; then cat "$f"; echo
    else echo "_${f} NOT inlined: it is a symlink (possibly dangling), or resolves outside the repo._"
    fi
  done
)"
RB="$(printf '%s' "$RULES_TXT" | wc -c)"

# Fail fast if a specialist's yardstick never made it into the pack. A
# specialist whose reference material is missing cannot review anything, but it
# still returns a well-formed empty result, so the run looks complete and clean.
# Discovering that after an hour of review wastes the run; refuse before
# dispatching. Each entry is "<substring the pack must contain>|<why>".
REQUIRED_RULE_MARKERS="${CRA_REQUIRED_RULE_MARKERS:-Mimiry API contract|the api-contract specialist has no yardstick without it}"
if [ -n "$REQUIRED_RULE_MARKERS" ]; then
  _missing=0
  while IFS='|' read -r _marker _why; do
    [ -n "$_marker" ] || continue
    case "$RULES_TXT" in
      *"$_marker"*) ;;
      *)
        echo "build-context-pack: required rules content missing from the pack: '$_marker'" >&2
        echo "  reason: $_why" >&2
        _missing=1
        ;;
    esac
  done <<EOF
$(printf '%s\n' "$REQUIRED_RULE_MARKERS")
EOF
  if [ "$_missing" = 1 ]; then
    echo "  The pack inlines CLAUDE.md and .claude/review-rules/*.md only." >&2
    echo "  Regenerate the digest, or set CRA_REQUIRED_RULE_MARKERS='' to override." >&2
    exit 9
  fi
fi

CLAUDE_B=0
if [ "$CLAUDE_OK" = 1 ]; then CLAUDE_B="$(wc -c < CLAUDE.md)"
elif [ -n "$CLAUDE_NOTE" ]; then CLAUDE_B=$(( $(printf '%s' "$CLAUDE_NOTE" | wc -c) + 2 )); fi   # bytes, like wc -c above

printf '%s\n' "$FILES" | while IFS= read -r f; do
  stem="$(basename "$f")"; stem="${stem%.*}"
  [ -n "$stem" ] || continue
  refs_search "$stem" \
    | sed 's|^\./||' | grep -vF "$f" | LC_ALL=C sort -u | head -10 \
    | sed "s|^|- references ${stem}: |" || true
done > "$SCRATCH/deps.raw"

{
  printf '%s\n' "$FILES" | while IFS= read -r f; do
    stem="$(basename "$f")"; stem="${stem%.*}"
    [ -n "$stem" ] || continue
    tests_search "$stem" \
      | sed 's|^\./||' || true
  done | LC_ALL=C sort -u | sed 's|^|- test: |'
  echo "Recent commits on touched files:"
  printf '%s\n' "$FILES" | xargs -r git log --oneline -5 -- 2>/dev/null || true
} > "$SCRATCH/tests.raw"

# ---- Stage 2: measure demands ----------------------------------------------
D_DIFF=$(( $(wc -c < "$SCRATCH/diff.raw") + 16 ))   # +fence overhead
if [ -n "$TICKET" ] && [ -f "$TICKET" ]; then D_TICKET="$(wc -c < "$TICKET")"; else D_TICKET=80; fi
D_RULES=$(( RB + 2 + CLAUDE_B ))
# plain assignment so a helper failure aborts under set -e instead of
# silently leaving the demands empty
MEASURE="$(python3 "$HELPER" files-measure --repo . --diff "$SCRATCH/diff.raw" \
    --floor "$FLOOR_FILE" --ceil "$CEIL_FILE")"
read -r D_FILES MIN4 D_IDEAL <<< "$MEASURE"
D_DEPS="$(wc -c < "$SCRATCH/deps.raw")"
D_TESTS="$(wc -c < "$SCRATCH/tests.raw")"

# ---- Stage 3: allocate — clamp to initial budgets, then pool the slack -----
# alloc_i = min(demand_i, B_i); surplus flows in priority order:
#   1. files up to MIN4 (a floor-quality excerpt for EVERY changed file)
#   2. rules to full demand (review-rules + CLAUDE.md complete)
#   3. diff to full demand
#   4. ticket to full demand — after rules and diff so a bloated ticket can
#      never crowd out the code under review, and with no ceiling of its own:
#      it competes only for surplus the higher-priority sections declined,
#      and a ticket that still cannot be shown whole refuses the pack in
#      Stage 4 instead of clipping quietly (2026-08-17/18: criteria 14-23 of
#      a 9,557 B ticket invisible on two consecutive runs while ~84 KB of
#      cap sat unused)
#   5. files toward per-file ceilings
#   6. dependents, then tests/history
min() { if [ "$1" -lt "$2" ]; then echo "$1"; else echo "$2"; fi; }

A_DIFF="$(min "$D_DIFF" "$B_DIFF")"
A_TICKET="$(min "$D_TICKET" "$B_TICKET")"
A_RULES="$(min "$D_RULES" "$B_RULES")"
A_FILES="$(min "$D_FILES" "$B_FILES")"
A_DEPS="$(min "$D_DEPS" "$B_DEPS")"
A_TESTS="$(min "$D_TESTS" "$B_TESTS")"

POOL=$(( CAP - OVERHEAD - A_DIFF - A_TICKET - A_RULES - A_FILES - A_DEPS - A_TESTS ))
if [ "$POOL" -lt 0 ]; then
  # Cap below the initial budgets: shrink sections in reverse priority so
  # every section still ends with a coherent, marker-announced clip instead
  # of the blunt tail cut. Files never shrink below the per-file floor;
  # any residue is left to the final hard clip.
  DEFICIT=$(( -POOL )); POOL=0
  shrink() {  # lower alloc variable $1 toward minimum $2, reducing DEFICIT
    local cur="${!1}" room take
    room=$(( cur - $2 )); [ "$room" -gt 0 ] || room=0
    take=$(( DEFICIT < room ? DEFICIT : room ))
    DEFICIT=$(( DEFICIT - take ))
    printf -v "$1" '%d' $(( cur - take ))
  }
  shrink A_TESTS 0
  shrink A_DEPS 0
  shrink A_DIFF 0
  shrink A_RULES 0
  shrink A_TICKET 0
  shrink A_FILES "$MIN4"
fi

grant() {  # raise alloc variable $1 toward target $2, draining POOL
  # (assigns via printf -v: a $(grant ...) subshell would lose the POOL update)
  local cur="${!1}" want g
  want=$(( $2 - cur )); [ "$want" -gt 0 ] || want=0
  g=$(( want < POOL ? want : POOL ))
  POOL=$(( POOL - g ))
  printf -v "$1" '%d' $(( cur + g ))
}

grant A_FILES "$MIN4"
grant A_RULES "$D_RULES"
grant A_DIFF  "$D_DIFF"
grant A_TICKET "$D_TICKET"
grant A_FILES "$D_FILES"
grant A_DEPS  "$D_DEPS"
grant A_TESTS "$D_TESTS"
# leftover pool deepens file excerpts past the per-file ceiling rather than
# going unused (the ceiling is a fairness device, not a wall)
grant A_FILES "$D_IDEAL"

# ---- Stage 4: refuse to build a silently-crippled pack ----------------------
# Per-file diff sizes ("bytes<TAB>path", b/-side of each `diff --git` header),
# driving the split suggestions of every refusal guard below. A path containing " b/"
# or spaces would mis-parse — accepted v1 divergence, same as the unquoted
# $PATHS expansion in get_diff.
awk '
  /^diff --git a\// {
    if (path != "") print bytes "\t" path
    bytes = 0
    path = substr($0, index($0, " b/") + 3)
  }
  { bytes += length($0) + 1 }
  END { if (path != "") print bytes "\t" path }
' "$SCRATCH/diff.raw" > "$SCRATCH/perfile.bytes"

print_split_plan() {
  # Ready-made --paths suggestions for a batched rerun. A file whose own diff
  # exceeds B_DIFF gets a solo pack — packed alone it gets nearly the whole
  # pool, so batching terminates in a real review instead of an INCOMPLETE
  # entry. The rest group by directory at the shallowest depth whose whole
  # group still fits one pack (see below), minus the solo files via :(exclude)
  # pathspecs (the pre-push hook passes each quoted suggestion through as one
  # --paths value; the builder expands it into pathspecs).
  #
  # Only names drawn from a conservative allowlist ([A-Za-z0-9._/@+=-], not
  # starting with "-" or ":") may be written into a --paths spec: filenames
  # are contributor-controlled, the hook scrapes specs from between quotes,
  # $PATHS expands word-split, and the suggestions exist to be pasted into a
  # shell — a denylist that stopped at whitespace/quotes still let $,
  # backtick and backslash through inside the double quotes, i.e. command
  # substitution on paste (review 20260818-221807). Unsafe names are
  # announced instead of emitted; they stay implicitly covered by their
  # directory group, and a group still too big for one pack refuses again —
  # loud (INCOMPLETE), never silent.
  # A suggestion must be strictly NARROWER than the scope that just failed.
  # Without this, a pack already scoped to `src/i18n` was told to retry with
  # `--paths "src"` — wider than what it was already doing — so the hook could
  # not make progress and recorded the whole group as INCOMPLETE and never
  # reviewed (roadbuddy 2026-08-20, the Kørelærer terminology sweep: 17 locale
  # files, unreviewable because two tickets left only 4096 B for a 16528 B
  # ticket). The current scope's depth therefore sets the FLOOR for the
  # directory search.
  # The scope is parsed into its positive terms and its :(exclude) terms. The
  # first positive term sets the depth FLOOR; the exclusions are carried into
  # every directory-group suggestion, because a group re-run on its own would
  # otherwise pull the excluded files straight back in — on roadbuddy
  # 2026-08-20 `docs :(exclude)docs/ARCH.md` was answered with `docs`, the
  # hook built that (wider) group, it refused, and its own plan suggested the
  # excluded form again: a cycle that exhausted the split depth while the
  # docs/tickets sub-groups it emitted on every lap were reviewed twice.
  SCOPE_DEPTH=0; SCOPE_EXCL=""; SCOPE_TERMS=0; SCOPE_NORM=""; SCOPE_EXCL_UNSAFE=0
  if [ -n "$PATHS" ]; then
    for _spec in $PATHS; do
      case "$_spec" in
        :\(exclude\)*)
          # Echoed into every group suggestion, so it passes the same allowlist
          # as every emitted name. An unsafe one cannot be carried, and a group
          # WITHOUT it would re-cover the excluded file — so directory groups
          # are withheld and the files go to bundles, which carry no exclusions.
          case "${_spec#:(exclude)}" in
            ''|-*|*[!A-Za-z0-9._/@+=-]*)
              SCOPE_EXCL_UNSAFE=1
              echo "  # cannot batch by name (unsafe exclusion in scope): directory groups withheld, files bundled" ;;
            *) SCOPE_EXCL="$SCOPE_EXCL $_spec" ;;
          esac ;;
        *)
          SCOPE_TERMS=$((SCOPE_TERMS + 1))
          if [ "$SCOPE_DEPTH" -eq 0 ]; then
            SCOPE_DEPTH=$(printf '%s' "${_spec%/}" | awk -F/ '{print NF}')
          fi ;;
      esac
      SCOPE_NORM="$SCOPE_NORM${SCOPE_NORM:+ }$_spec"
    done
    unset _spec
  fi
  # A multi-file bundle that refused is split in half, so bundling terminates
  # at one file per pack instead of re-suggesting the same bundle forever.
  _maxfiles="$BUNDLE_FILES"
  if [ "$SCOPE_TERMS" -gt 1 ] && [ $(( (SCOPE_TERMS + 1) / 2 )) -lt "$_maxfiles" ]; then
    _maxfiles=$(( (SCOPE_TERMS + 1) / 2 ))
  fi
  _plan="$( {
    awk -F'\t' -v big="$B_DIFF" '
      function unsafe(p) { return (p !~ /^[A-Za-z0-9._\/@+=-]+$/ || p ~ /^[-:]/) }
      $1 > big && !unsafe($2) \
        { printf "  --paths \"%s\"   # %s diff bytes\n", $2, $1 }
      $1 > big &&  unsafe($2) \
        { printf "  # cannot batch by name (unsafe path): %s — its directory group keeps it and may refuse again\n", $2 }
    ' "$SCRATCH/perfile.bytes"
    # Directory groups, chosen at the SHALLOWEST depth whose whole group still
    # fits one pack. Grouping only by top-level directory made a branch whose
    # work sits under one directory unreviewable: on roadbuddy 2026-08-18, `src`
    # was 438 KB against a 128 KB cap, so the single `src` group refused as
    # INCOMPLETE and ~50 files — the entire change — were never reviewed while
    # the run still exited 0. Descending a level splits that into
    # src/components/ui, src/components/calendar, ... each of which fits.
    #
    # A group that is still too big at its deepest directory is emitted anyway
    # and refuses loudly at build time, exactly as before — deeper is a better
    # attempt, never a silent drop.
    #
    # Two rules keep the plan strictly narrowing and its groups disjoint:
    #   * a file with no directory below the scope floor (it sits directly in
    #     the scope root) is never expressed as the root directory — that is
    #     the scope itself, or wider. It goes to a file bundle (tag B).
    #   * a group is emitted only if it is CLOSED: every non-solo file under it
    #     is assigned to it. A non-fitting fallback group whose subtree also
    #     holds deeper emitted groups is LEAKY — re-running it would re-cover
    #     those groups — so its files go to bundles instead.
    # The awk prints G-tagged suggestion lines and B-tagged bundle candidates;
    # the shell splits them apart below.
    awk -F'\t' -v big="$B_DIFF" -v groupmax="$GROUP_MAX" -v mindepth="$(( SCOPE_DEPTH + 1 ))" \
        -v scope_excl="$SCOPE_EXCL" -v excl_unsafe="$SCOPE_EXCL_UNSAFE" '
      function unsafe(p) { return (p !~ /^[A-Za-z0-9._\/@+=-]+$/ || p ~ /^[-:]/) }
      function prefix(path, d,   n, parts, i, out) {
        n = split(path, parts, "/")
        if (d >= n) d = n - 1          # never descend to the file itself
        if (d < 1) d = 1
        out = parts[1]
        for (i = 2; i <= d; i++) out = out "/" parts[i]
        return out
      }
      { bytes[NR] = $1; path[NR] = $2
        if ($1 > big && !unsafe($2)) solo[$2] = 1
        n = split($2, parts, "/"); depth[NR] = n - 1 }
      END {
        if (mindepth < 1) mindepth = 1
        for (i = 1; i <= NR; i++) {
          if (solo[path[i]]) continue
          for (d = mindepth; d <= depth[i]; d++)
            total[d, prefix(path[i], d)] += bytes[i]
        }
        for (i = 1; i <= NR; i++) {
          p = path[i]
          if (solo[p]) continue
          if (depth[i] < mindepth) { shallow[i] = 1; continue }
          chosen = ""; cd = 0
          for (d = mindepth; d <= depth[i]; d++) {
            g = prefix(p, d)
            chosen = g; cd = d          # keep descending; deepest wins if none fit
            if (total[d, g] <= groupmax) break
          }
          group[i] = chosen
          count[chosen]++; gd[chosen] = cd
        }
        # leak check: a file under g that is not assigned to g makes g leaky
        for (i = 1; i <= NR; i++) {
          if (solo[path[i]] || shallow[i]) continue
          for (g in count) {
            if (group[i] == g) continue
            if (gd[g] <= depth[i] && prefix(path[i], gd[g]) == g) leaky[g] = 1
          }
        }
        for (i = 1; i <= NR; i++) {
          p = path[i]
          if (solo[p]) continue
          if (shallow[i] || leaky[group[i]] || excl_unsafe) printf "B\t%s\t%s\n", bytes[i], p
        }
        # solo files are excluded from whichever emitted group would hold them
        for (i = 1; i <= NR; i++) {
          p = path[i]
          if (!solo[p]) continue
          for (d = mindepth; d <= depth[i]; d++) {
            g = prefix(p, d)
            # `g in count`, NOT `count[g] > 0`: referencing an element creates
            # it in awk, which invented empty "src" and "src/components" groups
            # — and an empty group means --paths "src", i.e. the unsplittable
            # group this change exists to remove.
            if ((g in count) && !leaky[g]) { excl[g] = excl[g] " :(exclude)" p; break }
          }
        }
        for (g in count) {
          if (leaky[g] || excl_unsafe) continue
          if (unsafe(g))
            printf "G\t  # cannot batch by name (unsafe directory): %s\n", g
          else
            printf "G\t  --paths \"%s%s%s\"   # %s files\n", g, excl[g], scope_excl, count[g]
        }
      }
    ' "$SCRATCH/perfile.bytes" > "$SCRATCH/plan.tagged"
    sed -n 's/^G\t//p' "$SCRATCH/plan.tagged"
    sed -n 's/^B\t//p' "$SCRATCH/plan.tagged" > "$SCRATCH/perfile.bundle"
    print_file_bundles "$SCRATCH/perfile.bundle" "$_maxfiles"
  } | filter_plan "$SCOPE_NORM" | LC_ALL=C sort )"
  # No usable suggestion means no way forward: the hook would record the group
  # as INCOMPLETE and never review it. Fall back to file bundles over every
  # changed file, which always narrow — except for a single-file scope, which
  # the filter leaves empty on purpose: a file that will not pack alone has
  # nowhere narrower to go, and the hook must say so rather than loop.
  case "$_plan" in
    *"  --paths "*) : ;;
    *) _plan="$(print_file_bundles "$SCRATCH/perfile.bytes" "$_maxfiles" \
          | filter_plan "$SCOPE_NORM" | LC_ALL=C sort)" ;;
  esac
  echo "Split the review into path-scoped packs, e.g.:"
  if [ -n "$_plan" ]; then
    printf '%s\n' "$_plan"
  else
    # A bare header over nothing reads as a planner failure (roadbuddy
    # 20260821-174644). Say what it is: a scope with nothing narrower in it.
    echo "  # nothing narrower to suggest for this scope (a single file, or names that cannot be emitted):"
    echo "  #   CRA_ALLOW_CLIPPED_TICKET=1 / CRA_ALLOW_CLIPPED_DIFF=1 force a degraded pack with an in-band marker"
  fi
  unset _plan _maxfiles
  return 0
}

# Files bundled into byte- and COUNT-bounded groups, as multi-file --paths
# specs. This is the fallback for a scope with no narrower directory inside it:
# `src/i18n` holds 17 locale files and nothing between them, so the directory
# search has nowhere to descend and emits no usable suggestion at all — which
# the hook records as "could not be packed even alone", i.e. never reviewed.
# Bundling always terminates: worst case it reaches one file per pack.
#
# Bounded by COUNT as well as bytes on purpose. The excerpt budget scales with
# the NUMBER of files, so a group of small-diff, large-file locale JSONs can sit
# far under the byte bound and still starve the ticket out of the pack — which
# is exactly how the failure presented (16528 B of ticket, 4096 B left for it).
print_file_bundles() {  # print_file_bundles <perfile-table> <maxfiles>
  awk -F'\t' -v groupmax="$GROUP_MAX" -v maxfiles="$2" '
    function unsafe(p) { return (p !~ /^[A-Za-z0-9._\/@+=-]+$/ || p ~ /^[-:]/) }
    function flush(   k, spec) {
      if (n == 0) return
      spec = ""
      for (k = 1; k <= n; k++) spec = spec (k == 1 ? "" : " ") bundle[k]
      printf "  --paths \"%s\"   # %s file(s)\n", spec, n
      n = 0; sum = 0
    }
    {
      if (unsafe($2)) { printf "  # cannot batch by name (unsafe path): %s\n", $2; next }
      if (n > 0 && (n >= maxfiles || sum + $1 > groupmax)) flush()
      bundle[++n] = $2; sum += $1
    }
    END { flush() }
  ' "$1"
}

# The last word on every plan: a suggestion equal to the scope that just failed
# is dropped (it could only fail again), and a spec that appears twice is kept
# once. Comparison is on the normalized spec between the quotes, so
# `docs  :(exclude)x` and `docs :(exclude)x` are the same suggestion.
filter_plan() {  # filter_plan <normalized-scope>  (stdin: plan lines)
  awk -v scope="$1" '
    {
      if (match($0, /^  --paths "[^"]*"/)) {
        spec = substr($0, 12, RLENGTH - 12)
        gsub(/[ \t]+/, " ", spec); sub(/^ /, "", spec); sub(/ $/, "", spec)
        if (spec == scope) next
        if (seen[spec]++) next
      }
      print
    }'
}

if [ "$A_FILES" -lt "$MIN4" ] && [ "${CRA_ALLOW_STARVED:-0}" != 1 ]; then
  {
    echo "context pack NOT built: $(printf '%s\n' "$FILES" | grep -c .) changed files need $MIN4 B for floor-quality excerpts, only $A_FILES B available under cap $CAP."
    print_split_plan
    echo "Or force a starved pack with CRA_ALLOW_STARVED=1 (every file present but far below floor quality)."
  } >&2
  exit 65
fi

# A pack whose diff is mostly missing reviews a fraction of the change while
# reporting success (2026-08-17: a 187 KB few-file diff shipped at 15%
# coverage). The file-floor guard above cannot see this — it counts files,
# not diff bytes — so the diff has its own floor. Orthogonal to
# CRA_ALLOW_STARVED: forcing a pack that is both starved and clipped takes
# both env vars, deliberately.
if [ "$DIFF_FLOOR_PCT" -gt 0 ] && [ "$D_DIFF" -gt 0 ] \
   && [ $(( A_DIFF * 100 )) -lt $(( D_DIFF * DIFF_FLOOR_PCT )) ] \
   && [ "${CRA_ALLOW_CLIPPED_DIFF:-0}" != 1 ]; then
  {
    echo "context pack NOT built: the diff needs $D_DIFF B but only $A_DIFF B fit under cap $CAP — $(( A_DIFF * 100 / D_DIFF ))% coverage, below the ${DIFF_FLOOR_PCT}% floor (CRA_DIFF_FLOOR_PCT)."
    DOM="$(awk -F'\t' -v tot="$D_DIFF" \
      '$1 * 2 > tot { print $2 " carries " $1 " of " tot " diff bytes"; exit }' \
      "$SCRATCH/perfile.bytes")"
    if [ -n "$DOM" ]; then
      echo "One file dominates this diff: $DOM — review it in its own pack."
    fi
    print_split_plan
    echo "Or force a clipped pack with CRA_ALLOW_CLIPPED_DIFF=1 (most of the diff will be invisible to every specialist)."
  } >&2
  exit 65
fi

# The ticket is the checklist the whole review verifies against: a criterion
# the pack cannot show is not "partially covered", it is unassessed while the
# report still reads as a success (2026-08-17/18: two consecutive runs noted
# the clip in-band and nothing failed, so it was recorded and forgotten). The
# floor is therefore the whole ticket — no percentage knob. Splitting with
# --paths frees pool for it; a ticket too big for any pack is one to trim.
if [ -n "$TICKET" ] && [ -f "$TICKET" ] && [ "$A_TICKET" -lt "$D_TICKET" ] \
   && [ "${CRA_ALLOW_CLIPPED_TICKET:-0}" != 1 ]; then
  {
    echo "context pack NOT built: the ticket needs $D_TICKET B but only $A_TICKET B fit under cap $CAP — acceptance criteria past the clip would go unassessed while the review reports success."
    print_split_plan
    echo "Or force a clipped pack with CRA_ALLOW_CLIPPED_TICKET=1 (criteria past the clip will be invisible to every specialist)."
  } >&2
  exit 65
fi
if [ "$RB" -gt "$A_RULES" ]; then
  echo "warning: review rules alone ($RB B) exceed the rules allocation ($A_RULES B); pack may hit the hard cap" >&2
fi

# ---- Stage 5: emit ----------------------------------------------------------
STARVED_FLAG=""
[ "$A_FILES" -lt "$MIN4" ] && STARVED_FLAG="--starved-ok"

{
  echo "# Context Pack"
  echo
  echo "## 1. Diff"
  python3 "$HELPER" clip --budget "$A_DIFF" --label diff --fence diff < "$SCRATCH/diff.raw"
  echo
  echo "## 2. Ticket"
  if [ -n "$TICKET" ] && [ -f "$TICKET" ]; then
    python3 "$HELPER" clip --budget "$A_TICKET" --label ticket < "$TICKET"
    printf '\n'
  elif [ -n "$TICKET_NOTE" ]; then
    echo "$TICKET_NOTE"
  else
    echo "_No ticket found — report the missing task-level context as a finding._"
  fi
  echo
  echo "## 3. Engineering rules"
  # Owner-curated review-rules are never clipped; CLAUDE.md gets what remains
  # of the allocation, cut at heading boundaries with the omitted headings
  # named (pilot 2026-08-01: silent tail-clipping here cost every pack the
  # code-conventions block and produced 34 duplicate findings).
  if [ -n "$RULES_TXT" ]; then printf '%s\n\n' "$RULES_TXT"; fi
  CB=$(( A_RULES - RB - 2 )); [ "$CB" -gt 0 ] || CB=0
  if [ "$CLAUDE_OK" = 1 ]; then
    python3 "$HELPER" clip-md --budget "$CB" --label CLAUDE.md < CLAUDE.md
  elif [ -n "$CLAUDE_NOTE" ]; then
    printf '%s\n' "$CLAUDE_NOTE"
  fi
  echo
  echo "## 4. Changed files (current state)"
  # Hunk-centered excerpts, one block per changed file, floor+ceiling per
  # file, waterfill allocation — never an even split, never a silent drop.
  python3 "$HELPER" files-emit --repo . --diff "$SCRATCH/diff.raw" \
    --floor "$FLOOR_FILE" --ceil "$CEIL_FILE" --budget "$A_FILES" $STARVED_FLAG
  echo
  echo "## 5. Dependents"
  python3 "$HELPER" clip --budget "$A_DEPS" --label dependents < "$SCRATCH/deps.raw"
  echo
  echo "## 6. Related tests and history"
  python3 "$HELPER" clip --budget "$A_TESTS" --label "tests+history" < "$SCRATCH/tests.raw"
  echo
} > "$OUT.tmp"

MARKER=$'\n\n_[pack truncated at cap]_\n'
SIZE="$(wc -c < "$OUT.tmp")"
if [ "$SIZE" -gt "$CAP" ]; then
  head -c $(( CAP - ${#MARKER} - 5 )) "$OUT.tmp" | balance_fences > "$OUT"
  printf '%s' "$MARKER" >> "$OUT"
else
  cat "$OUT.tmp" > "$OUT"
fi
rm -f "$OUT.tmp"
echo "context pack written: $OUT ($(wc -c < "$OUT") bytes)" >&2
