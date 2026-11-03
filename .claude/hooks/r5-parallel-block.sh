#!/usr/bin/env bash
# PreToolUse hook (matcher "Agent|Task") for curamoir's R5 supervisor.
# Ported from nthlayer-ecosystem/.claude/hooks/r5-parallel-block.sh (cm-99l),
# with these changes:
#   - registered for the Agent tool as well as Task (the tool's current
#     name; a "Task"-only matcher never fires);
#   - layer B applies to a dispatch of ANY subagent type, and records the
#     dispatch itself, atomically (r5-state.sh claim);
#   - layer A honours the lock's lease (lib/lease.sh), so a crashed holder
#     blocks other sessions for at most the lease, not forever.
#
#   A. Cross-session run lock. If $R5_LOCK_FILE is held, with a live lease,
#      by a different Claude Code session, refuse every subagent dispatch
#      from this one.
#
#   B. The run's window (r5-state.sh claim): one reviewer, or one fix agent,
#      at a time. The window protocol is documented in r5-state.sh; claim
#      prints the reason for a refusal.
#
# Blocked: exit 2 with a stderr message. If jq is missing while a run's lock
# or state exists, refuse (the guard cannot read them; failing open would let
# a parallel review through). Outside a curamoir checkout: allow.
set -euo pipefail

_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_HOOK_DIR/lib/resolve-repo-root.sh" 2>/dev/null || exit 0

if ! command -v jq >/dev/null 2>&1; then
  if [[ -f "$R5_LOCK_FILE" || -f "$R5_STATE_FILE" ]]; then
    printf 'r5-parallel-block: jq is missing, so the R5 lock and state cannot be read; refusing the dispatch\n' >&2
    exit 2
  fi
  exit 0
fi
# shellcheck source=lib/lease.sh
source "$_HOOK_DIR/lib/lease.sh"

PAYLOAD=$(cat)
CURRENT_SESSION="$(_r5_session)"
if [[ -z "$CURRENT_SESSION" ]]; then
  CURRENT_SESSION=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null || true)
fi

# ---- Layer A: another session's live lock ----
if [[ -f "$R5_LOCK_FILE" ]]; then
  HELD_SESSION=$(jq -r '.session_id // ""' "$R5_LOCK_FILE" 2>/dev/null || true)
  if [[ -n "$HELD_SESSION" && -n "$CURRENT_SESSION" && "$HELD_SESSION" != "$CURRENT_SESSION" ]] \
      && _r5_lease_live "$R5_LOCK_FILE"; then
    read -r REFRESH_AGE _ <<< "$(_r5_lock_ages "$R5_LOCK_FILE")"
    cat >&2 <<MSG
r5-parallel-block: the R5 supervisor lock is held by another session.
  supervisor: $(jq -r '.supervisor // "unknown"' "$R5_LOCK_FILE")
  target:     $(jq -r '.target // "unknown"' "$R5_LOCK_FILE")
  session:    $HELD_SESSION
  refreshed:  ${REFRESH_AGE}s ago

Refusing a subagent dispatch from session $CURRENT_SESSION until the holder
releases it or its lease runs out ($(( R5_LEASE_SECONDS - REFRESH_AGE ))s).
Do not remove the lock by hand: the run would then refuse this session
until it is adopted (r5-lock.sh acquire, then r5-state.sh adopt; to end
it, adopt it first, then r5-state.sh finish aborted).
MSG
    exit 2
  fi
  # A lapsed lease: the holder is gone, and layer B decides.
fi

# ---- Layer B: the run's window ----
[[ -f "$R5_STATE_FILE" ]] || exit 0
SUBAGENT=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.subagent_type // "general-purpose"' 2>/dev/null || echo "?")
# claim prints its reason; the hook adds which refusal it was.
rc=0
bash "$_HOOK_DIR/r5-state.sh" claim "$SUBAGENT" "$CURRENT_SESSION" || rc=$?
case "$rc" in
  0) exit 0 ;;
  2) why="one lens, one agent at a time" ;;
  4) why="another session's run" ;;
  *) why="the state helper failed (exit $rc)" ;;
esac
printf 'r5-parallel-block: refused %s for the R5 run on %s (%s)\n' \
  "$SUBAGENT" "$(jq -r '.target_bead // "unknown"' "$R5_STATE_FILE")" "$why" >&2
exit 2
