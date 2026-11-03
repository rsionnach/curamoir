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
# Storage: $R5_LOCK_FILE, <R5_ROOT>/.claude/r5-supervisor.lock, where
# R5_ROOT is the main worktree of this repository (lib/resolve-repo-root.sh),
# so a sibling worktree contends for the same lock. The lease rule is in
# lib/lease.sh.
#
# Concurrency: acquire, refresh and release each run under a short kernel
# mutex (lib/flock.sh), which is what makes every read-decide-write atomic:
# of two sessions acquiring (or reclaiming) at once, exactly one wins. The
# link(2) create and the reclaim re-check below are defence in depth: they
# keep that guarantee if a caller ever bypasses the mutex.
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
# session_id defaults to $CLAUDE_CODE_SESSION_ID (or $CLAUDE_SESSION_ID)
# when omitted.
#
# Exit codes:
#   0   - success
#   1   - usage error, or refresh/release called by a non-holder
#   2   - cross-session block, or same-session nesting refusal
#   3   - internal error (cannot resolve the root, jq or perl missing, the
#         .claude directory cannot be created, the mutex not taken within
#         10s, a lock write failed)

set -uo pipefail

# Resolve R5_ROOT via the shared resolver.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_SCRIPT_DIR/lib/resolve-repo-root.sh" || {
  printf 'r5-lock: cannot resolve the repository root\n' >&2
  exit 3
}
# shellcheck source=lib/flock.sh
source "$_SCRIPT_DIR/lib/flock.sh"
# shellcheck source=lib/lease.sh
source "$_SCRIPT_DIR/lib/lease.sh"

LOCK_FILE="$R5_LOCK_FILE"
LOCK_DIR="$(dirname "$LOCK_FILE")"
mkdir -p "$LOCK_DIR" || { printf 'r5-lock: cannot create %s\n' "$LOCK_DIR" >&2; exit 3; }

# jq is required for safe JSON read/write.
command -v jq >/dev/null 2>&1 || { printf 'r5-lock: jq is required\n' >&2; exit 3; }

_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Read a field from the lockfile (printing empty on any failure).
_lock_get() {
  local field="$1"
  [[ -f "$LOCK_FILE" ]] || { printf ''; return; }
  jq -r --arg f "$field" '.[$f] // ""' "$LOCK_FILE" 2>/dev/null
}

# The lock body for a holder, written to a temp file whose path is printed.
_lock_body() {
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
  printf '%s' "$tmp"
}

# A holder's rewrite (refresh, idempotent re-acquire): the lock is already
# ours, so an atomic replace is enough. Returns non-zero if it failed.
_lock_write() {
  local tmp
  tmp="$(_lock_body "$@")" || return 1
  mv "$tmp" "$LOCK_FILE" || { rm -f "$tmp"; return 1; }
}

# Taking the lock when nobody holds it: link(2) fails if the lock already
# exists (defence in depth under the mutex; see the header). Returns 1 if
# someone else got there first.
_lock_create() {
  local tmp
  tmp="$(_lock_body "$@")" || return 1
  if ln "$tmp" "$LOCK_FILE" 2>/dev/null; then
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

_lost_race() {
  printf 'r5-lock: another session took the lock at the same moment; it is held now\n' >&2
  exit 2
}

# Reclaiming a stale lock, then exiting: only if it is still the stale lock
# we read (defence in depth under the mutex), with the same atomic create as
# a fresh acquire.
_reclaim_and_exit() {
  local supervisor="$1" target="$2" session="$3" now="$4" stale_refreshed="$5"
  [[ "$(_lock_get refreshed_at)" == "$stale_refreshed" ]] || _lost_race
  rm -f "$LOCK_FILE"
  _lock_create "$supervisor" "$target" "$session" "$now" "$now" || _lost_race
  exit 0
}

# ---------------------------------------------------------------- acquire
_cmd_acquire() {
  local supervisor="${1:-}" target="${2:-}" session="${3:-}"
  [[ -z "$supervisor" || -z "$target" ]] && {
    printf 'usage: r5-lock.sh acquire <supervisor> <target> [session_id]\n' >&2
    exit 1
  }
  [[ -z "$session" ]] && session="$(_r5_session)"
  [[ -z "$session" ]] && {
    printf 'r5-lock: no session id (CLAUDE_CODE_SESSION_ID not set, none passed)\n' >&2
    exit 1
  }

  local now="$(_now)"

  # No existing lock — fresh acquire.
  if [[ ! -f "$LOCK_FILE" ]]; then
    _lock_create "$supervisor" "$target" "$session" "$now" "$now" || _lost_race
    exit 0
  fi

  # Lock exists — inspect holder.
  local held_supervisor held_session held_target held_refreshed held_acquired
  held_supervisor="$(_lock_get supervisor)"
  held_session="$(_lock_get session_id)"
  held_target="$(_lock_get target)"
  held_refreshed="$(_lock_get refreshed_at)"
  held_acquired="$(_lock_get acquired_at)"

  # Same session + same supervisor + same target => idempotent re-acquire
  # (bumps refreshed_at). The same supervisor on ANOTHER target is a second
  # run while one is in flight: refused, never a silent retarget.
  if [[ "$held_session" == "$session" && "$held_supervisor" == "$supervisor" ]]; then
    if [[ "$held_target" != "$target" ]]; then
      printf 'r5-lock: %s already holds the lock for %s in this session; finish or abort that run before %s\n' \
        "$supervisor" "$held_target" "$target" >&2
      exit 2
    fi
    _lock_write "$supervisor" "$target" "$session" "$held_acquired" "$now" || exit 3
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
  local refresh_age acquire_age
  read -r refresh_age acquire_age <<< "$(_r5_lock_ages "$LOCK_FILE")"

  if (( refresh_age > R5_LEASE_SECONDS )); then
    printf 'r5-lock: stale lease (refreshed_at %ss ago > %ss), reclaiming from %s/%s in session %s\n' \
      "$refresh_age" "$R5_LEASE_SECONDS" "$held_supervisor" "$held_target" "$held_session" >&2
    _reclaim_and_exit "$supervisor" "$target" "$session" "$now" "$held_refreshed"
  fi

  if (( acquire_age > R5_ABSOLUTE_TTL_SECONDS )); then
    printf 'r5-lock: absolute TTL exceeded (acquired %ss ago > %ss), reclaiming from %s/%s in session %s\n' \
      "$acquire_age" "$R5_ABSOLUTE_TTL_SECONDS" "$held_supervisor" "$held_target" "$held_session" >&2
    _reclaim_and_exit "$supervisor" "$target" "$session" "$now" "$held_refreshed"
  fi

  # Lease fresh, held by someone else — block.
  cat >&2 <<EOF
r5-lock: held by another supervisor.
  supervisor: $held_supervisor
  target:     $held_target
  session:    $held_session
  refreshed:  $held_refreshed (${refresh_age}s ago)

If this state is stale (the prior supervisor crashed or the session was
abandoned), the lease will auto-reclaim in $(( R5_LEASE_SECONDS - refresh_age ))s.
Do not remove the lock by hand: an R5 run in .claude/r5-state.json would then
refuse every other session's subagents. Once the lease has lapsed, acquire
the lock and take the run over (r5-state.sh adopt), or end it
(r5-state.sh finish aborted).
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
  [[ -z "$session" ]] && session="$(_r5_session)"

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

  _lock_write "$supervisor" "$held_target" "$session" "$held_acquired" "$(_now)" || exit 3
  exit 0
}

# ---------------------------------------------------------------- release
_cmd_release() {
  local supervisor="${1:-}" session="${2:-}"
  [[ -z "$supervisor" ]] && {
    printf 'usage: r5-lock.sh release <supervisor> [session_id]\n' >&2
    exit 1
  }
  [[ -z "$session" ]] && session="$(_r5_session)"

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

# The mutex (see the header): held for milliseconds, dropped by the kernel if
# its holder is killed.
_with_mutex() {
  _r5_flock "$LOCK_FILE.flock" r5-lock
  "$@"
}

# Dispatch.
case "${1:-}" in
  acquire) shift; _with_mutex _cmd_acquire "$@" ;;
  refresh) shift; _with_mutex _cmd_refresh "$@" ;;
  release) shift; _with_mutex _cmd_release "$@" ;;
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
