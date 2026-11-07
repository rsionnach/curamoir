#!/usr/bin/env bash
# flock.sh — the short critical-section mutex for r5-lock.sh and r5-state.sh
# (cm-99l). Sourced, not run.
#
# _r5_flock <file> <who>
#   Takes an exclusive flock(2) on <file> through file descriptor 9 and holds
#   it until the calling shell exits. Waits up to 10s, then exits 3.
#
# Why flock(2) and not a mkdir(2) directory: the kernel drops the lock when
# its holder dies, so a killed process never leaves a mutex behind; there is
# no staleness timeout to wait out, and no stale-clear race in which two
# waiters both "clear" a mutex and both enter. macOS ships no flock(1), so
# lib/r5_flock.py takes the lock on the shell's fd 9: the lock belongs to the
# open file description, which the shell keeps open after the helper exits. A
# child the shell starts inherits fd 9 and keeps the lock alive while it runs:
# callers start only short-lived children (jq, mv, date, python3) while they
# hold it.
_R5_FLOCK_PY="$(dirname -- "${BASH_SOURCE[0]}")/r5_flock.py"
_R5_FLOCK_TIMEOUT=10  # seconds; the one place it is set
_r5_flock() {
  local file="$1" who="$2" rc=0
  command -v python3 >/dev/null 2>&1 || { printf '%s: python3 is required for the mutex\n' "$who" >&2; exit 3; }
  exec 9>>"$file" || { printf '%s: cannot open %s\n' "$who" "$file" >&2; exit 3; }
  python3 "$_R5_FLOCK_PY" 9 "$_R5_FLOCK_TIMEOUT" || rc=$?
  case "$rc" in
    0) ;;
    4) printf '%s: could not lock %s within %ss\n' "$who" "$file" "$_R5_FLOCK_TIMEOUT" >&2; exit 3 ;;
    *) printf '%s: cannot lock %s (r5_flock.py exit %s)\n' "$who" "$file" "$rc" >&2; exit 3 ;;
  esac
}
