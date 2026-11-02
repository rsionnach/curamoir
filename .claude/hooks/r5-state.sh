#!/usr/bin/env bash
# r5-state.sh — the only writer of <R5_ROOT>/.claude/r5-state.json, the R5
# supervisor's append-only audit trail (cm-99l). r5-parallel-block.sh reads
# it: while the last event is reviewer_dispatched, no other subagent may be
# dispatched. Writing it through one helper keeps that signal reliable.
#
# The reviewer window: `pass` (or `request`, for a re-dispatch) asks for
# exactly one reviewer. The parallel-block hook then calls `claim` on the
# next subagent dispatch, which records reviewer_dispatched atomically, so
# of two dispatches issued together only the first is let through. Until
# `findings` (or `abandon`) is recorded, every other dispatch is refused.
#
# Subcommands:
#   init <bead> <base> <head>      start a run (refuses if one is in flight)
#   pass <name>                    set current_pass, append pass_started
#                                  (requests the pass's first reviewer)
#   request <pass> <iteration>     request a re-dispatch of the same pass
#   claim <subagent_type>          (the hook) let one dispatch through as
#                                  the requested reviewer; exit 2 = refuse
#   findings <pass> <crit> <imp> <minor> [verdict]
#                                  append findings_received (closes the window)
#   abandon <pass> <reason>        a reviewer stalled or died: close the
#                                  window without findings (then `request`)
#   event <name> [json-object]     append any other event ({ts, event, ...})
#   done <pass> <fix_iterations> [json-object]
#                                  record the pass complete, clear current_pass
#                                  (refused unless <pass> is the current pass
#                                  and its last findings had no critical or
#                                  important finding)
#   advisory <crit> <imp> <minor> <verdict>
#                                  record the excellence read
#   finish <status>                completed | failed | aborted; archive the
#                                  file as r5-state-<bead>-<status>.json
#   show                           print the state file
set -euo pipefail

_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_DIR/lib/resolve-repo-root.sh" || exit 3
command -v jq >/dev/null 2>&1 || { printf 'r5-state: jq is required\n' >&2; exit 3; }

STATE="$R5_ROOT/.claude/r5-state.json"

# Every write is a read-modify-write of one file, made by the orchestrator and
# by concurrent hook invocations: serialise them with a short mkdir(2) mutex
# (atomic), cleared after MUTEX_STALE_SECONDS if a killed process left it.
MUTEX_DIR="$STATE.mutex"
MUTEX_STALE_SECONDS=30
_lock() {
  local tries=0
  mkdir -p "$(dirname "$STATE")" || exit 3
  until mkdir "$MUTEX_DIR" 2>/dev/null; do
    if [[ -d "$MUTEX_DIR" ]]; then
      local born
      born=$(stat -f %m "$MUTEX_DIR" 2>/dev/null || stat -c %Y "$MUTEX_DIR" 2>/dev/null || echo 0)
      (( $(date -u +%s) - born > MUTEX_STALE_SECONDS )) && rmdir "$MUTEX_DIR" 2>/dev/null
    fi
    (( ++tries > 100 )) && { printf 'r5-state: could not take %s after 10s\n' "$MUTEX_DIR" >&2; exit 3; }
    sleep 0.1
  done
  trap 'rmdir "$MUTEX_DIR" 2>/dev/null' EXIT
}
_last_event() { jq -r '.audit_trail[-1].event // ""' "$STATE"; }
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
    mkdir -p "$(dirname "$STATE")"
    jq -n --arg b "$bead" --arg base "$base" --arg head "$head" --arg t "$(_now)" '{
      supervisor: "r5-supervise", version: 2, target_bead: $b, started_at: $t,
      passes: ["correctness", "clarity", "edge-cases", "provenance"],
      advisory_passes: ["excellence"], current_pass: null, completed_passes: [],
      advisory_results: [], status: "in_progress",
      diff_range: {base: $base, head: $head},
      audit_trail: [{ts: $t, event: "supervisor_started"}]}' > "$STATE"
    ;;
  pass)
    _need; name="${2:?pass}"
    [[ "$(_last_event)" != "reviewer_dispatched" ]] || {
      printf 'r5-state: a reviewer is still in flight; record its findings or abandon it first\n' >&2; exit 2; }
    _apply '.current_pass = $p' --arg p "$name"
    _append "$(jq -n --arg t "$(_now)" --arg p "$name" '{ts:$t, event:"pass_started", pass:$p}')"
    ;;
  request)
    _need; pass="${2:?pass}"
    [[ "$(jq -r '.current_pass // ""' "$STATE")" == "$pass" ]] || {
      printf 'r5-state: %s is not the current pass\n' "$pass" >&2; exit 1; }
    [[ "$(_last_event)" != "reviewer_dispatched" ]] || {
      printf 'r5-state: a reviewer is still in flight; record its findings or abandon it first\n' >&2; exit 2; }
    _append "$(jq -n --arg t "$(_now)" --arg p "$pass" --argjson i "${3:?iteration}" \
      '{ts:$t, event:"reviewer_requested", pass:$p, iteration:$i}')"
    ;;
  claim)
    # Called by r5-parallel-block.sh for every subagent dispatch while a run
    # is in flight. Exit 0 = let it through; 2 = refuse.
    [[ -f "$STATE" ]] || exit 0
    [[ "$(jq -r '.status // ""' "$STATE")" == "in_progress" ]] || exit 0
    case "$(_last_event)" in
      pass_started|reviewer_requested)
        _append "$(jq -n --arg t "$(_now)" --arg p "$(jq -r '.current_pass // ""' "$STATE")" \
          --arg s "${2:-}" '{ts:$t, event:"reviewer_dispatched", pass:$p, subagent_type:$s}')"
        exit 0 ;;
      reviewer_dispatched)
        exit 2 ;;
      *)
        exit 0 ;;  # between passes or during a fix round: not a reviewer
    esac
    ;;
  abandon)
    _need
    [[ "$(_last_event)" == "reviewer_dispatched" ]] || {
      printf 'r5-state: no reviewer is in flight\n' >&2; exit 1; }
    _append "$(jq -n --arg t "$(_now)" --arg p "${2:?pass}" --arg r "${3:?reason}" \
      '{ts:$t, event:"reviewer_abandoned", pass:$p, reason:$r}')"
    ;;
  findings)
    _need
    _append "$(jq -n --arg t "$(_now)" --arg p "${2:?pass}" --argjson c "${3:?}" --argjson i "${4:?}" \
      --argjson m "${5:?}" --arg v "${6:-}" \
      '{ts:$t, event:"findings_received", pass:$p, counts:{critical:$c, important:$i, minor:$m}, verdict:$v}')"
    ;;
  event)
    _need; name="${2:?event}"; extra="${3:-}"; [[ -n "$extra" ]] || extra='{}'
    _append "$(jq -n --arg t "$(_now)" --arg e "$name" --argjson x "$extra" '{ts:$t, event:$e} + $x')"
    ;;
  done)
    _need; name="${2:?pass}"; iters="${3:?fix_iterations}"; extra="${4:-}"; [[ -n "$extra" ]] || extra='{}'
    [[ "$(jq -r '.current_pass // ""' "$STATE")" == "$name" ]] || {
      printf 'r5-state: %s is not the current pass\n' "$name" >&2; exit 1; }
    clean="$(jq -r --arg p "$name" '[.audit_trail[] | select(.event=="findings_received" and .pass==$p)] | last
      | if . == null then "none" elif (.counts.critical == 0 and .counts.important == 0) then "clean" else "dirty" end' "$STATE")"
    [[ "$clean" == "clean" ]] || {
      printf 'r5-state: %s cannot be done: its last findings are %s\n' "$name" "$clean" >&2; exit 1; }
    _apply '.completed_passes += [({name:$p, fix_iterations:$n, completed_at:$t} + $x)] | .current_pass = null' \
      --arg p "$name" --argjson n "$iters" --arg t "$(_now)" --argjson x "$extra"
    _append "$(jq -n --arg t "$(_now)" --arg p "$name" '{ts:$t, event:"pass_completed", pass:$p}')"
    ;;
  advisory)
    _need
    rec="$(jq -n --arg t "$(_now)" --argjson c "${2:?}" --argjson i "${3:?}" --argjson m "${4:?}" --arg v "${5:?verdict}" \
      '{ts:$t, event:"advisory_pass_completed", pass:"excellence", counts:{critical:$c, important:$i, minor:$m}, verdict:$v}')"
    _apply '.advisory_results += [$r]' --argjson r "$rec"
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
  *) sed -n '2,24p' "${BASH_SOURCE[0]}" >&2; exit 1 ;;
esac
