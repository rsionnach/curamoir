#!/usr/bin/env bash
# r5-state.sh — the only writer of <R5_ROOT>/.claude/r5-state.json, the R5
# supervisor's append-only audit trail (cm-99l). r5-parallel-block.sh reads
# it: while the last event is reviewer_dispatched, no other subagent may be
# dispatched. Writing it through one helper keeps that signal reliable.
#
# Subcommands:
#   init <bead> <base> <head>      start a run (refuses if one is in flight)
#   pass <name>                    set current_pass, append pass_started
#   dispatched <pass> <iteration>  append reviewer_dispatched (blocks dispatch)
#   findings <pass> <crit> <imp> <minor> [verdict]
#                                  append findings_received (unblocks)
#   event <name> [json-object]     append any other event ({ts, event, ...})
#   done <pass> <fix_iterations> [json-object]
#                                  record the pass complete, clear current_pass
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
    _apply '.current_pass = $p' --arg p "$name"
    _append "$(jq -n --arg t "$(_now)" --arg p "$name" '{ts:$t, event:"pass_started", pass:$p}')"
    ;;
  dispatched)
    _need
    _append "$(jq -n --arg t "$(_now)" --arg p "${2:?pass}" --argjson i "${3:?iteration}" \
      '{ts:$t, event:"reviewer_dispatched", pass:$p, iteration:$i}')"
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
