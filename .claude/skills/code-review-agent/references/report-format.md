# Review report format

Structure, in order:

1. **Header** — repo, branch vs base, changed-lines count, ticket id (or "none"),
   context-pack size in bytes, and the execution mode as the exact token
   `Execution mode: parallel-specialists` or `Execution mode: inline-sequential`
   (when inline, add one clause on why — the harness greps for this token).
2. **High risk — human decision required** — findings, most severe first.
3. **Attention suggested** — findings.
4. **Safe to auto-fix** — findings.
5. **Acceptance criteria** — one line per criterion from the ticket:
   met / not met / unclear, with a one-sentence justification. Omit if no ticket.
6. **Candidate patterns** — generalizable lessons worth promoting to
   `.claude/review-rules/` in the target repo.
7. **Q&A invitation** — one line: "Challenge any finding — ask for the evidence,
   the real-world impact, or a fix sketch."
8. **Machine-readable block** — the report's LAST element is exactly one fenced
   ```json block:

```json
{"findings": [{"file": "src/x.ts", "line": 42,
  "category": "security|pattern|correctness|testing|other",
  "severity": "high|medium|low", "confidence": "high|medium|low",
  "specialists": ["security"], "title": "...", "evidence": "...",
  "suggested_fix": "..."}],
 "acceptance_criteria": [{"criterion": "...", "status": "met|not-met|unclear"}],
 "candidate_patterns": [{"statement": "...", "rationale": "..."}]}
```

Human-section finding presentation: `file:line — [severity/confidence] title`,
then evidence, suggested fix, and contributing specialists on following lines.

Risk-tier mapping: severity high → section 2; medium → section 3; low → section 4.
