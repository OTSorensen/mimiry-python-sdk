# <TICKET-ID>: <one-line summary>

## Context

Why this work exists. The symptom, who hits it, and what it costs — not the
solution. If it came from a bug report, name the ID and the file it lives in.

## Goal

One paragraph: what the change must achieve, stated as an outcome rather than
an implementation.

## Acceptance criteria

Numbered, each independently checkable from the diff. The review grades every
one as met / not-met / unclear, so vague criteria produce vague reviews.

Write them as observable behaviour:

1. A cache hit performs no SSH signing and no token-endpoint request.
2. A group/other readable cache file is refused and removed.

Not as intentions ("the cache should be secure").

## Non-goals

What this change deliberately does *not* do. The review reports diff work that
contradicts a stated non-goal, so this is the cheapest way to keep scope
honest.

## Verification

How the outcome was proven. For anything touching the live API, name the real
command and its real output — a passing unit suite is not evidence that the
API contract is right — a client method has shipped completely broken with a
green test suite, because the test asserted the same wrong call the code made.
