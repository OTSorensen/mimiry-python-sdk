#!/usr/bin/env python3
"""Extract the last ```json fenced block from a review report. Stdlib only."""
import json
import re
import sys


def extract(text: str) -> dict:
    blocks = re.findall(r"```json\s*\n(.*?)```", text, flags=re.DOTALL)
    if not blocks:
        raise ValueError("no ```json block found in report")
    return json.loads(blocks[-1])


if __name__ == "__main__":
    print(json.dumps(extract(sys.stdin.read()), indent=2))
