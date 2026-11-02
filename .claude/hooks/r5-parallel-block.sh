#!/usr/bin/env bash
# PreToolUse hook (matcher "Agent|Task") for curamoir's R5 supervisor.
# Ported from nthlayer-ecosystem/.claude/hooks/r5-parallel-block.sh (cm-99l),
# with two fixes:
#   - it is registered for the Agent tool as well as Task (the subagent tool
#     is now called Agent; a "Task"-only matcher never fires), and
#   - layer B blocks a dispatch of ANY subagent type while a reviewer is in
#     flight, not only pr-review-toolkit:code-reviewer — a reviewer briefed
#     as general-purpose must not slip past it.
#
# Two layers, in order:
#
#   A. Cross-session run-level mutex. If <R5_ROOT>/.claude/r5-supervisor.lock
#      is held by a different Claude Code session, refuse every subagent
#      dispatch from this one.
#
#   B. One reviewer at a time within the run. .claude/r5-state.json is written
#      by r5-supervise: if its status is in_progress and the last audit event
#      is reviewer_dispatched (the supervisor has not yet recorded that
#      reviewer's findings), refuse any further dispatch. This is what makes
#      two lenses at the same time impossible.
#
# When blocked: exit 2 with a stderr message naming the holder or the pass in
# flight. Escape hatches: the lock's lease reclaims after 15 min; a stale
# state file is cleared with `rm .claude/r5-state.json`.
set -euo pipefail

_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_HOOK_DIR/lib/resolve-repo-root.sh" 2>/dev/null || exit 0  # outside a curamoir checkout: nothing to guard

PAYLOAD=$(cat)

# ---- Layer A: cross-session lock ----
LOCK_FILE="$R5_ROOT/.claude/r5-supervisor.lock"
if [[ -f "$LOCK_FILE" ]]; then
  CURRENT_SESSION="${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
  if [[ -z "$CURRENT_SESSION" ]]; then
    CURRENT_SESSION=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null || true)
  fi
  HELD_SESSION=$(jq -r '.session_id // ""' "$LOCK_FILE" 2>/dev/null || true)
  if [[ -n "$HELD_SESSION" && -n "$CURRENT_SESSION" && "$HELD_SESSION" != "$CURRENT_SESSION" ]]; then
    cat >&2 <<EOF
r5-parallel-block: the R5 supervisor lock is held by another session.
  supervisor: $(jq -r '.supervisor // "unknown"' "$LOCK_FILE")
  target:     $(jq -r '.target // "unknown"' "$LOCK_FILE")
  session:    $HELD_SESSION
  refreshed:  $(jq -r '.refreshed_at // "?"' "$LOCK_FILE")

Refusing a subagent dispatch from session $CURRENT_SESSION.
The lease reclaims after ~15 min (.claude/hooks/r5-lock.sh status).
Manual override: rm "$LOCK_FILE"
EOF
    exit 2
  fi
fi

# ---- Layer B: one reviewer in flight at a time ----
STATE_FILE="$R5_ROOT/.claude/r5-state.json"
[[ -f "$STATE_FILE" ]] || exit 0
STATUS=$(jq -r '.status // ""' "$STATE_FILE" 2>/dev/null || echo "")
[[ "$STATUS" == "in_progress" ]] || exit 0
LATEST_EVENT=$(jq -r '.audit_trail[-1].event // ""' "$STATE_FILE" 2>/dev/null || echo "")
[[ "$LATEST_EVENT" == "reviewer_dispatched" ]] || exit 0

cat >&2 <<EOF
r5-parallel-block: a reviewer is in flight; no other subagent may be dispatched
until its findings are recorded (one lens at a time).
  target: $(jq -r '.target_bead // "unknown"' "$STATE_FILE")
  pass:   $(jq -r '.current_pass // "unknown"' "$STATE_FILE")
  wanted: $(printf '%s' "$PAYLOAD" | jq -r '.tool_input.subagent_type // "general-purpose"' 2>/dev/null || echo "?")

Record the findings (append a findings_received event) first. If the state is
stale, clear it with: rm "$STATE_FILE"
EOF
exit 2
