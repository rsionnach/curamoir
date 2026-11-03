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
# perl takes the lock on the shell's fd 9: the lock belongs to the open file
# description, which the shell keeps open after perl exits. A child the shell
# starts inherits fd 9 and keeps the lock alive while it runs: callers start
# only short-lived children (jq, mv, date) while they hold it.
_r5_flock() {
  local file="$1" who="$2"
  command -v perl >/dev/null 2>&1 || { printf '%s: perl is required for the mutex\n' "$who" >&2; exit 3; }
  exec 9>>"$file" || { printf '%s: cannot open %s\n' "$who" "$file" >&2; exit 3; }
  perl -MFcntl=:flock -e '
    open(my $fh, ">&=", 9) or exit 3;
    $SIG{ALRM} = sub { exit 4 };
    alarm 10;
    flock($fh, LOCK_EX) or exit 3;
    exit 0' \
    || { printf '%s: could not lock %s within 10s\n' "$who" "$file" >&2; exit 3; }
}
