#!/usr/bin/env bash
# r5-lock.sh — the run-level mutex for curamoir's R5 supervisor
# (r5-supervise) and epic-executor. Ported from
# nthlayer-ecosystem/.claude/hooks/r5-lock.sh (cm-99l).
#
# The two skills are alternative supervisors over the same primitive
# (rule-of-five-review). They must never nest and never run concurrently in
# this repository. Acquire on start, refresh before every subagent dispatch,
# release on every exit path.
#
# Storage: <R5_ROOT>/.claude/r5-supervisor.lock, where R5_ROOT is the main
# worktree of this repository (lib/resolve-repo-root.sh), so a sibling
# worktree contends for the same lock.
#
# Lock shape (JSON):
#   {"supervisor", "target", "session_id", "acquired_at", "refreshed_at"}
#
# Subcommands:
#   acquire <supervisor> <target> [session_id]   - claim the lock
#   refresh <supervisor> [session_id]            - bump refreshed_at
#   release <supervisor> [session_id]            - drop the lock
#   status                                       - print holder or "no lock"
#
# session_id defaults to $CLAUDE_CODE_SESSION_ID when omitted.
#
# Exit codes:
#   0   - success
#   1   - usage error, or refresh/release called by a non-holder
#   2   - cross-session block, or same-session nesting refusal
#   3   - internal error (cannot resolve the root, jq missing)

set -uo pipefail

# Lease window: refreshed_at older than this is reclaimable by another
# supervisor (with a warning). Tune up if a real dispatch ever exceeds
# this; do not weaken the per-dispatch refresh cadence.
LEASE_SECONDS=900

# Absolute backstop: acquired_at older than this is reclaimable even if
# refreshed_at looks fresh (defends against a runaway refresh loop on a
# wedged session).
ABSOLUTE_TTL_SECONDS=86400  # 24h

# Resolve R5_ROOT via the shared resolver.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_SCRIPT_DIR/lib/resolve-repo-root.sh" || {
  printf 'r5-lock: cannot resolve the repository root\n' >&2
  exit 3
}

LOCK_FILE="$R5_ROOT/.claude/r5-supervisor.lock"
LOCK_DIR="$(dirname "$LOCK_FILE")"
mkdir -p "$LOCK_DIR" || { printf 'r5-lock: cannot create %s\n' "$LOCK_DIR" >&2; exit 3; }

# jq is required for safe JSON read/write.
command -v jq >/dev/null 2>&1 || { printf 'r5-lock: jq is required\n' >&2; exit 3; }

# Default session id from env when caller omits it.
_default_session() {
  if [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
    printf '%s' "$CLAUDE_CODE_SESSION_ID"
  elif [[ -n "${CLAUDE_SESSION_ID:-}" ]]; then
    printf '%s' "$CLAUDE_SESSION_ID"
  else
    printf ''
  fi
}

_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Parse an ISO-8601 Z timestamp to epoch seconds. Returns 0 on bad input.
_epoch() {
  local ts="${1:-}"
  [[ -z "$ts" ]] && { printf '0'; return; }
  # macOS BSD date wants -j -f; GNU date wants -d. Try GNU first, fall
  # back to BSD; if neither parses, return 0 so callers treat as stale.
  date -u -d "$ts" +%s 2>/dev/null || \
    date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null || \
    printf '0'
}

# Read a field from the lockfile (printing empty on any failure).
_lock_get() {
  local field="$1"
  [[ -f "$LOCK_FILE" ]] || { printf ''; return; }
  jq -r --arg f "$field" '.[$f] // ""' "$LOCK_FILE" 2>/dev/null
}

# Atomic write: write to temp file, fsync via mv. set -C ensures the
# initial creation is exclusive (no clobber); subsequent updates are
# unconditional rewrites by the same holder.
_lock_write() {
  local supervisor="$1" target="$2" session="$3" acquired_at="$4" refreshed_at="$5"
  local tmp="$LOCK_FILE.tmp.$$"
  jq -n \
    --arg supervisor "$supervisor" \
    --arg target "$target" \
    --arg session_id "$session" \
    --arg acquired_at "$acquired_at" \
    --arg refreshed_at "$refreshed_at" \
    '{supervisor:$supervisor, target:$target, session_id:$session_id, acquired_at:$acquired_at, refreshed_at:$refreshed_at}' \
    > "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$LOCK_FILE"
}

# ---------------------------------------------------------------- acquire
_cmd_acquire() {
  local supervisor="${1:-}" target="${2:-}" session="${3:-}"
  [[ -z "$supervisor" || -z "$target" ]] && {
    printf 'usage: r5-lock.sh acquire <supervisor> <target> [session_id]\n' >&2
    exit 1
  }
  [[ -z "$session" ]] && session="$(_default_session)"
  [[ -z "$session" ]] && {
    printf 'r5-lock: no session id (CLAUDE_CODE_SESSION_ID not set, none passed)\n' >&2
    exit 1
  }

  local now="$(_now)"

  # No existing lock — fresh acquire.
  if [[ ! -f "$LOCK_FILE" ]]; then
    _lock_write "$supervisor" "$target" "$session" "$now" "$now"
    exit 0
  fi

  # Lock exists — inspect holder.
  local held_supervisor held_session held_target held_refreshed held_acquired
  held_supervisor="$(_lock_get supervisor)"
  held_session="$(_lock_get session_id)"
  held_target="$(_lock_get target)"
  held_refreshed="$(_lock_get refreshed_at)"
  held_acquired="$(_lock_get acquired_at)"

  # Same session + same supervisor => idempotent re-acquire (bumps refreshed_at).
  if [[ "$held_session" == "$session" && "$held_supervisor" == "$supervisor" ]]; then
    _lock_write "$supervisor" "$target" "$session" "$held_acquired" "$now"
    exit 0
  fi

  # Same session + different supervisor => nesting, refuse.
  if [[ "$held_session" == "$session" && "$held_supervisor" != "$supervisor" ]]; then
    cat >&2 <<EOF
r5-lock: don't nest supervisors.
  $held_supervisor is in flight on $held_target in this session.
  Do not start $supervisor on $target inside the same session — the two
  supervisors are alternatives, not layers. Finish or abort the current
  run first.
EOF
    exit 2
  fi

  # Different session — check lease + absolute TTL for stale reclaim.
  local now_epoch refreshed_epoch acquired_epoch refresh_age acquire_age
  now_epoch=$(date -u +%s)
  refreshed_epoch=$(_epoch "$held_refreshed")
  acquired_epoch=$(_epoch "$held_acquired")
  refresh_age=$(( now_epoch - refreshed_epoch ))
  acquire_age=$(( now_epoch - acquired_epoch ))

  if (( refresh_age > LEASE_SECONDS )); then
    printf 'r5-lock: stale lease (refreshed_at %ss ago > %ss), reclaiming from %s/%s in session %s\n' \
      "$refresh_age" "$LEASE_SECONDS" "$held_supervisor" "$held_target" "$held_session" >&2
    _lock_write "$supervisor" "$target" "$session" "$now" "$now"
    exit 0
  fi

  if (( acquire_age > ABSOLUTE_TTL_SECONDS )); then
    printf 'r5-lock: absolute TTL exceeded (acquired %ss ago > %ss), reclaiming from %s/%s in session %s\n' \
      "$acquire_age" "$ABSOLUTE_TTL_SECONDS" "$held_supervisor" "$held_target" "$held_session" >&2
    _lock_write "$supervisor" "$target" "$session" "$now" "$now"
    exit 0
  fi

  # Lease fresh, held by someone else — block.
  cat >&2 <<EOF
r5-lock: held by another supervisor.
  supervisor: $held_supervisor
  target:     $held_target
  session:    $held_session
  refreshed:  $held_refreshed (${refresh_age}s ago)

If this state is stale (the prior supervisor crashed or the session was
abandoned), the lease will auto-reclaim in $(( LEASE_SECONDS - refresh_age ))s.
Manual override: rm "$LOCK_FILE"
EOF
  exit 2
}

# ---------------------------------------------------------------- refresh
_cmd_refresh() {
  local supervisor="${1:-}" session="${2:-}"
  [[ -z "$supervisor" ]] && {
    printf 'usage: r5-lock.sh refresh <supervisor> [session_id]\n' >&2
    exit 1
  }
  [[ -z "$session" ]] && session="$(_default_session)"

  [[ -f "$LOCK_FILE" ]] || {
    printf 'r5-lock: refresh: no lockfile (you do not hold the lock)\n' >&2
    exit 1
  }
  local held_supervisor held_session held_target held_acquired
  held_supervisor="$(_lock_get supervisor)"
  held_session="$(_lock_get session_id)"
  held_target="$(_lock_get target)"
  held_acquired="$(_lock_get acquired_at)"

  if [[ "$held_supervisor" != "$supervisor" || "$held_session" != "$session" ]]; then
    printf 'r5-lock: refresh: lock held by %s/%s, not %s/%s\n' \
      "$held_supervisor" "$held_session" "$supervisor" "$session" >&2
    exit 1
  fi

  _lock_write "$supervisor" "$held_target" "$session" "$held_acquired" "$(_now)"
  exit 0
}

# ---------------------------------------------------------------- release
_cmd_release() {
  local supervisor="${1:-}" session="${2:-}"
  [[ -z "$supervisor" ]] && {
    printf 'usage: r5-lock.sh release <supervisor> [session_id]\n' >&2
    exit 1
  }
  [[ -z "$session" ]] && session="$(_default_session)"

  [[ -f "$LOCK_FILE" ]] || exit 0  # no lock, nothing to release

  local held_supervisor held_session
  held_supervisor="$(_lock_get supervisor)"
  held_session="$(_lock_get session_id)"

  if [[ "$held_supervisor" != "$supervisor" || "$held_session" != "$session" ]]; then
    printf 'r5-lock: release: lock held by %s/%s, not %s/%s — refusing to drop\n' \
      "$held_supervisor" "$held_session" "$supervisor" "$session" >&2
    exit 1
  fi

  rm -f "$LOCK_FILE"
  exit 0
}

# ---------------------------------------------------------------- status
_cmd_status() {
  if [[ ! -f "$LOCK_FILE" ]]; then
    printf 'no lock\n'
    exit 0
  fi
  cat "$LOCK_FILE"
  printf '\n'
  exit 0
}

# Dispatch.
case "${1:-}" in
  acquire) shift; _cmd_acquire "$@" ;;
  refresh) shift; _cmd_refresh "$@" ;;
  release) shift; _cmd_release "$@" ;;
  status)  shift; _cmd_status ;;
  *)
    cat >&2 <<EOF
usage: r5-lock.sh <subcommand> [args]
  acquire <supervisor> <target> [session_id]
  refresh <supervisor> [session_id]
  release <supervisor> [session_id]
  status

session_id defaults to \$CLAUDE_CODE_SESSION_ID.
Lock file: $LOCK_FILE
EOF
    exit 1
    ;;
esac
