#!/usr/bin/env python3
"""cra-watch.py — an out-of-process observer for a running pre-push-review.sh.

It attaches to the run through the pointer pre-push-review.sh writes
(<git-dir>/cra-run-current), samples the run's process subtree once a second
through /proc, and every tick prints one heartbeat line per running pack. It
never signals the review, never writes into its scratch directory, and is not
a child of it — so it cannot end, delay or reorder a review, only describe one.

WHAT IT CALLS A FAULT (validated 2026-09-08 against the real harness):
  a pack whose subtree has had NO non-shell process older than one second for
  --stall-ticks consecutive ticks. A healthy pack always has one — the builder,
  the reviewer, or a git/grep/python step — and on seven real packs the longest
  gap measured was 2 s. A spin loop forking mkdir thousands of times a second
  (the 2026-09-08 claim_report wedge) never showed one in 99 s: a 1 Hz sampler
  DOES catch its forks, but never with age >= 1 s. Which fault it is comes from
  CPU: burning -> STALE LOOP; idle -> STALLED (a blocked read, a lost pipe).
  Every fault names the pack's spec, its process chain with wait channels, who
  holds the write end of any pipe the pack shell is blocked on, and which of
  the pack's scratch artifacts exist — enough to place the hang in the script.

WHAT IT ONLY WARNS ABOUT:
  a pack whose reviewer (`claude`) or builder is alive but has been in that
  phase longer than --warn-min / --build-warn-min. `claude -p` prints nothing
  until it finishes, so a live reviewer is opaque by design; a builder is a
  shell script whose legitimate work on a large repo is many short commands,
  so a loop inside it is not separable from an honest long build at this
  distance. Both get a WARN, never a FAULT.

EXIT: 0 the run ended with no fault seen; 42 a fault was found (the diagnostic
is the last thing printed); 3 no run to attach to within --wait seconds. An
agent runs it as a background task: its exit is the ping.
"""
import argparse
import os
import shlex
import signal
import stat as statmod
import subprocess
import sys
import time

HZ = os.sysconf("SC_CLK_TCK")
SHELLS = {"bash", "sh", "dash", "zsh", "ksh"}
# Processes the harness legitimately runs directly under a pack shell. Anything
# else that is stable under a pack is reported (once) as unfamiliar — that is
# the shape of a leaked helper holding a pipe open.
FAMILIAR = {"git", "grep", "rg", "python3", "python", "sed", "awk", "gawk", "sort",
            "head", "tail", "cat", "tee", "sha256sum", "shasum", "cut", "wc", "tr",
            "mkdir", "date", "ls", "find", "xargs", "diff", "uniq", "basename",
            "dirname", "readlink", "stat", "mktemp", "rm", "cp", "mv", "env",
            "timeout", "claude", "node"}
EXIT_FAULT = 42
EXIT_NO_RUN = 3


def read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


def btime():
    for line in (read("/proc/stat") or "").splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return 0


BTIME = btime()


def proc_table():
    """pid -> dict(ppid, comm, state, cpu, start_ticks). One pass over /proc."""
    table = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        s = read("/proc/%s/stat" % d)
        if not s:
            continue
        _, _, rest = s.partition("(")
        comm, _, fields = rest.rpartition(")")
        f = fields.split()
        if len(f) < 20:
            continue
        table[int(d)] = dict(pid=int(d), ppid=int(f[1]), comm=comm, state=f[0],
                             cpu=int(f[11]) + int(f[12]), start=int(f[19]))
    return table


def cmdline(pid):
    c = read("/proc/%d/cmdline" % pid, "rb")
    return c.replace(b"\0", b" ").decode(errors="replace").strip() if c else ""


def wchan(pid):
    w = (read("/proc/%d/wchan" % pid) or "").strip()
    return "" if w == "0" else w          # "0" is the kernel's way of saying "running"


def start_ticks(pid):
    t = proc_table().get(pid)
    return t["start"] if t else None


def tag_of(p):
    if p["comm"] == "claude":
        return "claude"
    if p["comm"] in SHELLS and "build-context-pack" in cmdline(p["pid"]):
        return "builder"
    if p["comm"] in SHELLS:
        return "shell"
    return "other"


def subtree(root, table, kids, exclude=()):
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid not in table or pid in exclude:
            continue
        out.append(table[pid])
        stack.extend(kids.get(pid, []))
    return out


def fd_pipes(pid):
    """(read_pipes, write_pipes) for one pid — pipe names like 'pipe:[123]'."""
    r, w = set(), set()
    base = "/proc/%d/fd" % pid
    try:
        for fd in os.listdir(base):
            p = os.path.join(base, fd)
            try:
                target = os.readlink(p)
                mode = os.lstat(p).st_mode
            except OSError:
                continue
            if not target.startswith("pipe:["):
                continue
            if mode & statmod.S_IRUSR:
                r.add(target)
            if mode & statmod.S_IWUSR:
                w.add(target)
    except OSError:
        pass
    return r, w


def pipe_writers(pipe_name, table):
    holders = []
    for pid in table:
        _, w = fd_pipes(pid)
        if pipe_name in w:
            holders.append(pid)
    return holders


def fmt_age(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def trim(s, n=72):
    return s if len(s) <= n else s[: n - 1] + "…"


class Pack:
    def __init__(self, n, pid, launched, spec):
        self.n, self.pid, self.launched, self.spec = n, pid, launched, spec
        self.start_ticks = None       # pinned at first sight; a reused pid fails this
        self.done = False
        self.done_how = ""
        self.phase = "PRE-BUILD"
        self.phase_since = time.time()
        self.first_seen = time.time()
        self.shell_only_ticks = 0
        self.warned = set()
        self.prev_cpu = {}
        self.tick_cpu = 0
        self.tick_samples = 0
        self.tick_stable = 0
        self.tick_claude = False
        self.tick_builder = False
        self.last_tree = []
        self.unfamiliar = set()


class Watch:
    def __init__(self, args):
        self.a = args
        self.git_dir = None
        self.stamp = self.scratch = self.parent = self.pstart = self.repo = None
        self.packs = {}
        self.run_pack = None          # the top-level pack: pre-pool build, or a single-pack review
        self.faults = []
        self.log_fh = open(args.log, "a") if args.log else None
        self.tick_no = 0

    # ---- output --------------------------------------------------------------
    def say(self, line=""):
        stamp = time.strftime("%H:%M:%S")
        text = "%s  %s" % (stamp, line) if line else ""
        print(text, flush=True)
        if self.log_fh:
            self.log_fh.write(text + "\n")
            self.log_fh.flush()

    def notify(self, headline, body):
        cmd = self.a.notify or os.environ.get("CRA_WATCH_NOTIFY")
        if not cmd:
            return
        # No shell. The headline carries a pack spec — contributor-controlled
        # file names — so it is passed as one argv element, never interpolated
        # (review 20260908-113713 and the mimiry review of the same commit both
        # flagged the list2cmdline form: Windows quoting handed to /bin/sh).
        try:
            subprocess.run(shlex.split(cmd) + [headline], input=body.encode(), timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # ---- attach --------------------------------------------------------------
    def attach(self):
        try:
            self.git_dir = subprocess.check_output(
                ["git", "-C", self.a.repo, "rev-parse", "--absolute-git-dir"],
                stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            print("cra-watch: %s is not a git repository" % self.a.repo, file=sys.stderr)
            return False
        deadline = time.time() + self.a.wait
        told = False
        while True:
            if self.try_pointer():
                return True
            if time.time() >= deadline:
                return False
            if not told:
                self.say("waiting up to %ds for a review to start in %s" % (self.a.wait, self.a.repo))
                told = True
            time.sleep(2)

    def try_pointer(self):
        raw = read(os.path.join(self.git_dir, "cra-run-current"))
        if not raw:
            return False
        parts = raw.rstrip("\n").split("\t")
        if len(parts) < 4:
            return False
        stamp, scratch, pid, pstart = parts[0], parts[1], int(parts[2]), int(parts[3] or 0)
        live = start_ticks(pid)
        if live is None or (pstart and live != pstart):
            return False      # the pointer outlived its run (or a pid was reused)
        self.stamp, self.scratch, self.parent, self.pstart = stamp, scratch, pid, live
        self.repo = parts[4] if len(parts) > 4 else self.a.repo
        return True

    def parent_alive(self):
        return start_ticks(self.parent) == self.pstart

    # ---- manifest ------------------------------------------------------------
    def load_manifest(self):
        raw = read(os.path.join(self.scratch, "pool.tsv"))
        if not raw:
            return
        for line in raw.splitlines():
            f = line.split("\t", 3)
            if len(f) < 4:
                continue
            n = f[0]
            if n not in self.packs:
                self.packs[n] = Pack(n, int(f[1]), int(f[2]), f[3])

    def pack_done(self, pack, table):
        if os.path.exists(os.path.join(self.scratch, "packstat-%s" % pack.n)):
            return "status written"
        p = table.get(pack.pid)
        if p is None:
            return "exited without a status (the run scores it UNDETERMINED)"
        if pack.start_ticks is None:
            # First sight. A pid that started more than a few seconds off the
            # manifest's launch time is a reused pid, not this pack.
            started_epoch = BTIME + p["start"] / HZ
            if abs(started_epoch - pack.launched) > 5:
                return "pid reused by another process; pack is gone"
            pack.start_ticks = p["start"]
        elif p["start"] != pack.start_ticks:
            return "pid reused by another process; pack is gone"
        return None

    # ---- sampling ------------------------------------------------------------
    def sample(self):
        table = proc_table()
        kids = {}
        for p in table.values():
            kids.setdefault(p["ppid"], []).append(p["pid"])
        self.load_manifest()
        live_roots = []
        for pack in self.packs.values():
            if pack.done:
                continue
            how = self.pack_done(pack, table)
            if how:
                pack.done, pack.done_how = True, how
                continue
            live_roots.append(pack.pid)
            self.observe(pack, subtree(pack.pid, table, kids))
        # The top-level pack: everything under the parent that is not a pack. It
        # is judged only while no pack is running — before the pool starts (the
        # first build, or the whole of a single-pack review) and after it ends.
        if not live_roots and self.parent in table:
            if self.run_pack is None:
                self.run_pack = Pack("run", self.parent, 0, "(top level: the single pack, or the plan build)")
                self.run_pack.start_ticks = self.pstart
            tree = subtree(self.parent, table, kids, exclude=set(live_roots))
            # The parent shell itself is the coordinator, not a worker: drop it.
            self.observe(self.run_pack, [p for p in tree if p["pid"] != self.parent])
        elif self.run_pack is not None and live_roots:
            self.run_pack.shell_only_ticks = 0
        return table

    def observe(self, pack, tree):
        now_cpu = {}
        stable = False
        uptime = float((read("/proc/uptime") or "0").split()[0])
        for p in tree:
            p["tag"] = tag_of(p)
            p["age"] = uptime - p["start"] / HZ
            now_cpu[p["pid"]] = p["cpu"]
        # Whatever a live reviewer spawns is its own business (its tools: rg, git,
        # cat, ...): "unfamiliar" is only meaningful where the harness itself is
        # the one forking.
        reviewing = any(p["tag"] == "claude" for p in tree)
        for p in tree:
            if p["tag"] != "shell" and p["age"] >= 1.0:
                stable = True
                if (not reviewing and p["tag"] == "other" and p["comm"] not in FAMILIAR
                        and p["pid"] not in pack.unfamiliar):
                    pack.unfamiliar.add(p["pid"])
                    self.say("pack %s: unfamiliar process under it — %s[%d] (%s, %s)" % (
                        pack.n, p["comm"], p["pid"], wchan(p["pid"]), fmt_age(p["age"])))
            if p["tag"] == "claude":
                pack.tick_claude = True
            if p["tag"] == "builder":
                pack.tick_builder = True
        delta = 0
        for pid, c in now_cpu.items():
            prev = pack.prev_cpu.get(pid)
            delta += (c - prev) if prev is not None and c >= prev else c
        pack.prev_cpu = now_cpu
        pack.tick_cpu += delta
        pack.tick_samples += 1
        pack.tick_stable += 1 if stable else 0
        pack.last_tree = tree

    # ---- per tick ------------------------------------------------------------
    def tick(self):
        self.tick_no += 1
        done = [p for p in self.packs.values() if p.done]
        running = [p for p in self.packs.values() if not p.done]
        judged = list(running)
        if self.run_pack is not None and not running:
            judged.append(self.run_pack)
        lines, faults, warns = [], [], []
        for pack in judged:
            if pack.tick_samples == 0:
                continue
            if pack.tick_claude:
                phase = "REVIEWING"
            elif pack.tick_builder:
                phase = "BUILDING"
            elif pack.phase == "REVIEWING":
                phase = "POST-REVIEW"
            elif pack.phase in ("BUILDING", "POST-BUILD"):
                phase = "POST-BUILD"
            else:
                phase = "PRE-BUILD"
            if phase != pack.phase:
                pack.phase, pack.phase_since = phase, time.time()
            presence = pack.tick_stable / pack.tick_samples
            cpu_pct = 100.0 * pack.tick_cpu / HZ / max(1, pack.tick_samples)
            if presence < 0.05:
                pack.shell_only_ticks += 1
            else:
                pack.shell_only_ticks = 0
            phase_age = time.time() - pack.phase_since
            total_age = time.time() - pack.first_seen
            worker = ""
            for p in pack.last_tree:
                if p["tag"] in ("claude", "builder"):
                    worker = "%s[%d]" % (p["tag"], p["pid"])
                    break
            lines.append("            pack %-4s %-11s %6s  %-16s %s" % (
                pack.n, phase, fmt_age(total_age), worker, trim(pack.spec)))
            # FAULT: nothing real has run under this pack for stall-ticks ticks.
            if pack.shell_only_ticks >= self.a.stall_ticks:
                kind = "STALE LOOP" if cpu_pct >= self.a.cpu_pct else "STALLED"
                faults.append((pack, kind, cpu_pct))
                continue
            # WARN: alive and working, but for longer than this harness has ever needed.
            limit = self.a.build_warn_min if phase == "BUILDING" else self.a.warn_min
            key = (phase, int(phase_age // (limit * 60)))
            if phase_age >= limit * 60 and key not in pack.warned:
                pack.warned.add(key)
                warns.append("pack %s has been %s for %s (spec: %s) — %s" % (
                    pack.n, phase, fmt_age(phase_age), trim(pack.spec, 100),
                    "a live reviewer prints nothing until it finishes, so this may be a slow review or a wedged one; look at the process" if phase == "REVIEWING"
                    else "far longer than a build has ever taken here; a loop inside the builder would look exactly like this"))
            pack.tick_cpu = pack.tick_samples = pack.tick_stable = 0
            pack.tick_claude = pack.tick_builder = False
        total = len(self.packs)
        if total:
            head = "run %s — %d packs: %d done, %d running" % (self.stamp, total, len(done), len(running))
        else:
            head = "run %s — single pack (no pool yet)" % self.stamp
        if faults:
            self.say(head + " — *** FAULT ***")
        elif warns:
            self.say(head + " — WARNING")
        else:
            self.say(head + " — all healthy")
        for l in lines:
            self.say(l)
        for w in warns:
            self.say("*** WARN: " + w)
            self.notify("cra-watch WARN: " + w.split(" (spec")[0], w)
        for pack, kind, cpu_pct in faults:
            self.fault(pack, kind, cpu_pct)
        return bool(faults)

    # ---- the diagnostic --------------------------------------------------------
    def fault(self, pack, kind, cpu_pct):
        table = proc_table()
        gap = fmt_age(pack.shell_only_ticks * self.a.tick)
        out = []
        out.append("*** pack %s FAULT: %s — %s with no process surviving a second under it; subtree %s" % (
            pack.n, kind, gap,
            ("burning %.1f%% CPU (a loop that forks and reaps, or spins, without ever finishing anything)" % cpu_pct)
            if kind == "STALE LOOP" else "idle (blocked: a read on a pipe or file nothing will ever write)"))
        out.append("    spec:  %s" % pack.spec)
        out.append("    phase: %s for %s; pack age %s" % (pack.phase, fmt_age(time.time() - pack.phase_since),
                                                          fmt_age(time.time() - pack.first_seen)))
        out.append("    tree:")
        by_pid = {p["pid"]: p for p in pack.last_tree}
        depth = {}
        for p in sorted(pack.last_tree, key=lambda q: q["pid"]):
            d = 0
            q = p
            while q["ppid"] in by_pid:
                d += 1
                q = by_pid[q["ppid"]]
            depth[p["pid"]] = d
            out.append("      %s%d %-8s %-18s age %-7s cpu %ds%s" % (
                "  " * d, p["pid"], p["comm"], wchan(p["pid"]) or p["state"], fmt_age(p.get("age", 0)),
                p["cpu"] // HZ, "   <- pack shell" if p["pid"] == pack.pid else ""))
        # Who holds the write end of any pipe a shell in this pack is blocked reading?
        for p in pack.last_tree:
            if "pipe" not in wchan(p["pid"]):
                continue
            rpipes, _ = fd_pipes(p["pid"])
            for name in sorted(rpipes):
                holders = [h for h in pipe_writers(name, table) if h != p["pid"]]
                if not holders:
                    out.append("    pipe:  %d is blocked reading %s and NO process holds its write end (a race the kernel will resolve)" % (p["pid"], name))
                for h in holders:
                    hp = table.get(h, {})
                    out.append("    pipe:  %d is blocked reading %s — write end held by %d (%s, %s%s)" % (
                        p["pid"], name, h, hp.get("comm", "?"), wchan(h) or hp.get("state", "?"),
                        ", inside this pack" if h in by_pid else ", OUTSIDE this pack"))
        # Which of the pack's scratch artifacts exist places the hang in the script.
        n = pack.n
        arts = [("packlog-%s" % n, "pack_tickets ran"),
                ("tickcand-%s" % n, "pack_tickets listed the diff"),
                ("pack-%s-%s.md" % (self.stamp, n), "the builder wrote the pack"),
                ("packstat-%s" % n, "the pack finished")]
        have = []
        for fname, meaning in arts:
            path = os.path.join(self.scratch, fname)
            try:
                st = os.stat(path)
                have.append("%s (%d B, %s ago)" % (fname, st.st_size, fmt_age(time.time() - st.st_mtime)))
            except OSError:
                have.append("%s MISSING" % fname)
        out.append("    artifacts: " + "; ".join(have))
        if pack.n == "run":
            reading = "the top-level pack hung: single-pack build or review, or the plan build before the pool"
        elif not os.path.exists(os.path.join(self.scratch, "packlog-%s" % n)):
            reading = ("packlog-%s was never created, so this pack never reached pack_tickets: it hung in "
                       "run_one_pack's opening command substitutions (claim_report / the slug)" % n)
        elif not os.path.exists(os.path.join(self.scratch, "pack-%s-%s.md" % (self.stamp, n))):
            reading = "pack_tickets ran but no pack file exists: it hung in pack_key, ledger_lookup or the builder"
        elif not os.path.exists(os.path.join(self.scratch, "packstat-%s" % n)):
            reading = "the pack was built but no status was written: it hung in or after the reviewer (review_pack, extraction, ledger_append)"
        else:
            reading = "status exists — this should not be judged; report it"
        out.append("    reading: " + reading)
        out.append("    the review has NOT been touched: %s (pid %d) is still where it was. Kill it or interrupt the push to recover." % (
            "the pack" if pack.n != "run" else "the run", pack.pid))
        body = "\n".join(out)
        for l in out:
            self.say(l)
        self.faults.append((pack.n, kind))
        self.notify("cra-watch FAULT: pack %s %s — %s" % (pack.n, kind, trim(pack.spec, 60)), body)

    # ---- main loop -------------------------------------------------------------
    def run(self):
        if not self.attach():
            print("cra-watch: no live review found via %s/cra-run-current" % self.git_dir, file=sys.stderr)
            return EXIT_NO_RUN
        self.say("attached to run %s (parent pid %d, scratch %s); tick %ds, fault after %d shell-only ticks, warn at %dm review / %dm build" % (
            self.stamp, self.parent, self.scratch, self.a.tick, self.a.stall_ticks, self.a.warn_min, self.a.build_warn_min))
        next_tick = time.time() + self.a.tick
        while True:
            if not self.parent_alive():
                self.sample_final()
                return EXIT_FAULT if self.faults else 0
            self.sample()
            if time.time() >= next_tick or self.a.once:
                faulted = self.tick()
                if self.a.once:
                    return EXIT_FAULT if faulted else 0
                if faulted and not self.a.keep_going:
                    return EXIT_FAULT
                next_tick = time.time() + self.a.tick
            time.sleep(1)

    def sample_final(self):
        done = sum(1 for p in self.packs.values() if p.done)
        self.say("run %s ended (parent pid %d gone): %d/%d packs recorded done before it left%s" % (
            self.stamp, self.parent, done, len(self.packs),
            "; faults seen: " + ", ".join("pack %s %s" % f for f in self.faults) if self.faults else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository whose review to watch (default: cwd)")
    ap.add_argument("--tick", type=int, default=60, help="seconds between heartbeats (default 60)")
    ap.add_argument("--stall-ticks", type=int, default=3, help="consecutive shell-only ticks before a FAULT (default 3)")
    ap.add_argument("--cpu-pct", type=float, default=2.0, help="subtree CPUpct separating STALE LOOP from STALLED (default 2)")
    ap.add_argument("--warn-min", type=int, default=35, help="WARN when a pack has been reviewing this long (default 35)")
    ap.add_argument("--build-warn-min", type=int, default=10, help="WARN when a pack has been building this long (default 10)")
    ap.add_argument("--wait", type=int, default=120, help="seconds to wait for a run to appear (default 120)")
    ap.add_argument("--log", help="also append every line to this file")
    ap.add_argument("--notify", help="command to run on WARN/FAULT, or CRA_WATCH_NOTIFY. Split like a shell "
                    "would (shlex) and run WITHOUT one: the headline is appended as a single extra argument, "
                    "the detail arrives on stdin, and shell syntax (pipes, redirects, $VARS) is NOT interpreted "
                    "— wrap those in a script. e.g. --notify 'notify-send cra-watch'")
    ap.add_argument("--keep-going", action="store_true", help="report faults but keep watching until the run ends")
    ap.add_argument("--once", action="store_true", help="one sample, one heartbeat, exit")
    args = ap.parse_args()
    if not os.path.isdir("/proc"):
        print("cra-watch: needs a Linux /proc", file=sys.stderr)
        return 2
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return Watch(args).run()


if __name__ == "__main__":
    sys.exit(main())
