#!/usr/bin/env bash
# Watch a backgrounded pre-push review and capture a forensic snapshot if it
# wedges. Written after a run hung for 3d17h with no timeout and no error:
# the `claude` reviewer child died and the harness's wait never noticed, so
# its documented interrupt-and-resume path never fired.
#
# Stall = no write to the log AND no new report file for $STALL_SECS.
# On stall, dump everything the canonical repo would need to reproduce it.

set -uo pipefail

LOG="${1:?usage: watch-review.sh <logfile> <reports-dir> [stall_secs]}"
REPORTS="${2:?}"
STALL_SECS="${3:-900}"          # 15 min without progress = wedged
POLL=30
SNAP="/tmp/cra-deadlock-$(date +%Y%m%d-%H%M%S).txt"

last_change=$(date +%s)
last_sig=""

progress_sig() {
  local logm rc
  logm=$(stat -c %Y "$LOG" 2>/dev/null || echo 0)
  rc=$(ls -1 "$REPORTS" 2>/dev/null | wc -l)
  echo "${logm}:${rc}"
}

capture() {
  {
    echo "=== CRA pre-push review deadlock — forensic snapshot ==="
    echo "captured:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "stall:     no progress for ${STALL_SECS}s"
    echo
    echo "--- repo / branch ---"
    git -C "$(dirname "$REPORTS")/../.." rev-parse --abbrev-ref HEAD 2>/dev/null
    git -C "$(dirname "$REPORTS")/../.." log --oneline -1 2>/dev/null
    echo
    echo "--- review processes (STAT/WCHAN is the tell) ---"
    ps -eo pid,ppid,lstart,etime,stat,pcpu,wchan:24,cmd | grep -E "pre-push-review|claude" | grep -v grep
    echo
    echo "--- process tree ---"
    for p in $(pgrep -f pre-push-review); do
      echo "pid $p:"
      ls -l "/proc/$p/fd" 2>/dev/null | grep -E "pipe|deleted" | sed 's/^/    /'
      echo "    wchan: $(cat /proc/$p/wchan 2>/dev/null)"
      echo "    children: $(pgrep -P "$p" | tr '\n' ' ')"
    done
    echo
    echo "--- is any reviewer child alive? ---"
    pgrep -af "claude -p" || echo "    NONE — the reviewer is gone; parents are waiting on a dead writer"
    echo
    echo "--- pack progress from the log ---"
    grep -E "^pack [0-9]+/[0-9]+" "$LOG" 2>/dev/null
    echo
    echo "--- reports written ---"
    ls -l --time-style=full-iso "$REPORTS" 2>/dev/null
    echo
    echo "--- log mtime vs now ---"
    echo "log last written: $(stat -c '%y' "$LOG" 2>/dev/null)"
    echo "now:              $(date '+%Y-%m-%d %H:%M:%S.%N %z')"
    echo
    echo "--- log tail (non-JSON) ---"
    grep -vE '^\{|^```' "$LOG" 2>/dev/null | tail -30
  } > "$SNAP" 2>&1
  echo "DEADLOCK: snapshot written to $SNAP"
}

while true; do
  if ! pgrep -f pre-push-review >/dev/null 2>&1; then
    echo "review finished (no pre-push-review process left)"
    exit 0
  fi

  sig=$(progress_sig)
  now=$(date +%s)
  if [ "$sig" != "$last_sig" ]; then
    last_sig="$sig"
    last_change=$now
  elif [ $((now - last_change)) -ge "$STALL_SECS" ]; then
    capture
    exit 42
  fi
  sleep "$POLL"
done
