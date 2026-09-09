"""CLI for SDK auth checks and compute-session/volume management.

The SDK's primary surface is the Python decorator, but the CLI lets users
verify auth and inspect/manage sessions and volumes without writing code:

    $ mimiry balance
    $ mimiry availability --gpu-family A100 --provider verda
    $ mimiry sessions --active
    $ mimiry session create --image docker.io/pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime \\
          --gpu A100_40G_SXM --provider verda --command "nvidia-smi" --wait
    $ mimiry session logs <id> --follow
    $ mimiry session ssh <id>
    $ mimiry session terminate <id>
    $ mimiry volume create --name data --size-gb 100
    $ mimiry transactions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from mimiry import __version__
from mimiry._auth import get_token
from mimiry._availability import preflight_gpu_availability
from mimiry._client import MimiryClient
from mimiry._config import configure, get_config
from mimiry._session import ERROR_STATES, READY_STATE, TERMINAL_STATES, _extract_state
from mimiry._ssh import _common_ssh_opts, ssh_target_from_session

# Server-side filter for `sessions --active`: the states that mean a session is
# over. Derived from the single source of truth in _session so a state added
# there cannot be silently omitted here — a missing name makes a dead session
# show up as active.
_TERMINAL_STATES = ",".join(sorted(TERMINAL_STATES))


def _client() -> MimiryClient:
    cfg = get_config()
    token = get_token(cfg.ssh_key_path, cfg.api_base)
    return MimiryClient(token)


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2))


def _pubkey() -> str:
    cfg = get_config()
    if cfg.ssh_key_path is None:
        raise RuntimeError("no SSH key configured — run `mimiry setup` or set MIMIRY_SSH_KEY")
    from pathlib import Path

    return Path(f"{cfg.ssh_key_path}.pub").read_text().strip()


# ────────────────────────── account ──────────────────────────


def cmd_balance(_: argparse.Namespace) -> int:
    with _client() as c:
        _print_json(c.get_balance())
    return 0


def cmd_quota(_: argparse.Namespace) -> int:
    with _client() as c:
        _print_json(c.get_quota())
    return 0


def cmd_transactions(args: argparse.Namespace) -> int:
    params = {"limit": args.limit} if args.limit else {}
    with _client() as c:
        _print_json(c.get_transactions(**params))
    return 0


def cmd_availability(args: argparse.Namespace) -> int:
    params: dict[str, object] = {}
    if args.gpu_family:
        params["gpu_family"] = args.gpu_family
    if args.provider:
        params["provider"] = args.provider
    if args.location:
        params["location"] = args.location
    if args.min_vram:
        params["min_vram_gb"] = args.min_vram
    if args.available_only:
        params["available_only"] = "true"
    with _client() as c:
        _print_json(c.get_availability(**params))
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    """Print a JWT — useful for piping into curl during debugging."""
    cfg = get_config()
    token = get_token(cfg.ssh_key_path, cfg.api_base, use_cache=not args.refresh)
    print(token.access_token)
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    """Discard cached JWTs. The SSH key and config file are left untouched."""
    from mimiry import _token_cache

    removed = _token_cache.clear()
    print(f"Cleared {removed} cached token{'' if removed == 1 else 's'}.")
    # Say what this does NOT do. These are bearer credentials with up to an
    # hour of life, and `mimiry token` exists to pipe them into other tools —
    # so copies routinely outlive the cache in shell history and scrollback.
    # Mimiry exposes no revocation endpoint, so a user reaching for `logout`
    # after a suspected leak needs to know the real remedy is key rotation.
    print(
        "Any token already issued stays valid until it expires (up to 1h) — "
        "Mimiry has no revocation endpoint. If a token may have leaked, "
        "rotate your SSH key in the portal."
    )
    return 0


def cmd_config(_: argparse.Namespace) -> int:
    """Show the resolved SDK configuration (no network call)."""
    cfg = get_config()
    _print_json(
        {
            "ssh_key_path": str(cfg.ssh_key_path) if cfg.ssh_key_path else None,
            "api_base": cfg.api_base,
            "timeout_seconds": cfg.timeout_seconds,
        }
    )
    return 0


def cmd_whoami(_: argparse.Namespace) -> int:
    """Verify auth end-to-end and print the account balance."""
    with _client() as c:
        bal = c.get_balance()
    print(f"Authenticated against {get_config().api_base}.")
    _print_json(bal)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive first-time auth wizard (also aliased as ``mimiry init``)."""
    from mimiry._setup import run_setup_wizard

    return run_setup_wizard(ssh_key_path=args.ssh_key, api_base=args.api_base)


# ────────────────────────── session management ──────────────────────────


def _print_table(rows: list[dict], cols: list[tuple[str, str, int]]) -> None:
    """Render ``rows`` as a fixed-width table. ``cols`` = (header, key, width)."""
    header = "  ".join(f"{h:<{w}}" for h, _, w in cols)
    print(header)
    for r in rows:
        print("  ".join(f"{str(r.get(k, '') or ''):<{w}}" for _, k, w in cols))


def cmd_session_list(args: argparse.Namespace) -> int:
    params = {"state_not": _TERMINAL_STATES} if args.active else {}
    with _client() as c:
        sessions = c.list_sessions(**params)
    if args.json:
        _print_json(sessions)
        return 0
    if not sessions:
        print("No active sessions." if args.active else "No sessions.")
        return 0
    sessions = sorted(sessions, key=lambda s: s.get("created_at") or "", reverse=True)
    if args.limit:
        sessions = sessions[: args.limit]
    rows = [{**s, "created": (s.get("created_at") or "")[:19]} for s in sessions]
    _print_table(rows, [("ID", "id", 36), ("STATE", "state", 16),
                        ("CREATED (UTC)", "created", 20), ("NAME", "name", 0)])
    return 0


def cmd_session_status(args: argparse.Namespace) -> int:
    with _client() as c:
        if args.wait:
            payload = _wait_for_terminal(c, args.id)
        else:
            payload = c.get_session(args.id, events_tail=args.events)
        _print_json(payload)
    # Non-zero exit if the session ended in an error state (scriptability).
    return 1 if _extract_state(payload) in {"failed", "provision_failed", "stopped"} else 0


def cmd_session_terminate(args: argparse.Namespace) -> int:
    with _client() as c:
        resp = c.terminate_session(args.id)
    print(f"Terminated {args.id}." if resp is None else json.dumps(resp, indent=2))
    return 0


def cmd_session_logs(args: argparse.Namespace) -> int:
    with _client() as c:
        if args.follow:
            return _follow_logs(c, args.id, tail=args.tail, timestamps=args.timestamps)
        resp = c.get_logs(args.id, tail=args.tail, timestamps=args.timestamps)
    status = resp.get("_status", 200)
    if status == 503:
        print(f"Container still starting — retry in {resp.get('retry_after_seconds', 5)}s.",
              file=sys.stderr)
        return 1
    if status == 409:
        print("Session is not running; no logs available.", file=sys.stderr)
        return 1
    print((resp.get("logs") or "").rstrip("\n"))
    return 0


def cmd_session_ssh(args: argparse.Namespace) -> int:
    """Open an interactive SSH shell into a running session."""
    cfg = get_config()
    with _client() as c:
        payload = c.get_session(args.id)
    state = _extract_state(payload)
    ssh = payload.get("ssh") or {}

    # Gate on the endpoint, not on the state name. A session that publishes a
    # host and has not reached a terminal state is worth attempting: refusing
    # on an unrecognised state strands a paid, reachable instance behind a CLI
    # that will not connect to it. Only a terminal state — where no host can
    # exist — is a hard refusal.
    if state in TERMINAL_STATES:
        print(
            f"Session {args.id} has ended (state={state}); there is nothing to connect to. "
            "Check `mimiry session logs` for what it did.",
            file=sys.stderr,
        )
        return 1
    if not ssh.get("host"):
        if payload.get("ssh_enabled") is False:
            print(
                f"Session {args.id} was created with --no-ssh, so it has no SSH endpoint.",
                file=sys.stderr,
            )
        else:
            print(
                f"Session {args.id} has no SSH endpoint yet (state={state}). "
                "It is still coming up — retry in a moment, or watch it with "
                "`mimiry session status`.",
                file=sys.stderr,
            )
        return 1
    argv = _build_ssh_argv(payload, cfg.ssh_key_path)
    os.execvp(argv[0], argv)  # replaces this process with ssh
    return 0  # unreachable


def cmd_session_create(args: argparse.Namespace) -> int:
    payload = _build_create_payload(args)
    with _client() as c:
        # A volume can only be mounted by a session in its own location. Settle
        # that first, so the GPU availability check below runs against the
        # location the session will really use — and so a conflict is refused
        # before the session exists rather than killed seconds after.
        location = _preflight_volume_location(
            c, payload.get("volume_mounts") or [], args.location
        ) or args.location
        if location:
            payload["gpu"]["location"] = location
        # Resolve a GPU family alias (e.g. "A100") to the concrete catalog name
        # the API requires, and fail fast on an impossible combo. Best-effort.
        resolved_gpu = preflight_gpu_availability(c, args.gpu, args.provider, location)
        payload["gpu"]["types"] = [resolved_gpu]
        if location and location != args.location:
            print(f"Using location {location} — the attached volume lives there.")
        session = c.create_session(payload)
        sid = session.get("id", "?")
        print(f"Created session {sid} (state={session.get('state', '?')}).")
        if args.wait:
            final = _wait_for_ready(c, sid)
            state = _extract_state(final)
            if state in ERROR_STATES:
                err = final.get("error")
                print(
                    f"Session failed in state={state}."
                    + (f" {err}" if err else "")
                    + f"\n  mimiry session logs {sid}",
                    file=sys.stderr,
                )
                return 1
            if state in TERMINAL_STATES:
                # The session ran and ended before we saw it ready — normal for
                # a short `--command` run with auto-terminate. The work was
                # done, so this is a success, not a failure.
                print(f"Session ran and ended (state={state}).")
                print(f"  mimiry session logs {sid}")
                return 0
            ssh = final.get("ssh") or {}
            if ssh.get("host"):
                print(f"  SSH ready: mimiry session ssh {sid}")
        print("\nManage it:")
        print(f"  mimiry session status {sid}")
        print(f"  mimiry session logs {sid} --follow")
        print(f"  mimiry session ssh {sid}")
        print(f"  mimiry session terminate {sid}")
    return 0


# ── session helpers ──


def _preflight_volume_location(
    client: MimiryClient, mounts: list, requested_location: str | None
) -> str | None:
    """Reconcile the session's location with the locations of the volumes it
    mounts, returning the location to use (or ``None`` to leave it as-is).

    A volume lives in one location and the platform refuses to attach it
    anywhere else — but only after the session has been created, so the user
    watches a session appear and die instead of being told upfront. When no
    location was requested, the volume's own location is adopted; when one was
    requested and disagrees, the session is refused before it exists.

    Best-effort in one direction only: a definite mismatch raises, but any
    failure to *read* the volumes (network, unknown name, a payload without a
    location) leaves the request untouched and lets the platform decide.
    """
    names = [m.get("volume_name") for m in mounts if m.get("volume_name")]
    if not names:
        return None

    try:
        volumes = client.list_volumes()
    except Exception:
        return None

    by_name = {v.get("name"): v for v in volumes if isinstance(v, dict) and v.get("name")}
    located: dict[str, str] = {}
    for name in names:
        loc = (by_name.get(name) or {}).get("location")
        if loc:
            located[name] = loc

    if not located:
        return None

    distinct = set(located.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{n} in {loc}" for n, loc in sorted(located.items()))
        raise RuntimeError(
            f"the requested volumes are in different locations ({detail}); a session "
            f"runs in one location and can only mount volumes that live there. "
            f"Attach volumes from a single location, or create the missing one with "
            f"`mimiry volume create --location <location>`."
        )

    volume_location = distinct.pop()
    if requested_location and requested_location != volume_location:
        names_txt = ", ".join(sorted(located))
        raise RuntimeError(
            f"--location {requested_location} conflicts with volume {names_txt}, which "
            f"is in {volume_location}. A volume can only be mounted by a session in its "
            f"own location. Re-run with --location {volume_location}, or create a volume "
            f"in {requested_location} with `mimiry volume create --location "
            f"{requested_location}`."
        )
    return volume_location


def _build_ssh_argv(payload: dict, key_path) -> list[str]:
    target = ssh_target_from_session(payload, key_path)
    return ["ssh", *_common_ssh_opts(target), f"{target.username}@{target.host}"]


def _build_create_payload(args: argparse.Namespace) -> dict:
    gpu_spec: dict[str, object] = {"types": [args.gpu], "count": args.gpu_count}
    if args.provider:
        gpu_spec["provider"] = args.provider
    if args.location:
        gpu_spec["location"] = args.location

    env: dict[str, str] = {}
    for item in args.env or []:
        key, sep, val = item.partition("=")
        if not sep or not key:
            raise RuntimeError(f"--env must be KEY=VALUE (got: {item})")
        env[key] = val

    mounts = []
    for spec in args.volume or []:
        name, sep, path = spec.partition(":")
        if not sep or not name or not path:
            raise RuntimeError(f"--volume must be NAME:MOUNT_PATH (got: {spec})")
        mounts.append({"volume_name": name, "mount_path": path})

    mode = args.auto_terminate or ("on_complete" if args.command else "never")
    name = args.name or f"mimiry-cli-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    payload: dict[str, object] = {
        "name": name,
        "image": {"uri": args.image},
        "gpu": gpu_spec,
        "auto_terminate": {"mode": mode},
        "ssh_enabled": not args.no_ssh,
    }
    if args.command:
        payload["command"] = args.command
    if env:
        payload["environment_vars"] = env
    if mounts:
        payload["volume_mounts"] = mounts
    if not args.no_ssh:
        payload["ssh_public_key"] = _pubkey()
    return payload


def _wait_for_ready(client: MimiryClient, session_id: str) -> dict:
    """Poll until the session is ready to use or reaches a terminal state."""
    cfg = get_config()
    deadline = time.monotonic() + cfg.timeout_seconds
    last = None
    while time.monotonic() < deadline:
        payload = client.get_session(session_id)
        state = _extract_state(payload)
        if state != last:
            print(f"  state={state}", file=sys.stderr)
            last = state
        if state == READY_STATE or state in TERMINAL_STATES:
            return payload
        time.sleep(cfg.poll_interval_seconds)
    return client.get_session(session_id)


def _wait_for_terminal(client: MimiryClient, session_id: str) -> dict:
    """Poll until the session reaches a terminal state."""
    cfg = get_config()
    deadline = time.monotonic() + cfg.timeout_seconds
    last = None
    while time.monotonic() < deadline:
        payload = client.get_session(session_id)
        state = _extract_state(payload)
        if state != last:
            print(f"  state={state}", file=sys.stderr)
            last = state
        if state in TERMINAL_STATES:
            return payload
        time.sleep(cfg.poll_interval_seconds)
    return client.get_session(session_id)


def _follow_logs(client: MimiryClient, session_id: str, *, tail: int, timestamps: bool) -> int:
    """Stream logs by polling until the session reaches a terminal state."""
    cfg = get_config()
    seen = ""
    while True:
        resp = client.get_logs(session_id, tail=max(tail, 1000), timestamps=timestamps)
        status = resp.get("_status", 200)
        if status == 200:
            logs = resp.get("logs") or ""
            if logs.startswith(seen):
                sys.stdout.write(logs[len(seen):])
            else:
                sys.stdout.write(logs)  # log rotated/reset; reprint
            sys.stdout.flush()
            seen = logs
        elif status == 503:
            time.sleep(float(resp.get("retry_after_seconds", cfg.log_poll_interval_seconds)))
            continue
        state = _extract_state(client.get_session(session_id))
        if state in TERMINAL_STATES:
            print(f"\n-- session {state} --", file=sys.stderr)
            return 0
        time.sleep(cfg.log_poll_interval_seconds)


# ────────────────────────── volume management ──────────────────────────


def cmd_volume_list(args: argparse.Namespace) -> int:
    params = {"state_not": "deleted"} if not args.all else {}
    with _client() as c:
        volumes = c.list_volumes(**params)
    if args.json:
        _print_json(volumes)
        return 0
    if not volumes:
        print("No volumes." if args.all else "No active volumes.")
        return 0
    volumes = sorted(volumes, key=lambda v: v.get("created_at") or "", reverse=True)
    rows = [{**v, "attached": v.get("attached_to") or "-"} for v in volumes]
    _print_table(rows, [("ID", "id", 36), ("STATE", "state", 14),
                        ("SIZE_GB", "size_gb", 8), ("ATTACHED", "attached", 36), ("NAME", "name", 0)])
    return 0


def cmd_volume_status(args: argparse.Namespace) -> int:
    with _client() as c:
        _print_json(c.get_volume(args.id))
    return 0


def cmd_volume_create(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"name": args.name, "size_gb": args.size_gb}
    if args.provider:
        payload["provider"] = args.provider
    if args.location:
        payload["location"] = args.location
    with _client() as c:
        _print_json(c.create_volume(payload))
    return 0


def cmd_volume_extend(args: argparse.Namespace) -> int:
    with _client() as c:
        _print_json(c.extend_volume(args.id, args.size_gb))
    return 0


def cmd_volume_delete(args: argparse.Namespace) -> int:
    with _client() as c:
        resp = c.delete_volume(args.id)
    print(f"Delete requested for {args.id}." if resp is None else json.dumps(resp, indent=2))
    return 0


# ────────────────────────── parser ──────────────────────────


def _add_list_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--active", action="store_true",
                   help="Only non-terminal (running/provisioning) sessions.")
    p.add_argument("--limit", type=int, default=20, help="Max rows to show (default 20).")
    p.add_argument("--json", action="store_true", help="Raw JSON output.")
    p.set_defaults(func=cmd_session_list)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mimiry", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"mimiry {__version__}")
    parser.add_argument("--ssh-key", help="Path to SSH private key (overrides MIMIRY_SSH_KEY).")
    parser.add_argument("--api-base", help="API base URL (default: alpha.mimiry.com).")
    subs = parser.add_subparsers(dest="cmd")

    def _cmd_help(_a: argparse.Namespace) -> int:
        parser.print_help()
        return 0

    # account
    subs.add_parser("balance", help="Show account balance.").set_defaults(func=cmd_balance)
    subs.add_parser("quota", help="Show account quota / usage.").set_defaults(func=cmd_quota)
    tx = subs.add_parser("transactions", help="Show credit/debit history.")
    tx.add_argument("--limit", type=int, help="Max records.")
    tx.set_defaults(func=cmd_transactions)
    subs.add_parser("config", help="Show resolved SDK config (no network).").set_defaults(func=cmd_config)
    subs.add_parser("whoami", help="Verify auth and show balance.").set_defaults(func=cmd_whoami)

    avail = subs.add_parser("availability", help="Show GPU availability (no auth).")
    avail.add_argument("--gpu-family", help="Filter, e.g. A100 or H100.")
    avail.add_argument("--provider", help="Filter by provider, e.g. verda.")
    avail.add_argument("--location", help="Filter by location, e.g. FIN-02.")
    avail.add_argument("--min-vram", type=int, metavar="GB", help="Minimum VRAM in GB.")
    avail.add_argument("--available-only", action="store_true", help="Only currently-available GPUs.")
    avail.set_defaults(func=cmd_availability)

    tok = subs.add_parser("token", help="Print a JWT (for debugging).")
    tok.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass the on-disk cache and force a fresh SSH exchange.",
    )
    tok.set_defaults(func=cmd_token)

    subs.add_parser(
        "logout", help="Clear cached JWTs (leaves your SSH key and config intact)."
    ).set_defaults(func=cmd_logout)
    setup_help = "Interactive auth setup wizard (generate/register SSH key, verify)."
    subs.add_parser("setup", help=setup_help).set_defaults(func=cmd_setup)
    subs.add_parser("init", help="Alias for `setup`.").set_defaults(func=cmd_setup)

    # session group
    sess = subs.add_parser("session", help="Manage compute sessions.")
    sess_subs = sess.add_subparsers(dest="session_cmd", required=True)
    _add_list_args(sess_subs.add_parser("list", help="List sessions (newest first)."))

    s_status = sess_subs.add_parser("status", help="Show one session's details.")
    s_status.add_argument("id", help="Session ID.")
    s_status.add_argument("--events", type=int, metavar="N", help="Include last N events (-1 for all).")
    s_status.add_argument("--wait", action="store_true", help="Block until the session is terminal.")
    s_status.set_defaults(func=cmd_session_status)

    s_term = sess_subs.add_parser("terminate", help="Terminate a running session.")
    s_term.add_argument("id", help="Session ID.")
    s_term.set_defaults(func=cmd_session_terminate)

    s_logs = sess_subs.add_parser("logs", help="Fetch a session's container logs.")
    s_logs.add_argument("id", help="Session ID.")
    s_logs.add_argument("--tail", type=int, default=200, help="Lines from the end (default 200).")
    s_logs.add_argument("--timestamps", action="store_true", help="Prefix each line with a timestamp.")
    s_logs.add_argument("--follow", action="store_true", help="Stream until the session ends.")
    s_logs.set_defaults(func=cmd_session_logs)

    s_ssh = sess_subs.add_parser("ssh", help="Open an interactive SSH shell into a session.")
    s_ssh.add_argument("id", help="Session ID.")
    s_ssh.set_defaults(func=cmd_session_ssh)

    s_create = sess_subs.add_parser("create", help="Launch a new GPU session.")
    s_create.add_argument("--image", required=True, help="Container image URI.")
    s_create.add_argument(
        "--gpu",
        required=True,
        help="GPU type, e.g. A100_40G_SXM. Run `mimiry availability` for what is "
             "currently offered. No default: a hard-coded one goes stale the moment "
             "the catalog changes, and every session created without noticing fails.",
    )
    s_create.add_argument("--gpu-count", type=int, default=1, help="GPU count (default 1).")
    s_create.add_argument("--provider", help="Provider hint, e.g. verda.")
    s_create.add_argument(
        "--location",
        help="Location to run in, e.g. FIN-02. Defaults to the location of an "
             "attached --volume, since a volume can only be mounted where it lives.",
    )
    s_create.add_argument("--command", help="Command to run (omit for an interactive box).")
    s_create.add_argument("--name", help="Session name (default auto-generated).")
    s_create.add_argument("--env", action="append", metavar="KEY=VAL", help="Env var (repeatable).")
    s_create.add_argument("--volume", action="append", metavar="NAME:MOUNT",
                          help="Attach a volume at a mount path (repeatable).")
    s_create.add_argument("--auto-terminate", choices=["never", "on_complete", "on_success"],
                          help="Default: on_complete with --command, else never.")
    s_create.add_argument("--no-ssh", action="store_true", help="Disable SSH on the session.")
    s_create.add_argument("--wait", action="store_true", help="Block until the session starts.")
    s_create.set_defaults(func=cmd_session_create)

    # sessions alias
    _add_list_args(subs.add_parser("sessions", help="Alias for `session list`."))

    # `mimiry help` as a friendly alias for `mimiry --help`
    subs.add_parser("help", help="Show this help message.").set_defaults(func=_cmd_help)

    # volume group
    vol = subs.add_parser("volume", help="Manage persistent block volumes.")
    vol_subs = vol.add_subparsers(dest="volume_cmd", required=True)

    v_list = vol_subs.add_parser("list", help="List volumes (hides deleted unless --all).")
    v_list.add_argument("--all", action="store_true", help="Include deleted volumes.")
    v_list.add_argument("--json", action="store_true", help="Raw JSON output.")
    v_list.set_defaults(func=cmd_volume_list)

    v_status = vol_subs.add_parser("status", help="Show one volume's details.")
    v_status.add_argument("id", help="Volume ID.")
    v_status.set_defaults(func=cmd_volume_status)

    v_create = vol_subs.add_parser("create", help="Create a volume.")
    v_create.add_argument("--name", required=True, help="Volume name.")
    v_create.add_argument("--size-gb", type=int, required=True, help="Size in GB.")
    v_create.add_argument("--provider", help="Provider hint.")
    v_create.add_argument(
        "--location",
        help="Location to create the volume in, e.g. FIN-02. Not a hint: a "
             "volume can only be mounted by a session in this same location.",
    )
    v_create.set_defaults(func=cmd_volume_create)

    v_extend = vol_subs.add_parser("extend", help="Grow a volume (cannot shrink).")
    v_extend.add_argument("id", help="Volume ID.")
    v_extend.add_argument("--size-gb", type=int, required=True, help="New size in GB (> current).")
    v_extend.set_defaults(func=cmd_volume_extend)

    v_delete = vol_subs.add_parser("delete", help="Delete a volume (must be detached).")
    v_delete.add_argument("id", help="Volume ID.")
    v_delete.set_defaults(func=cmd_volume_delete)

    args = parser.parse_args(argv)

    # Bare `mimiry` (no subcommand) prints full help instead of erroring.
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

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
