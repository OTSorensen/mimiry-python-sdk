#!/usr/bin/env python3
"""Byte-precise section truncation for the context-pack builder.

Every function operates on raw bytes and guarantees its output never
exceeds the byte budget it was given; truncation is always announced
in-band with an explicit marker. Deterministic by construction: stdlib
only, integer arithmetic, byte-keyed ordering.

Subcommands (all budgets in bytes):
  clip          --budget N --label L [--fence LANG]     stdin -> stdout
  clip-md       --budget N --label L                    stdin -> stdout
  files-measure --repo DIR --diff FILE --floor N --ceil N
                prints "total_need floor_need"
  files-emit    --repo DIR --diff FILE --floor N --ceil N --budget N
                [--starved-ok]                          -> stdout
"""
import argparse
import os
import re
import sys

CONTEXT_LINES = 8      # lines of context around a changed range
MERGE_GAP = 4          # merge windows separated by <= this many lines
MIN_MARKER_ALLOWANCE = 200  # a truncation marker may use up to this even at budget 0
MAX_LISTED_HEADINGS = 15

HUNK_RE = re.compile(rb"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _unquote_git_path(raw):
    """Undo git's C-style path quoting ("b/src/k\\303\\270re.ts")."""
    if not (raw.startswith(b'"') and raw.endswith(b'"') and len(raw) >= 2):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):
            nxt = body[i + 1:i + 2]
            if nxt in (b"\\", b'"'):
                out += nxt
                i += 2
                continue
            simple = {b"t": 9, b"n": 10, b"r": 13}.get(nxt)
            if simple is not None:
                out.append(simple)
                i += 2
                continue
            oct3 = body[i + 1:i + 4]
            if len(oct3) == 3 and oct3.isdigit():
                out.append(int(oct3, 8) & 0xFF)
                i += 4
                continue
        out.append(c)
        i += 1
    return bytes(out)


def parse_diff(diff):
    """Map new-file path -> list of changed (start, end) line ranges.

    An empty list means "no parseable hunks — treat as whole file".
    Deleted files (+++ /dev/null) are skipped. Hunk bodies are consumed by
    line count so content lines starting with "+++ " or "@@" can never be
    mistaken for headers. Handles git's trailing-TAB disambiguation and
    C-quoted paths.
    """
    parsed = {}
    current = None
    old_rem = new_rem = 0
    for line in diff.split(b"\n"):
        if old_rem > 0 or new_rem > 0:
            if line.startswith(b"\\"):      # "\ No newline at end of file"
                continue
            if line.startswith(b"+"):
                new_rem -= 1
            elif line.startswith(b"-"):
                old_rem -= 1
            else:
                old_rem -= 1
                new_rem -= 1
            continue
        if line.startswith(b"+++ "):
            target = _unquote_git_path(line[4:].rstrip(b"\t"))
            if target == b"/dev/null":
                current = None
            else:
                if target.startswith(b"b/"):
                    target = target[2:]
                current = target.decode("utf-8", "surrogateescape")
                parsed.setdefault(current, [])
            continue
        m = HUNK_RE.match(line)
        if m and current is not None:
            old_count = int(m.group(1)) if m.group(1) is not None else 1
            start = int(m.group(2))
            count = int(m.group(3)) if m.group(3) is not None else 1
            if count == 0:
                anchor = max(start, 1)
                parsed[current].append((anchor, anchor))
            else:
                parsed[current].append((start, start + count - 1))
            old_rem, new_rem = old_count, count
    return parsed


def windows_for(ranges, nlines, context=CONTEXT_LINES, gap=MERGE_GAP):
    """Expand changed ranges by context and merge; 1-indexed inclusive."""
    if nlines <= 0:
        return []
    if not ranges:
        return [(1, nlines)]
    expanded = sorted((max(1, lo - context), min(nlines, hi + context))
                      for lo, hi in ranges)
    merged = [expanded[0]]
    for lo, hi in expanded[1:]:
        plo, phi = merged[-1]
        if lo - phi - 1 <= gap:
            merged[-1] = (plo, max(phi, hi))
        else:
            merged.append((lo, hi))
    return merged


def is_binary(data):
    return b"\x00" in data[:8192]


def _utf8_safe_cut(data, limit):
    """Hard byte cut that never splits a UTF-8 code point."""
    if len(data) <= limit:
        return data
    cut = limit
    while cut > 0 and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    return data[:cut]


def _line_cut(data, limit):
    """Cut at the last newline within limit bytes; hard cut if none fits."""
    if len(data) <= limit:
        return data
    if limit <= 0:
        return b""
    nl = data.rfind(b"\n", 0, limit)
    if nl == -1:
        return _utf8_safe_cut(data, limit)
    return data[:nl + 1]


def _newline_safe_cut(data, limit):
    """Hard cut that never leaves a dangling partial line."""
    if len(data) <= limit:
        return data
    cut = _utf8_safe_cut(data, max(limit, 0))
    nl = cut.rfind(b"\n")
    return cut[:nl + 1] if nl != -1 else b""


def _fence_for(data, lang=b""):
    run = 0
    for m in re.finditer(rb"`+", data):
        run = max(run, len(m.group(0)))
    fence = b"`" * max(3, run + 1)
    return fence + lang + b"\n", fence + b"\n"


def _clip_marker(label, shown, total):
    hint = " — split the review with --paths" if label == "diff" else ""
    return ("\n_[%s truncated: showing %d of %d bytes%s]_\n"
            % (label, shown, total, hint)).encode("utf-8")


def clip_bytes(data, budget, label, fence_lang=None):
    """Newline-boundary clip with a loud marker; output <= budget."""
    budget = max(budget, 0)
    if fence_lang is None:
        if len(data) <= budget:
            return data
        reserve = len(_clip_marker(label, len(data), len(data)))
        kept = _line_cut(data, max(budget - reserve, 0))
        return kept + _clip_marker(label, len(kept), len(data))

    opening, closing = _fence_for(data, fence_lang.encode("utf-8"))
    body = data if (data.endswith(b"\n") or not data) else data + b"\n"
    if len(opening) + len(body) + len(closing) <= budget:
        return opening + body + closing
    reserve = (len(opening) + len(closing) + 1
               + len(_clip_marker(label, len(data), len(data))))
    kept = _line_cut(data, max(budget - reserve, 0))
    if kept and not kept.endswith(b"\n"):
        kept += b"\n"
    # a clipped prefix cannot contain more backticks than the whole input,
    # so the fence chosen from the full input stays safe
    return (opening + kept + closing
            + _clip_marker(label, len(kept), len(data)))


def _md_blocks(data):
    """Split markdown into (heading_line_or_None, block_bytes) tuples."""
    blocks = []
    current_heading = None
    current = []
    for line in data.splitlines(keepends=True):
        if re.match(rb"^#{1,6} ", line):
            if current or current_heading is not None:
                blocks.append((current_heading, b"".join(current)))
            current_heading = line.rstrip(b"\n").decode("utf-8",
                                                        "surrogateescape")
            current = [line]
        else:
            current.append(line)
    if current or current_heading is not None:
        blocks.append((current_heading, b"".join(current)))
    return blocks


def _md_marker(label, shown, total, omitted_headings, listed,
               first_is_partial=False):
    names = list(omitted_headings[:listed])
    quoted = ['"%s"' % h for h in names]
    if quoted and first_is_partial:
        quoted[0] += " (cut mid-section)"
    parts = ", ".join(quoted)
    rest = len(omitted_headings) - len(names)
    if rest > 0:
        parts += " …and %d more" % rest
    if not parts:
        parts = "(unnamed content)"
    return ("\n_[%s truncated: showing %d of %d bytes. Omitted sections: %s]_\n"
            % (label, shown, total, parts)).encode("utf-8")


MIN_PARTIAL_BLOCK = 200  # don't bother rendering a sliver of a section


def clip_md_bytes(data, budget, label):
    """Clip markdown at heading boundaries, naming what was omitted.

    The first omitted section is line-clipped into the remaining space when
    at least MIN_PARTIAL_BLOCK bytes of it fit, so a huge trailing section
    cannot strand the whole allocation.
    """
    budget = max(budget, 0)
    if len(data) <= budget:
        return data
    blocks = _md_blocks(data)
    if not any(h for h, _ in blocks):
        return clip_bytes(data, budget, label)
    total = len(data)
    for k in range(len(blocks), -1, -1):
        kept = b"".join(body for _, body in blocks[:k])
        omitted = [h for h, _ in blocks[k:] if h]
        marker = _md_marker(label, len(kept), total, omitted,
                            MAX_LISTED_HEADINGS)
        if len(kept) + len(marker) <= budget:
            if k < len(blocks) and blocks[k][0] is not None:
                # size the partial marker with max-width digits (shown<=total)
                sizing = _md_marker(label, total, total, omitted,
                                    MAX_LISTED_HEADINGS,
                                    first_is_partial=True)
                avail = budget - len(kept) - len(sizing)
                partial = _line_cut(blocks[k][1], avail) if avail > 0 else b""
                if len(partial) >= MIN_PARTIAL_BLOCK:
                    return kept + partial + _md_marker(
                        label, len(kept) + len(partial), total, omitted,
                        MAX_LISTED_HEADINGS, first_is_partial=True)
            return kept + marker
    # nothing fits: emit the marker alone, shrinking its heading list
    allowance = max(budget, MIN_MARKER_ALLOWANCE)
    omitted = [h for h, _ in blocks if h]
    for listed in range(min(MAX_LISTED_HEADINGS, len(omitted)), -1, -1):
        marker = _md_marker(label, 0, total, omitted, listed)
        if len(marker) <= allowance:
            return marker
    return _utf8_safe_cut(_md_marker(label, 0, total, omitted, 0), allowance)


def _intersects(r, w):
    return r[0] <= w[1] and w[0] <= r[1]


def _gap_marker(lo, hi, ranges, windows):
    """Omission line for lines lo..hi; flags changed hunks hidden inside."""
    hidden = sum(1 for r in ranges
                 if _intersects(r, (lo, hi))
                 and not any(_intersects(r, w) for w in windows))
    if hidden:
        return ("[lines %d-%d omitted — contains %d changed hunk%s]"
                % (lo, hi, hidden, "" if hidden == 1 else "s")).encode("utf-8")
    return b"[lines %d-%d omitted]" % (lo, hi)


def _block_parts(lines, windows, nlines, ranges):
    """Body lines (with omission markers) + shown-line count for windows."""
    body = []
    shown = 0
    prev_end = 0
    for lo, hi in windows:
        if lo > prev_end + 1:
            body.append(_gap_marker(prev_end + 1, lo - 1, ranges, windows))
        body.extend(lines[lo - 1:hi])
        shown += hi - lo + 1
        prev_end = hi
    if prev_end < nlines:
        body.append(_gap_marker(prev_end + 1, nlines, ranges, windows))
    return body, shown


def _assemble(path, lines, windows, nlines, total_bytes, ranges):
    header = ("### %s\n" % path).encode("utf-8", "surrogateescape")
    whole = windows == [(1, nlines)]
    body_lines, shown = _block_parts(lines, windows, nlines, ranges)
    body = b"".join(line + b"\n" for line in body_lines)
    status = b""
    if not whole:
        if ranges:
            hunks_shown = sum(1 for r in ranges
                              if any(_intersects(r, w) for w in windows))
            status = ("_showing %d of %d lines (%d of %d changed hunks; "
                      "%d of %d bytes)_\n"
                      % (shown, nlines, hunks_shown, len(ranges),
                         len(body), total_bytes)).encode("utf-8")
        else:
            status = ("_showing %d of %d lines (whole-file excerpt; "
                      "%d of %d bytes)_\n"
                      % (shown, nlines, len(body),
                         total_bytes)).encode("utf-8")
    opening, closing = _fence_for(body)
    return header + status + opening + body + closing + b"\n"


def render_block(path, content, ranges, cap):
    """One `### path` block fitted within cap bytes, hunk-centered.

    content=None renders a loud not-on-disk stub instead of a silent skip.
    """
    cap = max(cap, 0)
    header = ("### %s\n" % path).encode("utf-8", "surrogateescape")
    if content is None:
        stub = header + "_[file not present on disk — skipped]_\n\n".encode(
            "utf-8")
        return _newline_safe_cut(stub, cap)
    if is_binary(content):
        stub = header + ("_binary file (%d bytes) — omitted_\n\n"
                         % len(content)).encode("utf-8")
        return _newline_safe_cut(stub, cap)
    lines = content.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    nlines = len(lines)
    total = len(content)

    for context in (CONTEXT_LINES, 4, 2, 0):
        block = _assemble(path, lines, windows_for(ranges, nlines, context),
                          nlines, total, ranges)
        if len(block) <= cap:
            return block
    zero = windows_for(ranges, nlines, 0)
    for keep in range(len(zero) - 1, 0, -1):
        block = _assemble(path, lines, zero[:keep], nlines, total, ranges)
        if len(block) <= cap:
            return block
    if zero:
        lo, hi = zero[0]
        while hi > lo:
            hi = lo + (hi - lo) // 2
            block = _assemble(path, lines, [(lo, hi)], nlines, total, ranges)
            if len(block) <= cap:
                return block
    fallback = header + b"_[excerpt omitted: allowance too small]_\n\n"
    return _newline_safe_cut(fallback, cap)


def waterfill(items, budget):
    """Deterministic allocation of budget across (name, need, floor) items.

    Smallest needs served first so their surplus flows to larger files;
    every file gets at least its floor while budget remains.
    """
    order = sorted(items, key=lambda it: (it[1], it[0]))
    remaining = max(budget, 0)
    alloc = {}
    for i, (name, need, floor) in enumerate(order):
        share = remaining // (len(order) - i)
        a = min(need, max(share, min(need, floor)))
        a = min(a, remaining)
        alloc[name] = a
        remaining -= a
    return alloc


def _collect_files(repo, diff, floor, ceil):
    """(path, content, ranges, need, floor_need, ideal) per changed file.

    need caps the section's claim on the shared pool at ceil per file;
    ideal is the full hunk-centered excerpt, so leftover pool can deepen
    files past the ceiling instead of going unused. Files missing on disk
    get a loud stub (content=None), never a silent skip.
    """
    out = []
    for path, ranges in sorted(parse_diff(diff).items()):
        full = os.path.join(repo, path)
        repo_real = os.path.realpath(repo)
        if os.path.islink(full):
            # Never follow it: a symlink committed by the branch under review
            # would otherwise pull whatever it points at — outside the repo
            # included — into the pack (review 20260822-122651). The diff
            # already shows the link's target path; that is all it contains.
            content = ("_symlink -> %s; not excerpted_\n"
                       % os.readlink(full)).encode("utf-8", "surrogateescape")
        elif not os.path.realpath(full).startswith(repo_real + os.sep):
            # A symlinked PARENT directory (git never records a path through
            # one, so this is local tampering) must not route the read outside
            # the repo either: containment is checked on the resolved path.
            content = ("_resolves outside the repo (symlink in its path); "
                       "not excerpted_\n").encode("utf-8", "surrogateescape")
        elif not os.path.isfile(full):
            stub = len(render_block(path, None, ranges, cap=1 << 30))
            out.append((path, None, ranges, stub, stub, stub))
            continue
        else:
            with open(full, "rb") as fh:
                content = fh.read()
        overhead = len(("### %s\n" % path).encode("utf-8",
                                                  "surrogateescape")) + 112
        ideal = len(render_block(path, content, ranges, cap=1 << 30))
        need = min(ideal, ceil + overhead)
        floor_need = min(ideal, floor + overhead)
        out.append((path, content, ranges, need, floor_need, ideal))
    return out


def measure(repo, diff, floor, ceil):
    files = _collect_files(repo, diff, floor, ceil)
    return (sum(f[3] for f in files), sum(f[4] for f in files),
            sum(f[5] for f in files))


STARVED_WARNING = ("_[WARNING: file excerpts below floor — pack is "
                   "starved; split with --paths]_\n\n").encode("utf-8")


def emit(repo, diff, floor, ceil, budget, starved_ok=False):
    files = _collect_files(repo, diff, floor, ceil)
    if not files:
        return b""
    budget = max(budget, 0)
    prefix = b""
    floor_total = sum(f[4] for f in files)
    if budget < floor_total:
        prefix = STARVED_WARNING
        budget = max(budget - len(prefix), 0)
        files = [(p, c, r, need, 0, ideal)
                 for p, c, r, need, _, ideal in files]
    # waterfill against the ideal need: when the granted budget exceeds the
    # per-file ceilings, files deepen instead of stranding the surplus
    alloc = waterfill([(p, ideal, fl)
                       for p, c, r, _, fl, ideal in files], budget)
    parts = [prefix]
    for path, content, ranges, _, _, _ in files:  # already path-sorted
        parts.append(render_block(path, content, ranges, cap=alloc[path]))
    return b"".join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pack_sections.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("clip")
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--fence", default=None)

    p = sub.add_parser("clip-md")
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--label", required=True)

    for name in ("files-measure", "files-emit"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--diff", required=True)
        p.add_argument("--floor", type=int, required=True)
        p.add_argument("--ceil", type=int, required=True)
        if name == "files-emit":
            p.add_argument("--budget", type=int, required=True)
            p.add_argument("--starved-ok", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "clip":
        data = sys.stdin.buffer.read()
        sys.stdout.buffer.write(clip_bytes(data, args.budget, args.label,
                                           fence_lang=args.fence))
    elif args.cmd == "clip-md":
        data = sys.stdin.buffer.read()
        sys.stdout.buffer.write(clip_md_bytes(data, args.budget, args.label))
    else:
        with open(args.diff, "rb") as fh:
            diff = fh.read()
        if args.cmd == "files-measure":
            total, floor_need, ideal = measure(args.repo, diff, args.floor,
                                               args.ceil)
            print("%d %d %d" % (total, floor_need, ideal))
        else:
            _, floor_need, _ = measure(args.repo, diff, args.floor,
                                       args.ceil)
            if args.budget < floor_need and not args.starved_ok:
                sys.stderr.write(
                    "files-emit: budget %d B is below the %d B floor for "
                    "these files; pass --starved-ok to force a starved "
                    "section\n" % (args.budget, floor_need))
                return 65
            sys.stdout.buffer.write(emit(args.repo, diff, args.floor,
                                         args.ceil, args.budget,
                                         starved_ok=args.starved_ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
