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
#   event <name> [json-object]     append any other event ({ts, event, ...});
#                                  of the window events it writes only
#                                  fixes_applied and reviewer_left_tree_dirty,
#                                  and only after the current pass's findings
#   done <pass> <fix_iterations> [json-object]
#                                  record the pass complete, clear current_pass
#                                  (refused unless the run's last window event
#                                  is that pass's findings, with no critical or
#                                  important finding)
#   advisory <crit> <imp> <minor> <verdict>
#                                  the excellence reviewer's report
#   finish <status>                completed (refused unless every pass is
#                                  done and the advisory read recorded) |
#                                  failed | aborted; archive the
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
  "reviewer_abandoned","findings_received","fix_dispatched","fixes_applied",
  "reviewer_left_tree_dirty","pass_completed","advisory_pass_completed"]'
# The two window events the orchestrator writes through `event`, and the last
# window event each may follow.
FIXES_APPLIED_AFTER='["findings_received","fix_dispatched","fixes_applied","reviewer_left_tree_dirty"]'
LEFT_DIRTY_AFTER='["findings_received"]'
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
_object() {  # _object <json>: refuse anything but a JSON object
  jq -en --argjson x "$1" '$x | type == "object"' >/dev/null 2>&1 || {
    printf 'r5-state: expected a JSON object, got %s\n' "$1" >&2; exit 1; }
}
_blocking_pass() {  # _blocking_pass <name>: one of the four blocking passes
  jq -e --arg p "$1" '.passes | index($p)' "$STATE" >/dev/null || {
    printf 'r5-state: %s is not a blocking pass\n' "$1" >&2; exit 1; }
}
_epoch() {
  date -u -d "$1" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" +%s 2>/dev/null || printf '0'
}
# True if the run owner's lock has lapsed: the lock exists, is held by
# <owner>, and its lease (or its absolute TTL) has run out. These numbers
# match r5-lock.sh and r5-parallel-block.sh.
_owner_lease_lapsed() {  # _owner_lease_lapsed <owner>
  local lock="$R5_ROOT/.claude/r5-supervisor.lock" now refreshed acquired
  [[ -f "$lock" ]] || return 1
  [[ "$(jq -r '.session_id // ""' "$lock" 2>/dev/null)" == "$1" ]] || return 1
  now=$(date -u +%s)
  refreshed=$(_epoch "$(jq -r '.refreshed_at // ""' "$lock")")
  acquired=$(_epoch "$(jq -r '.acquired_at // ""' "$lock")")
  (( now - refreshed > 900 || now - acquired > 86400 ))
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
    # Another session's dispatch. It goes ahead only when the run's owner
    # has crashed: its lock is still there and its lease has lapsed. Any
    # other case (no lock, a lock someone else took and has not adopted the
    # run with) is refused. An unknown session on either side is treated
    # as the run's own, so that check fails closed too.
    if [[ -n "$owner" && -n "$caller" && "$owner" != "$caller" ]]; then
      _owner_lease_lapsed "$owner" && exit 0
      exit 2
    fi
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
    _blocking_pass "$pass"
    _need_current "$pass"
    _need_in_flight
    _append "$(jq -n --arg t "$(_now)" --arg p "$pass" --argjson c "${3:?}" --argjson i "${4:?}" \
      --argjson m "${5:?}" --arg v "${6:-}" \
      '{ts:$t, event:"findings_received", pass:$p, counts:{critical:$c, important:$i, minor:$m}, verdict:$v}')"
    ;;
  event)
    _need; name="${2:?event}"; extra="${3:-}"; [[ -n "$extra" ]] || extra='{}'
    _object "$extra"
    case "$name" in
      fixes_applied) after="$FIXES_APPLIED_AFTER" ;;
      reviewer_left_tree_dirty) after="$LEFT_DIRTY_AFTER" ;;
      *)
        if jq -en --argjson w "$WINDOW_EVENTS" --arg e "$name" '$w | index($e)' >/dev/null; then
          printf 'r5-state: %s is written by its own subcommand, not by event\n' "$name" >&2; exit 1
        fi
        after="" ;;
    esac
    if [[ -n "$after" ]]; then
      # A fix (or a dirty tree) belongs to the current pass, after its
      # reviewer has reported; never while a reviewer is requested or running.
      last="$(_last_event)"
      [[ -n "$(_current_pass)" ]] && jq -en --argjson a "$after" --arg e "$last" '$a | index($e)' >/dev/null || {
        printf 'r5-state: %s cannot follow %s\n' "$name" "${last:-nothing}" >&2; exit 2; }
      extra="$(jq -cn --argjson x "$extra" --arg p "$(_current_pass)" '$x + {pass:$p}')"
    fi
    # The fixed fields are applied last, so the caller's object cannot
    # rename the event or rewrite its timestamp.
    _append "$(jq -n --arg t "$(_now)" --arg e "$name" --argjson x "$extra" '$x + {ts:$t, event:$e}')"
    ;;
  done)
    _need; name="${2:?pass}"; iters="${3:?fix_iterations}"; extra="${4:-}"; [[ -n "$extra" ]] || extra='{}'
    _object "$extra"
    _blocking_pass "$name"
    _need_current "$name"
    # The last window event must be this pass's findings: anything after
    # them (a re-request, a dispatch, a fix) has not been reviewed yet.
    clean="$(_last | jq -r --arg p "$name" '
      if (.event != "findings_received" or .pass != $p) then "followed by later activity"
        elif (.counts.critical == 0 and .counts.important == 0) then "clean" else "dirty" end')"
    [[ "$clean" == "clean" ]] || {
      printf 'r5-state: %s cannot be done: its findings are %s\n' "$name" "$clean" >&2; exit 1; }
    _apply '.completed_passes += [($x + {name:$p, fix_iterations:$n, completed_at:$t})] | .current_pass = null' \
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
    if [[ "$status" == completed ]]; then
      # Completed means every blocking pass done, in order, the advisory read
      # recorded, and nothing left open.
      missing="$(jq -r '[.passes[] as $p | select([.completed_passes[].name] | index($p) | not) | $p]
        + (if (.advisory_results | length) == 0 then ["excellence"] else [] end)
        + (if .current_pass != null then ["(" + .current_pass + " is still current)"] else [] end)
        | join(", ")' "$STATE")"
      [[ -z "$missing" ]] || {
        printf 'r5-state: the run is not complete: %s\n' "$missing" >&2; exit 2; }
    fi
    _apply '.status = $s | .current_pass = null' --arg s "$status"
    _append "$(jq -n --arg t "$(_now)" --arg s "$status" '{ts:$t, event:("supervisor_" + $s)}')"
    bead="$(jq -r '.target_bead' "$STATE")"
    mv "$STATE" "$R5_ROOT/.claude/r5-state-${bead}-${status}-$(date -u +%Y%m%dT%H%M%SZ).json"
    ;;
  show) _need; cat "$STATE" ;;
  *) sed -n '2,56p' "${BASH_SOURCE[0]}" >&2; exit 1 ;;
esac
