#!/usr/bin/env bash
# PreToolUse hook (matcher "Agent|Task") for curamoir's R5 supervisor.
# Ported from nthlayer-ecosystem/.claude/hooks/r5-parallel-block.sh (cm-99l),
# with these changes:
#   - registered for the Agent tool as well as Task (the tool's current
#     name; a "Task"-only matcher never fires);
#   - layer B applies to a dispatch of ANY subagent type, and records the
#     reviewer's dispatch itself, atomically (r5-state.sh claim), so of two
#     dispatches issued in one message only the first is let through;
#   - layer A honours the lock's lease, so a crashed holder blocks other
#     sessions for at most LEASE_SECONDS, not forever.
#
#   A. Cross-session run lock. If <R5_ROOT>/.claude/r5-supervisor.lock is
#      held, with a live lease, by a different Claude Code session, refuse
#      every subagent dispatch from this one.
#
#   B. One agent at a time within the run (r5-state.sh claim), for the
#      session that owns the run: after the supervisor requests a reviewer
#      (`pass` / `request`), the first dispatch is let through and recorded
#      as reviewer_dispatched; every further dispatch is refused until that
#      reviewer's findings are recorded (or it is abandoned and requested
#      again). In a fix round, one fix agent at a time.
#
# Blocked: exit 2 with a stderr message. If jq is missing while a run's lock
# or state exists, refuse (the guard cannot read them; failing open would let
# a parallel review through). Outside a curamoir checkout: allow.
set -euo pipefail

LEASE_SECONDS=900
ABSOLUTE_TTL_SECONDS=86400

_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_HOOK_DIR/lib/resolve-repo-root.sh" 2>/dev/null || exit 0

LOCK_FILE="$R5_ROOT/.claude/r5-supervisor.lock"
STATE_FILE="$R5_ROOT/.claude/r5-state.json"

if ! command -v jq >/dev/null 2>&1; then
  if [[ -f "$LOCK_FILE" || -f "$STATE_FILE" ]]; then
    printf 'r5-parallel-block: jq is missing, so the R5 lock and state cannot be read; refusing the dispatch\n' >&2
    exit 2
  fi
  exit 0
fi

PAYLOAD=$(cat)

_epoch() {
  local ts="${1:-}"
  [[ -z "$ts" ]] && { printf '0'; return; }
  date -u -d "$ts" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null || printf '0'
}

CURRENT_SESSION="${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
if [[ -z "$CURRENT_SESSION" ]]; then
  CURRENT_SESSION=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null || true)
fi

# ---- Layer A: cross-session lock, with its lease ----
if [[ -f "$LOCK_FILE" ]]; then
  HELD_SESSION=$(jq -r '.session_id // ""' "$LOCK_FILE" 2>/dev/null || true)
  if [[ -n "$HELD_SESSION" && -n "$CURRENT_SESSION" && "$HELD_SESSION" != "$CURRENT_SESSION" ]]; then
    NOW=$(date -u +%s)
    REFRESH_AGE=$(( NOW - $(_epoch "$(jq -r '.refreshed_at // ""' "$LOCK_FILE")") ))
    ACQUIRE_AGE=$(( NOW - $(_epoch "$(jq -r '.acquired_at // ""' "$LOCK_FILE")") ))
    if (( REFRESH_AGE <= LEASE_SECONDS && ACQUIRE_AGE <= ABSOLUTE_TTL_SECONDS )); then
      cat >&2 <<MSG
r5-parallel-block: the R5 supervisor lock is held by another session.
  supervisor: $(jq -r '.supervisor // "unknown"' "$LOCK_FILE")
  target:     $(jq -r '.target // "unknown"' "$LOCK_FILE")
  session:    $HELD_SESSION
  refreshed:  ${REFRESH_AGE}s ago

Refusing a subagent dispatch from session $CURRENT_SESSION until the holder
releases it or its lease runs out ($(( LEASE_SECONDS - REFRESH_AGE ))s).
Do not remove the lock by hand: the run would then refuse this session
until it is adopted (r5-lock.sh acquire, r5-state.sh adopt) or ended
(r5-state.sh finish aborted).
MSG
      exit 2
    fi
    # Stale lease: the holder is gone; this session's dispatches go ahead.
  fi
fi

# ---- Layer B: one reviewer (or one fix agent) at a time, in the run's own
# session. Another session's dispatches: layer A refuses them while the
# owner's lease is live; claim lets them through once it has lapsed, and
# refuses them (exit 4) in any other case, e.g. with no lock at all ----
[[ -f "$STATE_FILE" ]] || exit 0
SUBAGENT=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.subagent_type // "general-purpose"' 2>/dev/null || echo "?")
rc=0
bash "$_HOOK_DIR/r5-state.sh" claim "$SUBAGENT" "$CURRENT_SESSION" || rc=$?
(( rc == 0 )) && exit 0
if (( rc == 4 )); then
  cat >&2 <<MSG
r5-parallel-block: the R5 run on $(jq -r '.target_bead // "unknown"' "$STATE_FILE") belongs to session
$(jq -r '.session_id // "unknown"' "$STATE_FILE"), and its lock is not that session's
lapsed lock, so this session ($CURRENT_SESSION) may not dispatch subagents.

Take the run over (r5-lock.sh acquire r5-supervise <bead>, then
r5-state.sh adopt) or end it (r5-state.sh finish aborted).
MSG
  exit 2
fi
cat >&2 <<MSG
r5-parallel-block: a reviewer or fix agent of the R5 run is in flight (or an
abandoned reviewer has not been requested again); no other subagent may be
dispatched (one lens, one agent at a time).
  target: $(jq -r '.target_bead // "unknown"' "$STATE_FILE")
  pass:   $(jq -r '.current_pass // "unknown"' "$STATE_FILE")
  wanted: $SUBAGENT

Reviewer: record its findings (r5-state.sh findings ...) or, if it stalled
or never started, r5-state.sh abandon <pass> <reason>, then request it again.
Fix agent: r5-state.sh event fixes_applied '{"files":[...]}' when it is done.
MSG
exit 2
