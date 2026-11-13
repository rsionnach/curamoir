#!/usr/bin/env bash
# lease.sh — the R5 run lock's lease rule and the session id, shared by
# r5-lock.sh, r5-state.sh and r5-parallel-block.sh (cm-99l). Sourced, not
# run; requires jq.
#
# A lock is live while it was refreshed within R5_LEASE_SECONDS and acquired
# within R5_ABSOLUTE_TTL_SECONDS. Past either, its holder is presumed gone:
# another session may reclaim the lock, and the hook stops refusing other
# sessions on its behalf.

# Tune up if a real dispatch ever takes longer than this; do not weaken the
# refresh before every dispatch.
R5_LEASE_SECONDS=900
# A backstop against a wedged session that keeps refreshing.
R5_ABSOLUTE_TTL_SECONDS=86400

# How another session recovers a run it does not own: one wording for every
# message that refuses it (r5-lock.sh, r5-state.sh, the hook).
R5_ADOPT_HINT='take the run over: hold the R5 lock in this session (r5-lock.sh acquire r5-supervise <bead>, unless it already holds the lock for another supervisor), then r5-state.sh adopt; to end it instead, adopt it first, then r5-state.sh finish aborted'

# _r5_session: this session's id, from the environment ("" if unknown).
_r5_session() { printf '%s' "${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"; }

# _r5_epoch <ISO-8601 Z timestamp>: epoch seconds, or 0 (which reads as
# long expired) if the timestamp is empty or unparseable. GNU date takes
# -d; BSD (macOS) date takes -j -f.
_r5_epoch() {
  local ts="${1:-}"
  [[ -n "$ts" ]] || { printf '0'; return; }
  date -u -d "$ts" +%s 2>/dev/null \
    || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null \
    || printf '0'
}

# _r5_lock_ages <lockfile>: prints "<seconds since refresh> <seconds since
# acquire>".
_r5_lock_ages() {
  local now
  now=$(date -u +%s)
  printf '%s %s' \
    $(( now - $(_r5_epoch "$(jq -r '.refreshed_at // ""' "$1" 2>/dev/null)") )) \
    $(( now - $(_r5_epoch "$(jq -r '.acquired_at // ""' "$1" 2>/dev/null)") ))
}

# _r5_lease_live <lockfile>: true while the lock's lease holds. A negative
# age (a timestamp in the future: a clock set wrong, then corrected) counts
# as expired, so a bad clock cannot hold the lock indefinitely.
_r5_lease_live() {
  local refresh_age acquire_age
  read -r refresh_age acquire_age <<< "$(_r5_lock_ages "$1")"
  (( refresh_age >= 0 && acquire_age >= 0
     && refresh_age <= R5_LEASE_SECONDS && acquire_age <= R5_ABSOLUTE_TTL_SECONDS ))
}
