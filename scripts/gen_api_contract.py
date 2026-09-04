#!/usr/bin/env python3
"""Generate a compact API-contract digest from the Mimiry OpenAPI specs.

The full specs are ~137 KB — far too large to inline into a review context
pack (128 KB cap, shared with the diff, ticket, rules and file excerpts). This
distills them to just the contract surface an SDK client can get *wrong*:
HTTP method, path, and required request-body field names.

That is exactly the failure class this exists to catch: a client method sent
`PATCH` with a `size_gb` field where the spec defines `PUT` with `new_size_gb`.
Every call failed, and the unit test passed — because it asserted the SDK's
behaviour rather than the API's contract. A digest small enough to sit in
every review pack lets a specialist check the diff against the real contract.

   Regenerate with:
       python3 scripts/gen_api_contract.py <spec-dir> > .claude/review-rules/api-contract.md

   It lands in .claude/review-rules/ rather than docs/ because that is the
   directory the context-pack builder inlines into every review's engineering-
   rules section. In docs/ the specialist would never see its own yardstick.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

METHOD_ORDER = ["get", "post", "put", "patch", "delete"]


def resolve_ref(ref: str, root: dict) -> dict:
    """Resolve a local '#/components/schemas/X' reference."""
    node: object = root
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def schema_fields(schema: dict, root: dict, depth: int = 0) -> tuple[list[str], list[str]]:
    """Return (required_fields, optional_fields) for a request-body schema."""
    if depth > 4 or not isinstance(schema, dict):
        return [], []
    if "$ref" in schema:
        return schema_fields(resolve_ref(schema["$ref"], root), root, depth + 1)

    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    req = sorted(f for f in props if f in required)
    opt = sorted(f for f in props if f not in required)
    return req, opt


def body_schema(operation: dict, root: dict) -> dict:
    content = (operation.get("requestBody") or {}).get("content") or {}
    for media in ("application/json", "*/*"):
        if media in content:
            return content[media].get("schema") or {}
    return {}


def emit(spec_path: Path) -> list[str]:
    spec = json.loads(spec_path.read_text())
    lines: list[str] = [f"### {spec_path.stem}", ""]

    for path in sorted(spec.get("paths", {})):
        ops = spec["paths"][path]
        for method in METHOD_ORDER:
            op = ops.get(method)
            if not isinstance(op, dict):
                continue
            head = f"- `{method.upper()} {path}`"
            req, opt = schema_fields(body_schema(op, spec), spec)
            if req:
                head += f" — required: {', '.join('`' + f + '`' for f in req)}"
            if opt:
                head += f" · optional: {', '.join('`' + f + '`' for f in opt)}"
            lines.append(head)
    lines.append("")
    return lines


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 64
    spec_dir = Path(sys.argv[1]).expanduser()
    if not spec_dir.is_dir():
        print(f"not a directory: {spec_dir}", file=sys.stderr)
        return 66

    out = [
        "# Mimiry API contract (generated — do not hand-edit)",
        "",
        "Method, path, and request-body field names distilled from the OpenAPI",
        "specs. Regenerate with:",
        "",
        "```",
        "python3 scripts/gen_api_contract.py <path-to-mimiry-documentation> > .claude/review-rules/api-contract.md",
        "```",
        "",
        "This is the yardstick for the api-contract review specialist: every",
        "`MimiryClient` method must match the method and field names below.",
        "",
    ]
    for spec in sorted(spec_dir.glob("*.json")):
        out += emit(spec)

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
