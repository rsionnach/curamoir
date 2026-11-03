#!/usr/bin/env bash
# r5-state.sh — the only writer of <R5_ROOT>/.claude/r5-state.json, the R5
# supervisor's append-only audit trail (cm-99l). r5-parallel-block.sh reads
# it: while the last window event is reviewer_dispatched, no other subagent may be
# dispatched. Writing it through one helper keeps that signal reliable.
#
# The reviewer window: `pass` (or `request`, for a re-dispatch) asks for
# exactly one reviewer. The parallel-block hook then calls `claim` on the
# next subagent dispatch, which records reviewer_dispatched atomically, so
# of two dispatches issued together only the first is let through. Until
# `findings` (or `abandon`, then `request`) is recorded, every other dispatch
# is refused. In a fix round (a pass is current, its reviewer has reported)
# `claim` records each dispatch as fix_dispatched and lets one fix agent run
# at a time; `event fixes_applied` closes it, and `request` is refused while
# one is open. Between passes, dispatches are not the run's business.
#
# The run belongs to the session that started (or adopted) it: `claim`
# applies only to that session's dispatches, so a crashed run cannot block
# another session once the lock's lease has run out (the hook's layer A
# refuses other sessions while it is live).
#
# Subcommands:
#   init <bead> <base> <head>      start a run (refuses if one is in flight)
#   adopt                          take over an in-flight run from a session
#                                  that is gone (this session must hold the
#                                  lock: r5-lock.sh acquire first)
#   pass <name>                    set current_pass, append pass_started
#                                  (requests the pass's first reviewer);
#                                  passes run in order, excellence last
#   request <pass> <iteration>     request a re-dispatch of the current pass
#   claim <subagent_type> [session]
#                                  (the hook) let one dispatch through as
#                                  the requested reviewer or a fix agent;
#                                  exit 2 = refuse
#   findings <pass> <crit> <imp> <minor> [verdict]
#                                  the dispatched reviewer's report; closes
#                                  the window
#   abandon <pass> <reason>        the dispatched reviewer stalled, died or
#                                  never started: close the window without
#                                  findings (then `request`)
#   event <name> [json-object]     append any other event ({ts, event, ...})
#   done <pass> <fix_iterations> [json-object]
#                                  record the pass complete, clear current_pass
#                                  (refused unless the run's last window event
#                                  is that pass's findings, with no critical or
#                                  important finding)
#   advisory <crit> <imp> <minor> <verdict>
#                                  the excellence reviewer's report
#   finish <status>                completed | failed | aborted; archive the
#                                  file as r5-state-<bead>-<status>-<ts>.json
#   show                           print the state file
set -euo pipefail

_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_DIR/lib/resolve-repo-root.sh" || exit 3
command -v jq >/dev/null 2>&1 || { printf 'r5-state: jq is required\n' >&2; exit 3; }

STATE="$R5_ROOT/.claude/r5-state.json"

# Every write is a read-modify-write of one file, made by the orchestrator and
# by concurrent hook invocations: serialise them (lib/flock.sh).
# shellcheck source=lib/flock.sh
source "$_DIR/lib/flock.sh"
_lock() {
  mkdir -p "$(dirname "$STATE")" || exit 3
  _r5_flock "$STATE.flock" r5-state
}
_session() { printf '%s' "${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"; }
_current_pass() { jq -r '.current_pass // ""' "$STATE"; }
_need_current() {  # _need_current <pass>
  [[ "$(_current_pass)" == "$1" ]] || {
    printf 'r5-state: %s is not the current pass\n' "$1" >&2; exit 1; }
}
_need_in_flight() {
  [[ "$(_last_event)" == "reviewer_dispatched" ]] || {
    printf 'r5-state: no reviewer is in flight\n' >&2; exit 1; }
}
_need_not_in_flight() {
  [[ "$(_last_event)" != "reviewer_dispatched" ]] || {
    printf 'r5-state: a reviewer is still in flight; record its findings or abandon it first\n' >&2; exit 2; }
}
# A fix agent dispatched in this run and not yet closed by fixes_applied.
_open_fix() {
  jq -e '(.audit_trail | map(.event)) as $e
    | ($e | indices("fix_dispatched") | max // -1) > ($e | indices("fixes_applied") | max // -1)' \
    "$STATE" >/dev/null
}
_next_pass() {
  jq -r '([.completed_passes[].name]) as $done
    | ([.passes[] | select(. as $p | $done | index($p) | not)] | first) // "excellence"' "$STATE"
}
# The window events: the ones that open or close a reviewer's or a fix
# agent's window. Every decision reads the last of these, so a bookkeeping
# event (tests_passed, run_adopted, ...) can never reopen a window, and
# `event` may not write one.
WINDOW_EVENTS='["supervisor_started","pass_started","reviewer_requested","reviewer_dispatched",
  "reviewer_abandoned","findings_received","fix_dispatched","fixes_applied","pass_completed",
  "advisory_pass_completed"]'
_last() {  # the last window event, as JSON
  jq -c --argjson w "$WINDOW_EVENTS" '[.audit_trail[] | select(.event as $e | $w | index($e))] | last // {}' "$STATE"
}
_last_event() { _last | jq -r '.event // ""'; }
_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
_need() { [[ -f "$STATE" ]] || { printf 'r5-state: no run in flight (%s)\n' "$STATE" >&2; exit 1; }; }
_apply() {  # _apply <jq filter> [jq args...]
  local filter="$1"; shift
  local tmp="$STATE.tmp.$$"
  if jq "$@" "$filter" "$STATE" > "$tmp"; then mv "$tmp" "$STATE"; else rm -f "$tmp"; exit 1; fi
}
_append() {  # _append <event-json>
  _apply '.audit_trail += [$e]' --argjson e "$1"
}

_lock

case "${1:-}" in
  init)
    bead="${2:?bead}"; base="${3:?base}"; head="${4:?head}"
    if [[ -f "$STATE" ]] && [[ "$(jq -r '.status' "$STATE")" == "in_progress" ]]; then
      printf 'r5-state: a run is in flight on %s; finish or abort it first\n' \
        "$(jq -r '.target_bead' "$STATE")" >&2
      exit 2
    fi
    jq -n --arg b "$bead" --arg base "$base" --arg head "$head" --arg t "$(_now)" --arg s "$(_session)" '{
      supervisor: "r5-supervise", version: 3, target_bead: $b, session_id: $s, started_at: $t,
      passes: ["correctness", "clarity", "edge-cases", "provenance"],
      advisory_passes: ["excellence"], current_pass: null, completed_passes: [],
      advisory_results: [], status: "in_progress",
      diff_range: {base: $base, head: $head},
      audit_trail: [{ts: $t, event: "supervisor_started", session_id: $s}]}' > "$STATE"
    ;;
  adopt)
    _need; me="$(_session)"
    [[ -n "$me" ]] || { printf 'r5-state: no session id in the environment\n' >&2; exit 1; }
    holder="$(jq -r '.session_id // ""' "$R5_ROOT/.claude/r5-supervisor.lock" 2>/dev/null || true)"
    [[ "$holder" == "$me" ]] || {
      printf 'r5-state: this session does not hold the R5 lock; acquire it first\n' >&2; exit 2; }
    from="$(jq -r '.session_id // ""' "$STATE")"
    _apply '.session_id = $s' --arg s "$me"
    _append "$(jq -n --arg t "$(_now)" --arg f "$from" --arg s "$me" '{ts:$t, event:"run_adopted", from:$f, session_id:$s}')"
    ;;
  pass)
    _need; name="${2:?pass}"
    _need_not_in_flight
    cur="$(_current_pass)"
    [[ -z "$cur" ]] || {
      printf 'r5-state: %s is still the current pass; record it done first\n' "$cur" >&2; exit 2; }
    next="$(_next_pass)"
    [[ "$name" == "$next" ]] || {
      printf 'r5-state: the next pass is %s, not %s\n' "$next" "$name" >&2; exit 2; }
    _open_fix && { printf 'r5-state: a fix agent is still open; record fixes_applied first\n' >&2; exit 2; }
    _apply '.current_pass = $p' --arg p "$name"
    _append "$(jq -n --arg t "$(_now)" --arg p "$name" '{ts:$t, event:"pass_started", pass:$p}')"
    ;;
  request)
    _need; pass="${2:?pass}"
    _need_current "$pass"
    _need_not_in_flight
    _open_fix && { printf 'r5-state: a fix agent is still open; record fixes_applied first\n' >&2; exit 2; }
    _append "$(jq -n --arg t "$(_now)" --arg p "$pass" --argjson i "${3:?iteration}" \
      '{ts:$t, event:"reviewer_requested", pass:$p, iteration:$i}')"
    ;;
  claim)
    # Called by r5-parallel-block.sh for every subagent dispatch while a run
    # is in flight. Exit 0 = let it through; 2 = refuse.
    [[ -f "$STATE" ]] || exit 0
    [[ "$(jq -r '.status // ""' "$STATE")" == "in_progress" ]] || exit 0
    owner="$(jq -r '.session_id // ""' "$STATE")"; caller="${3:-$(_session)}"
    # Another session's dispatch: not this run's (an unknown session on
    # either side is treated as the run's own, so the check fails closed).
    [[ -n "$owner" && -n "$caller" && "$owner" != "$caller" ]] && exit 0
    pass="$(_current_pass)"
    case "$(_last_event)" in
      pass_started|reviewer_requested)
        _append "$(jq -n --arg t "$(_now)" --arg p "$pass" \
          --arg s "${2:-}" '{ts:$t, event:"reviewer_dispatched", pass:$p, subagent_type:$s}')"
        exit 0 ;;
      reviewer_dispatched|reviewer_abandoned)
        exit 2 ;;  # a reviewer is in flight, or one must be requested again
      *)
        [[ -n "$pass" ]] || exit 0  # between passes: not a reviewer
        _open_fix && exit 2         # one fix agent at a time
        _append "$(jq -n --arg t "$(_now)" --arg p "$pass" \
          --arg s "${2:-}" '{ts:$t, event:"fix_dispatched", pass:$p, subagent_type:$s}')"
        exit 0 ;;
    esac
    ;;
  abandon)
    _need; pass="${2:?pass}"
    _need_current "$pass"
    _need_in_flight
    _append "$(jq -n --arg t "$(_now)" --arg p "$pass" --arg r "${3:?reason}" \
      '{ts:$t, event:"reviewer_abandoned", pass:$p, reason:$r}')"
    ;;
  findings)
    _need; pass="${2:?pass}"
    _need_current "$pass"
    _need_in_flight
    _append "$(jq -n --arg t "$(_now)" --arg p "$pass" --argjson c "${3:?}" --argjson i "${4:?}" \
      --argjson m "${5:?}" --arg v "${6:-}" \
      '{ts:$t, event:"findings_received", pass:$p, counts:{critical:$c, important:$i, minor:$m}, verdict:$v}')"
    ;;
  event)
    _need; name="${2:?event}"; extra="${3:-}"; [[ -n "$extra" ]] || extra='{}'
    if [[ "$name" != fixes_applied ]] && jq -en --argjson w "$WINDOW_EVENTS" --arg e "$name" '$w | index($e)' >/dev/null; then
      printf 'r5-state: %s is written by its own subcommand, not by event\n' "$name" >&2; exit 1
    fi
    _append "$(jq -n --arg t "$(_now)" --arg e "$name" --argjson x "$extra" '{ts:$t, event:$e} + $x')"
    ;;
  done)
    _need; name="${2:?pass}"; iters="${3:?fix_iterations}"; extra="${4:-}"; [[ -n "$extra" ]] || extra='{}'
    _need_current "$name"
    # The last window event must be this pass's findings: anything after
    # them (a re-request, a dispatch, a fix) has not been reviewed yet.
    clean="$(_last | jq -r --arg p "$name" '
      if (.event != "findings_received" or .pass != $p) then "followed by later activity"
        elif (.counts.critical == 0 and .counts.important == 0) then "clean" else "dirty" end')"
    [[ "$clean" == "clean" ]] || {
      printf 'r5-state: %s cannot be done: its findings are %s\n' "$name" "$clean" >&2; exit 1; }
    _apply '.completed_passes += [({name:$p, fix_iterations:$n, completed_at:$t} + $x)] | .current_pass = null' \
      --arg p "$name" --argjson n "$iters" --arg t "$(_now)" --argjson x "$extra"
    _append "$(jq -n --arg t "$(_now)" --arg p "$name" '{ts:$t, event:"pass_completed", pass:$p}')"
    ;;
  advisory)
    _need
    _need_current excellence
    _need_in_flight
    rec="$(jq -n --arg t "$(_now)" --argjson c "${2:?}" --argjson i "${3:?}" --argjson m "${4:?}" --arg v "${5:?verdict}" \
      '{ts:$t, event:"advisory_pass_completed", pass:"excellence", counts:{critical:$c, important:$i, minor:$m}, verdict:$v}')"
    _apply '.advisory_results += [$r] | .current_pass = null' --argjson r "$rec"
    _append "$rec"
    ;;
  finish)
    _need; status="${2:?status}"
    case "$status" in completed|failed|aborted) ;; *) printf 'r5-state: bad status %s\n' "$status" >&2; exit 1;; esac
    _apply '.status = $s | .current_pass = null' --arg s "$status"
    _append "$(jq -n --arg t "$(_now)" --arg s "$status" '{ts:$t, event:("supervisor_" + $s)}')"
    bead="$(jq -r '.target_bead' "$STATE")"
    mv "$STATE" "$R5_ROOT/.claude/r5-state-${bead}-${status}-$(date -u +%Y%m%dT%H%M%SZ).json"
    ;;
  show) _need; cat "$STATE" ;;
  *) sed -n '2,51p' "${BASH_SOURCE[0]}" >&2; exit 1 ;;
esac
