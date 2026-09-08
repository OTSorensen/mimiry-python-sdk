# Vendored skill provenance

`.claude/skills/code-review-agent/` is a **copy** of the `code-review-agent`
repo, not a submodule or a symlink. This file records where the copy came from
and what was deliberately changed after copying, so a future session can answer
two questions without guessing:

1. Is this copy stale relative to upstream?
2. Which differences are intentional, and which are drift?

Without this record, a local roster change is indistinguishable from an
out-of-date copy, and the safe-looking fix (re-copy from upstream) silently
reverts it.

## Source

| | |
|---|---|
| Upstream repo | `~/projects/code-review-agent` (`github.com/OTSorensen/code-review-agent`) |
| Copied from commit | `a761262a870a7da721d12ff035e733ce547ccaa4` |
| Commit date | 2026-08-23 |
| Subject | The guard's comment points at the one enumeration of its readers |
| Copied on | 2026-09-04 |

## Intentional divergences from upstream

These are **not** drift. Reapply every one of them after any re-copy.

1. **`references/supabase-rls-agent.md` is not installed.** This repo has no
   SQL, no database, and no Supabase. An always-empty specialist is not free:
   the merge rule promotes a finding to high confidence when ≥2 specialists
   agree, so a permanently silent third slot quietly weakens that signal to a
   two-specialist ensemble.

2. **`references/api-contract-agent.md` is added** (repo-local, no upstream
   equivalent). It checks every client method against the generated OpenAPI
   digest in `.claude/review-rules/api-contract.md`.

3. **`SKILL.md`** names `api-contract-agent.md` in the third dispatch slot
   (step 4), tags findings `["api-contract"]` (step 5), and says
   `api-contract` in the overview paragraph. **All three**: the overview was
   missed on the first pass and left the file contradicting itself, which a
   review caught.

5. **`references/report-format.md`** drops `supabase-rls` from the category
   enum.

The roster is named **only** in `SKILL.md` — no script reads it, and
`extract_findings.py` does not validate specialist names — so a swap is one
line plus a prompt file.

## Checking for staleness

```
# What upstream is now, vs what this copy was taken from:
git -C ~/projects/code-review-agent log --oneline a761262a870a7da721d12ff035e733ce547ccaa4..HEAD

# What actually differs (expect only the divergences listed above):
diff -r ~/projects/code-review-agent/skills/code-review-agent \
        .claude/skills/code-review-agent
```

Empty first output means the copy is current.

## Updating from upstream

1. Re-copy `scripts/` and the shared `references/` files.
2. Reapply divergences 1–5 above.
3. Update the commit hash and date in this file.
4. Run a review and confirm the report header still says
   `Execution mode: parallel-specialists` — a roster edit that leaves a prompt
   file missing degrades to inline passes, which is legitimate but destroys the
   independence the ≥2-specialist rule depends on.
