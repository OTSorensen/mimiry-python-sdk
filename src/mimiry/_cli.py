"""CLI for SDK auth checks and compute-session management.

The SDK's primary surface is the Python decorator, but the CLI lets users
verify auth and inspect/manage sessions without writing code:

    $ mimiry balance
    {"account_type": "user", "balance": 49.95, ...}

    $ mimiry sessions --active          # what's running right now
    $ mimiry session status <id>
    $ mimiry session logs <id> --tail 100
    $ mimiry session terminate <id>
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


# ────────────────────────── session management ──────────────────────────

# Durable states that mean the session is over (not running, not billing).
_TERMINAL_STATES = "terminated,completed,failed,stopped,provision_failed"


def cmd_session_list(args: argparse.Namespace) -> int:
    """List sessions, newest first. ``--active`` shows only non-terminal ones."""
    params = {"state_not": _TERMINAL_STATES} if args.active else {}
    with _client() as c:
        sessions = c.list_sessions(**params)

    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0

    if not sessions:
        print("No active sessions." if args.active else "No sessions.")
        return 0

    sessions = sorted(sessions, key=lambda s: s.get("created_at") or "", reverse=True)
    if args.limit:
        sessions = sessions[: args.limit]

    # State only (widest durable state is "provision_failed" = 16 chars). The
    # transient sub-operation is available via `mimiry session status <id>`.
    print(f"{'ID':<36}  {'STATE':<16}  {'CREATED (UTC)':<20}  NAME")
    for s in sessions:
        print(
            f"{s.get('id', ''):<36}  {(s.get('state') or '?'):<16}  "
            f"{(s.get('created_at') or '')[:19]:<20}  {s.get('name', '')}"
        )
    return 0


def cmd_session_status(args: argparse.Namespace) -> int:
    """Print one session's full detail payload as JSON."""
    with _client() as c:
        print(json.dumps(c.get_session(args.id, events_tail=args.events), indent=2))
    return 0


def cmd_session_terminate(args: argparse.Namespace) -> int:
    """Terminate a running session."""
    with _client() as c:
        resp = c.terminate_session(args.id)
    print(f"Terminated {args.id}." if resp is None else json.dumps(resp, indent=2))
    return 0


def cmd_session_logs(args: argparse.Namespace) -> int:
    """Fetch a session's container logs."""
    with _client() as c:
        resp = c.get_logs(args.id, tail=args.tail, timestamps=args.timestamps)
    status = resp.get("_status", 200)
    if status == 503:
        wait = resp.get("retry_after_seconds", 5)
        print(f"Container still starting — retry in {wait}s.", file=sys.stderr)
        return 1
    if status == 409:
        print("Session is not running; no logs available.", file=sys.stderr)
        return 1
    print((resp.get("logs") or "").rstrip("\n"))
    return 0


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

    def _add_list_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--active", action="store_true",
                       help="Only non-terminal (running/provisioning) sessions.")
        p.add_argument("--limit", type=int, default=20, help="Max rows to show (default 20).")
        p.add_argument("--json", action="store_true", help="Raw JSON output.")
        p.set_defaults(func=cmd_session_list)

    # `mimiry session <verb>` group.
    sess = subs.add_parser("session", help="Manage compute sessions.")
    sess_subs = sess.add_subparsers(dest="session_cmd", required=True)
    _add_list_args(sess_subs.add_parser("list", help="List sessions (newest first)."))

    s_status = sess_subs.add_parser("status", help="Show one session's details.")
    s_status.add_argument("id", help="Session ID.")
    s_status.add_argument("--events", type=int, metavar="N",
                          help="Include last N events (use -1 for all).")
    s_status.set_defaults(func=cmd_session_status)

    s_term = sess_subs.add_parser("terminate", help="Terminate a running session.")
    s_term.add_argument("id", help="Session ID.")
    s_term.set_defaults(func=cmd_session_terminate)

    s_logs = sess_subs.add_parser("logs", help="Fetch a session's container logs.")
    s_logs.add_argument("id", help="Session ID.")
    s_logs.add_argument("--tail", type=int, default=200, help="Lines from the end (default 200).")
    s_logs.add_argument("--timestamps", action="store_true", help="Prefix each line with a timestamp.")
    s_logs.set_defaults(func=cmd_session_logs)

    # Convenience: `mimiry sessions` == `mimiry session list`.
    _add_list_args(subs.add_parser("sessions", help="Alias for `session list`."))

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
