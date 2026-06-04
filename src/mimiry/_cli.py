"""Minimal CLI for sanity-checking SDK auth + API connectivity.

This is intentionally tiny in v1 — the SDK's primary surface is the Python
decorator. The CLI exists so users can verify their auth setup before
writing code:

    $ mimiry balance
    {"account_type": "user", "balance": 49.95, ...}
"""

from __future__ import annotations

import argparse
import json
import sys

from mimiry._auth import get_token
from mimiry._client import MimiryClient
from mimiry._config import configure, get_config


def _client() -> MimiryClient:
    cfg = get_config()
    token = get_token(cfg.ssh_key_path, cfg.api_base)
    return MimiryClient(token)


def cmd_balance(_: argparse.Namespace) -> int:
    with _client() as c:
        print(json.dumps(c.get_balance(), indent=2))
    return 0


def cmd_quota(_: argparse.Namespace) -> int:
    with _client() as c:
        print(json.dumps(c.get_quota(), indent=2))
    return 0


def cmd_availability(args: argparse.Namespace) -> int:
    with _client() as c:
        params = {}
        if args.gpu_family:
            params["gpu_family"] = args.gpu_family
        print(json.dumps(c.get_availability(**params), indent=2))
    return 0


def cmd_token(_: argparse.Namespace) -> int:
    """Print a fresh JWT — useful for piping into curl during debugging."""
    cfg = get_config()
    token = get_token(cfg.ssh_key_path, cfg.api_base)
    print(token.access_token)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive first-time auth wizard (also aliased as ``mimiry init``)."""
    from mimiry._setup import run_setup_wizard

    return run_setup_wizard(ssh_key_path=args.ssh_key, api_base=args.api_base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mimiry", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ssh-key",
        help="Path to SSH private key (overrides MIMIRY_SSH_KEY env var).",
    )
    parser.add_argument("--api-base", help="API base URL (default: softlaunch.mimiry.com).")
    subs = parser.add_subparsers(dest="cmd", required=True)

    subs.add_parser("balance", help="Show account balance.").set_defaults(func=cmd_balance)
    subs.add_parser("quota", help="Show account quota / usage.").set_defaults(func=cmd_quota)

    avail = subs.add_parser("availability", help="Show GPU availability (no auth).")
    avail.add_argument("--gpu-family", help="Filter, e.g. T4 or H100.")
    avail.set_defaults(func=cmd_availability)

    subs.add_parser("token", help="Print a fresh JWT (for debugging).").set_defaults(func=cmd_token)

    setup_help = "Interactive auth setup wizard (generate/register SSH key, verify)."
    subs.add_parser("setup", help=setup_help).set_defaults(func=cmd_setup)
    subs.add_parser("init", help="Alias for `setup`.").set_defaults(func=cmd_setup)

    args = parser.parse_args(argv)

    if args.ssh_key or args.api_base:
        configure(ssh_key_path=args.ssh_key, api_base=args.api_base)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
