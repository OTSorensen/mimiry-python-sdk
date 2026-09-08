#!/usr/bin/env python3
"""Generate a compact API-contract digest from the Mimiry OpenAPI specs.

The full specs are ~137 KB — far too large to inline into a review context
pack (128 KB cap, shared with the diff, ticket, rules and file excerpts). This
distills them to just the contract surface an SDK client can get *wrong*:
HTTP method, path, required/optional request-body fields, and parameters.

That is exactly the failure class this exists to catch: a client method sent
`PATCH` with a `size_gb` field where the spec defines `PUT` with `new_size_gb`.
Every call failed, and the unit test passed — because it asserted the SDK's
behaviour rather than the API's contract. A digest small enough to sit in
every review pack lets a specialist check the diff against the real contract.

   Regenerate with:
       python3 scripts/gen_api_contract.py <spec-dir> -o .claude/review-rules/api-contract.md

   It lands in .claude/review-rules/ rather than docs/ because that is the
   directory the context-pack builder inlines into every review's engineering-
   rules section. In docs/ the specialist would never see its own yardstick.

   Prefer -o over a shell redirect: `>` truncates the target before this script
   runs, so a mis-invocation would replace a good digest with an empty file and
   the review would silently lose its yardstick. With -o the file is written
   only after a successful parse, via write-then-rename.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

METHOD_ORDER = ["get", "post", "put", "patch", "delete", "head", "options"]
_MAX_REF_DEPTH = 8


def resolve_ref(node: dict, root: dict, depth: int = 0) -> dict:
    """Follow a local ``$ref`` chain to the node it names.

    Returns the node unchanged when it carries no ``$ref``. Bounded so a
    self-referential spec cannot spin.
    """
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < _MAX_REF_DEPTH:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {}  # external refs are out of scope for a local digest
        target: object = root
        for part in ref.lstrip("#/").split("/"):
            if not isinstance(target, dict):
                return {}
            target = target.get(part, {})
        node = target if isinstance(target, dict) else {}
        seen += 1
    return node if isinstance(node, dict) else {}


def schema_fields(schema: dict, root: dict, depth: int = 0) -> tuple[list[str], list[str]]:
    """Return ``(required, optional)`` field names for a request-body schema.

    Composition matters: a schema expressed as ``allOf`` carries its fields in
    branches, and reading only the top level reports "no required fields" for
    an endpoint that has them — the digest would then bless a client omitting
    a mandatory field, which is the exact drift this file exists to surface.

    - ``allOf``  — merge every branch's properties, union their ``required``.
    - ``oneOf`` / ``anyOf`` — use the first branch. The digest is a review aid,
      not a validator, and naming one variant's fields beats naming none.
    """
    if depth > _MAX_REF_DEPTH or not isinstance(schema, dict):
        return [], []

    schema = resolve_ref(schema, root, depth)
    if not schema:
        return [], []

    props: dict = dict(schema.get("properties") or {})
    required: set[str] = set(schema.get("required") or [])

    for branch in schema.get("allOf") or []:
        b_req, b_opt = schema_fields(branch, root, depth + 1)
        for name in b_req:
            required.add(name)
            props.setdefault(name, {})
        for name in b_opt:
            props.setdefault(name, {})

    if not props and not required:
        for key in ("oneOf", "anyOf"):
            branches = schema.get(key) or []
            if branches:
                return schema_fields(branches[0], root, depth + 1)

    # A `required` name with no matching property is still part of the
    # contract — emit it rather than filtering it out, or the digest would
    # under-report exactly the fields a client must send.
    req = sorted(required)
    opt = sorted(name for name in props if name not in required)
    return req, opt


def body_schema(operation: dict, root: dict) -> tuple[dict, str | None]:
    """Return ``(schema, media_type)`` for an operation's request body.

    ``requestBody`` may itself be a ``$ref``, and the media type is not always
    ``application/json``. Both cases previously rendered identically to "no
    request body at all", which reads as a contract with nothing to get wrong.
    """
    body = resolve_ref(operation.get("requestBody") or {}, root)
    content = body.get("content") or {}
    if not content:
        return {}, None

    for media in ("application/json", "application/x-www-form-urlencoded", "*/*"):
        if media in content:
            return (content[media].get("schema") or {}), media

    media = next(iter(content))
    return (content[media].get("schema") or {}), media


def operation_params(path_item: dict, operation: dict, root: dict) -> tuple[list[str], list[str]]:
    """Return ``(required, optional)`` parameter names as ``name(in)``.

    Parameters are contract surface too: a required query parameter is exactly
    as breaking to omit as a required body field, and the first version of this
    digest left them invisible. Path-item-level parameters apply to every
    operation under it, so both levels are collected.
    """
    req: list[str] = []
    opt: list[str] = []
    seen: set[tuple[str, str]] = set()

    for raw in list(path_item.get("parameters") or []) + list(operation.get("parameters") or []):
        param = resolve_ref(raw if isinstance(raw, dict) else {}, root)
        name = param.get("name")
        where = param.get("in", "query")
        if not isinstance(name, str) or (name, where) in seen:
            continue
        seen.add((name, where))
        label = f"{name}({where})"
        (req if param.get("required") else opt).append(label)

    return sorted(req), sorted(opt)


def emit(spec_path: Path) -> tuple[list[str], int]:
    """Render one spec file. Returns ``(lines, endpoint_count)``."""
    try:
        spec = json.loads(spec_path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{spec_path}: not readable as JSON ({exc})") from exc

    lines: list[str] = [f"### {spec_path.stem}", ""]
    count = 0

    for path in sorted(spec.get("paths") or {}):
        path_item = spec["paths"][path]
        if not isinstance(path_item, dict):
            continue
        for method in METHOD_ORDER:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            count += 1

            head = f"- `{method.upper()} {path}`"
            schema, media = body_schema(op, spec)
            req, opt = schema_fields(schema, spec)

            if req:
                head += " — required: " + ", ".join(f"`{f}`" for f in req)
            if opt:
                head += (" · " if req else " — ") + "optional: " + ", ".join(f"`{f}`" for f in opt)
            if media and not req and not opt:
                # A body exists but yielded no field names — say so rather than
                # letting it look like an endpoint that takes no body.
                head += f" — body: unparsed ({media})"

            p_req, p_opt = operation_params(path_item, op, spec)
            if p_req:
                head += " · params required: " + ", ".join(f"`{p}`" for p in p_req)
            if p_opt:
                head += " · params: " + ", ".join(f"`{p}`" for p in p_opt)

            lines.append(head)

    lines.append("")
    return lines, count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the API-contract digest used by the review specialist."
    )
    parser.add_argument("spec_dir", help="directory holding the OpenAPI *.json specs")
    parser.add_argument(
        "-o",
        "--output",
        help="write here via write-then-rename (safer than a shell redirect); "
        "defaults to stdout",
    )
    args = parser.parse_args()

    spec_dir = Path(args.spec_dir).expanduser()
    if not spec_dir.is_dir():
        print(f"not a directory: {spec_dir}", file=sys.stderr)
        return 66

    specs = sorted(spec_dir.glob("*.json"))
    if not specs:
        # Refuse loudly. A header-only digest that exits 0 silently removes the
        # api-contract specialist's yardstick while every check still looks green.
        print(f"no *.json specs found in {spec_dir}", file=sys.stderr)
        return 66

    out = [
        "# Mimiry API contract (generated — do not hand-edit)",
        "",
        "Method, path, request-body fields and parameters distilled from the",
        "OpenAPI specs. Regenerate with:",
        "",
        "```",
        "python3 scripts/gen_api_contract.py <path-to-mimiry-documentation> \\",
        "    -o .claude/review-rules/api-contract.md",
        "```",
        "",
        "This is the yardstick for the api-contract review specialist: every",
        "`MimiryClient` method must match the method, path and field names below.",
        "",
    ]

    total = 0
    for spec in specs:
        lines, count = emit(spec)
        out += lines
        total += count

    if total == 0:
        print(f"parsed {len(specs)} spec file(s) but found no endpoints", file=sys.stderr)
        return 65

    body = "\n".join(out) + "\n"

    if not args.output:
        sys.stdout.write(body)
        return 0

    target = Path(args.output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".digest-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    print(f"{target}: {total} endpoints from {len(specs)} spec file(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
