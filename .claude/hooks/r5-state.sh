#!/usr/bin/env bash
# r5-state.sh — the only writer of $R5_STATE_FILE (<R5_ROOT>/.claude/
# r5-state.json), the R5 supervisor's append-only audit trail (cm-99l).
# r5-parallel-block.sh asks it (`claim`) whether a subagent dispatch may go
# ahead. This header is the authoritative description of the window
# protocol; SKILL.md and the hook point here.
#
# Windows. Every decision reads the last *window event* (WINDOW_EVENTS
# below); bookkeeping events (tests_passed, commit_made, run_adopted, ...)
# never open or close one.
#   - Reviewer window: `pass` (or `request`, for a re-dispatch) asks for
#     exactly one reviewer. The hook's `claim` on the next dispatch records
#     reviewer_dispatched atomically, so of two dispatches issued together
#     only the first is let through. Every other dispatch is refused until
#     `findings` closes the window (or `abandon`, and then `request` again).
#   - Fix window: once the current pass's reviewer has reported, `claim`
#     records a dispatch as fix_dispatched and lets one fix agent run at a
#     time; `fixed` closes it. `request` is refused while one is open.
#   - Between passes (no current pass), dispatches are not the run's
#     business.
#
# Ownership. The run belongs to the session that started (or adopted) it,
# and the windows apply to that session's dispatches. Another session's
# dispatch goes through only once the owner's lock has lapsed (a crashed
# run); while the lease is live the hook's layer A refuses it, and with no
# lock or someone else's lock `claim` refuses it (exit 4) until the run is
# adopted or finished. Every subcommand that changes the run (all but init,
# adopt, claim and show) is likewise the owner's alone: another session gets
# exit 4 until it adopts the run.
#
# Subcommands:
#   init <bead> <base> <head>      start a run, owned by this session (bead:
#                                  letters, digits, '.', '_', '-')
#   adopt                          take over the run from a session that is
#                                  gone (this session must hold the lock:
#                                  r5-lock.sh acquire first)
#   pass <pass>                    start the next pass (in order, excellence
#                                  last) and request its first reviewer
#   request <pass> <iteration>     request a re-dispatch of the current pass
#   claim <subagent_type> [session]
#                                  (the hook) let one dispatch through as
#                                  the requested reviewer or a fix agent
#   findings <pass> <crit> <imp> <minor> [verdict]
#                                  the dispatched reviewer's report
#   abandon <pass> <reason>        the dispatched reviewer stalled, died or
#                                  never started: close its window without
#                                  findings (then `request`)
#   fixed <pass> [json-object]     the fix round's changes are in place:
#                                  closes the fix window (fixes_applied)
#   tree-dirty <pass>              provenance left the tree dirty: the pass
#                                  failed whatever its counts; `done` is
#                                  refused until it is reviewed again
#   reopen <pass>                  a change after <pass> was done: it and
#                                  every later pass (and the advisory read)
#                                  are to run again, starting with <pass>
#   event <name> [json-object]     append a bookkeeping event
#   done <pass> <fix_iterations> [json-object]
#                                  the pass is complete (only if the run's
#                                  last window event is that pass's findings,
#                                  with no critical or important finding)
#   advisory <crit> <imp> <minor> <verdict>
#                                  the excellence reviewer's report
#   finish <completed|failed|aborted>
#                                  end the run (completed only when every
#                                  pass is done and the advisory read
#                                  recorded); archive the file as
#                                  r5-state-<bead>-<status>-<ts>.json
#   open-window                    (r5-lock.sh) "<owner> <bead> <reviewer|
#                                  fix-agent>" while one is in flight
#   show                           print the state file
#
# Exit codes:
#   0   - success (claim: the dispatch may go ahead)
#   1   - usage error or a bad argument (a missing one, a count that is not
#         a non-negative integer, a JSON argument that is not an object)
#   2   - refused by the run's state or order (claim: refuse the dispatch)
#   3   - internal error (cannot resolve the root, jq or perl missing, the
#         mutex not taken within 10s)
#   4   - another session's run (claim: refuse the dispatch; any other
#         subcommand that changes the run: adopt it first)
set -euo pipefail

# This script's real directory: through any symlink to the file, and with
# CDPATH cleared (an exported CDPATH makes `cd` print a second path), and
# physically (-P), so a `..` in a link target is the kernel's `..`. The
# same bootstrap opens each helper, before lib/ can be sourced.
_self="${BASH_SOURCE[0]}"
while [[ -L "$_self" ]]; do
  _link="$(readlink "$_self")"
  [[ "$_link" == /* ]] || _link="$(dirname -- "$_self")/$_link"
  _self="$_link"
done
_DIR="$(CDPATH= cd -P -- "$(dirname -- "$_self")" && pwd)"
# shellcheck source=lib/resolve-repo-root.sh
source "$_DIR/lib/resolve-repo-root.sh" || exit 3
command -v jq >/dev/null 2>&1 || { printf 'r5-state: jq is required\n' >&2; exit 3; }
# shellcheck source=lib/flock.sh
source "$_DIR/lib/flock.sh"
# shellcheck source=lib/lease.sh
source "$_DIR/lib/lease.sh"
# shellcheck source=lib/usage.sh
source "$_DIR/lib/usage.sh"

STATE="$R5_STATE_FILE"

WINDOW_EVENTS='["supervisor_started","pass_started","reviewer_requested",
  "reviewer_dispatched","reviewer_abandoned","findings_received",
  "fix_dispatched","fixes_applied","reviewer_left_tree_dirty",
  "pass_completed","advisory_pass_completed","pass_reopened"]'

_refuse() { printf 'r5-state: %s\n' "$*" >&2; exit 2; }
_bad_arg() { printf 'r5-state: %s\n' "$*" >&2; exit 1; }
_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# _arg <var> <name> <value>: set <var> to a required argument, or exit 1.
_arg() {
  [[ -n "$3" ]] || _bad_arg "$CMD: missing <$2>"
  printf -v "$1" '%s' "$3"
}
# _int <var> <name> <value>: as _arg, for a non-negative integer.
_int() {
  _arg "$1" "$2" "$3"
  [[ "$3" =~ ^[0-9]+$ ]] || _bad_arg "$CMD: <$2> must be a non-negative integer, got '$3'"
}

# Every write is a read-modify-write of one file, made by the orchestrator and
# by concurrent hook invocations: serialise them (lib/flock.sh).
_mutex() {
  mkdir -p "$(dirname "$STATE")" || exit 3
  _r5_flock "$STATE.flock" r5-state
}
_apply() {  # _apply <jq filter> [jq args...]; exit 1 on a bad JSON argument
  local filter="$1"; shift
  local tmp="$STATE.tmp.$$"
  if jq "$@" "$filter" "$STATE" > "$tmp"; then mv "$tmp" "$STATE"; else rm -f "$tmp"; exit 1; fi
}
_append() {  # _append <event-json>
  _apply '.audit_trail += [$e]' --argjson e "$1"
}
_event() {  # _event <name> [jq-args... <filter for the extra fields>]
  local name="$1"; shift
  local extra='{}'
  (( $# )) && extra="$(jq -cn "$@")"
  _append "$(jq -cn --arg t "$(_now)" --arg e "$name" --argjson x "$extra" '$x + {ts:$t, event:$e}')"
}

# ---- reading the state ----
_current_pass() { jq -r '.current_pass // ""' "$STATE"; }
_last_window_event() {  # as JSON, {} if none
  jq -c --argjson w "$WINDOW_EVENTS" '[.audit_trail[] | select(.event as $e | $w | index($e))] | last // {}' "$STATE"
}
_last_window_event_name() { _last_window_event | jq -r '.event // ""'; }
_open_fix() {  # a fix agent dispatched and not yet closed by fixes_applied
  jq -e '(.audit_trail | map(.event)) as $e
    | ($e | indices("fix_dispatched") | max // -1) > ($e | indices("fixes_applied") | max // -1)' \
    "$STATE" >/dev/null
}
_next_pass() {
  jq -r '([.completed_passes[].name]) as $done
    | ([.passes[] | select(. as $p | $done | index($p) | not)] | first) // "excellence"' "$STATE"
}
# The run owner's lock has lapsed: it exists, is the owner's, and its lease
# has run out (a crashed run).
_owner_lease_lapsed() {  # _owner_lease_lapsed <owner>
  [[ -f "$R5_LOCK_FILE" ]] || return 1
  [[ "$(jq -r '.session_id // ""' "$R5_LOCK_FILE" 2>/dev/null)" == "$1" ]] || return 1
  ! _r5_lease_live "$R5_LOCK_FILE"
}

# ---- preconditions ----
# The state file must have the shape init writes; a truncated, emptied or
# hand-edited file cannot say whether a reviewer is in flight, so nothing
# proceeds on it (claim refuses every dispatch) until it is moved aside.
_valid_state() {
  jq -e 'type == "object" and (.status | type) == "string"
    and (.audit_trail | type) == "array" and (.passes | type) == "array"
    and (.completed_passes | type) == "array"
    and (.advisory_results | type) == "array"' "$STATE" >/dev/null 2>&1
}
_need_valid_state() {
  _valid_state || _refuse "$STATE is unreadable or malformed, so nothing can proceed on it; move it aside (keep it: it is the run's audit trail) and start the run again"
}
_need_run() {
  [[ -f "$STATE" ]] || _refuse "no run in flight ($STATE)"
  _need_valid_state
}
# Every subcommand that changes the run (all but init, adopt, claim, show)
# is the owner's alone: a session whose run was adopted by another must not
# write to it, finish it included. An unknown session on either side (a run
# from before ownership was recorded, or no session id in the environment)
# is let through: it cannot be told apart from the owner, and refusing it
# would wedge a run that has no other owner.
_need_owned_run() {
  _need_run
  local owner caller
  owner="$(jq -r '.session_id // ""' "$STATE")"; caller="$(_r5_session)"
  if [[ -n "$owner" && -n "$caller" && "$owner" != "$caller" ]]; then
    printf 'r5-state: the run belongs to session %s, not this one (%s);\n  %s\n' \
      "$owner" "$caller" "$R5_ADOPT_HINT" >&2
    exit 4
  fi
}
_need_current() {  # _need_current <pass>
  [[ "$(_current_pass)" == "$1" ]] || _refuse "$1 is not the current pass"
}
_need_in_flight() {
  [[ "$(_last_window_event_name)" == "reviewer_dispatched" ]] || _refuse "no reviewer is in flight"
}
_need_not_in_flight() {
  [[ "$(_last_window_event_name)" != "reviewer_dispatched" ]] \
    || _refuse "a reviewer is still in flight; record its findings or abandon it first"
}
_need_no_open_fix() {
  ! _open_fix || _refuse "a fix agent is still open; record \`fixed <pass>\` first"
}
_need_last_in() {  # _need_last_in <what> <event>...: the last window event is one of these
  local what="$1" last; shift
  last="$(_last_window_event_name)"
  local e
  for e in "$@"; do [[ "$last" == "$e" ]] && return 0; done
  _refuse "$what cannot follow ${last:-nothing}"
}
_object() {  # _object <json>: a JSON object, or exit 1
  jq -en --argjson x "$1" '$x | type == "object"' >/dev/null 2>&1 \
    || _bad_arg "expected a JSON object, got $1"
}
_blocking_pass() {  # _blocking_pass <name>: one of the four blocking passes
  jq -e --arg p "$1" '.passes | index($p)' "$STATE" >/dev/null || _bad_arg "$1 is not a blocking pass"
}

# Move a finished run's file to r5-state-<bead>-<status>-<ts>.json.
_archive() {
  # Both fields are sanitised: a hand-edited file must not name a path.
  local bead status
  bead="$(jq -r '.target_bead' "$STATE" | tr -c 'A-Za-z0-9._\n-' _)"
  status="$(jq -r '.status' "$STATE" | tr -c 'A-Za-z0-9._\n-' _)"
  mv "$STATE" "$R5_ROOT/.claude/r5-state-${bead}-${status}-$(date -u +%Y%m%dT%H%M%SZ).json" \
    || { printf 'r5-state: could not archive %s\n' "$STATE" >&2; exit 3; }
}

_mutex

CMD="${1:-}"
case "$CMD" in
  init)
    _arg bead bead "${2:-}"; _arg base base "${3:-}"; _arg head head "${4:-}"
    # The bead id names the archive file at `finish`.
    [[ "$bead" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || _bad_arg "init: <bead> must be letters, digits, '.', '_' or '-', got '$bead'"
    if [[ -f "$STATE" ]]; then
      _need_valid_state
      [[ "$(jq -r '.status' "$STATE")" != "in_progress" ]] \
        || _refuse "a run is in flight on $(jq -r '.target_bead' "$STATE"); finish or abort it first"
      _archive  # a finished run whose archive move failed: keep its trail
    fi
    tmp="$STATE.tmp.$$"
    jq -n --arg b "$bead" --arg base "$base" --arg head "$head" --arg t "$(_now)" --arg s "$(_r5_session)" '{
      supervisor: "r5-supervise", target_bead: $b, session_id: $s, started_at: $t,
      passes: ["correctness", "clarity", "edge-cases", "provenance"],
      advisory_passes: ["excellence"], current_pass: null, completed_passes: [],
      advisory_results: [], status: "in_progress",
      diff_range: {base: $base, head: $head},
      audit_trail: [{ts: $t, event: "supervisor_started", session_id: $s}]}' > "$tmp" \
      || { rm -f "$tmp"; exit 3; }
    mv "$tmp" "$STATE" || { rm -f "$tmp"; exit 3; }
    ;;
  adopt)
    _need_run
    me="$(_r5_session)"
    [[ -n "$me" ]] || _bad_arg "no session id in the environment"
    holder="$(jq -r '.session_id // ""' "$R5_LOCK_FILE" 2>/dev/null || true)"
    [[ "$holder" == "$me" ]] || _refuse "this session does not hold the R5 lock; acquire it first"
    from="$(jq -r '.session_id // ""' "$STATE")"
    _apply '.session_id = $s' --arg s "$me"
    _event run_adopted --arg f "$from" --arg s "$me" '{from:$f, session_id:$s}'
    ;;
  pass)
    _need_owned_run; _arg pass pass "${2:-}"
    _need_not_in_flight
    cur="$(_current_pass)"
    [[ -z "$cur" ]] || _refuse "$cur is still the current pass; record it done first"
    next="$(_next_pass)"
    [[ "$pass" == "$next" ]] || _refuse "the next pass is $next, not $pass"
    _need_no_open_fix
    _apply '.current_pass = $p' --arg p "$pass"
    _event pass_started --arg p "$pass" '{pass:$p}'
    ;;
  request)
    _need_owned_run; _arg pass pass "${2:-}"; _int iteration iteration "${3:-}"
    _need_current "$pass"
    _need_not_in_flight
    _need_no_open_fix
    _event reviewer_requested --arg p "$pass" --argjson i "$iteration" '{pass:$p, iteration:$i}'
    ;;
  claim)
    # Exit 0 = let the dispatch through; 2 = refuse; 4 = another session's run.
    subagent="${2:-}"; caller="${3:-$(_r5_session)}"
    [[ -f "$STATE" ]] || exit 0
    _need_valid_state
    [[ "$(jq -r '.status' "$STATE")" == "in_progress" ]] || exit 0
    owner="$(jq -r '.session_id // ""' "$STATE")"
    # An unknown session on either side is treated as the run's own, so the
    # ownership check fails closed.
    if [[ -n "$owner" && -n "$caller" && "$owner" != "$caller" ]]; then
      _owner_lease_lapsed "$owner" && exit 0
      printf 'r5-state: the R5 run on %s belongs to session %s, whose lock is still live, or absent;\n  %s\n' \
        "$(jq -r '.target_bead' "$STATE")" "$owner" "$R5_ADOPT_HINT" >&2
      exit 4
    fi
    pass="$(_current_pass)"
    case "$(_last_window_event_name)" in
      pass_started|reviewer_requested)
        _event reviewer_dispatched --arg p "$pass" --arg s "$subagent" '{pass:$p, subagent_type:$s}'
        exit 0 ;;
      reviewer_dispatched)
        if [[ "$pass" == excellence ]]; then report="advisory"; else report="findings $pass"; fi
        _refuse "the $pass reviewer is in flight; record its report (\`$report ...\`), or abandon it if it stalled or never started" ;;
      reviewer_abandoned)
        _refuse "the $pass reviewer was abandoned; request it again before dispatching" ;;
      *)
        [[ -n "$pass" ]] || exit 0  # between passes: not the run's business
        ! _open_fix || _refuse "a fix agent is open; record \`fixed $pass\` when it is done"
        _event fix_dispatched --arg p "$pass" --arg s "$subagent" '{pass:$p, subagent_type:$s}'
        exit 0 ;;
    esac
    ;;
  findings)
    _need_owned_run; _arg pass pass "${2:-}"
    _int crit critical "${3:-}"; _int imp important "${4:-}"; _int minor minor "${5:-}"
    _blocking_pass "$pass"
    _need_current "$pass"
    _need_in_flight
    _event findings_received --arg p "$pass" --argjson c "$crit" --argjson i "$imp" \
      --argjson m "$minor" --arg v "${6:-}" \
      '{pass:$p, counts:{critical:$c, important:$i, minor:$m}, verdict:$v}'
    ;;
  abandon)
    _need_owned_run; _arg pass pass "${2:-}"; _arg reason reason "${3:-}"
    _need_current "$pass"
    _need_in_flight
    _event reviewer_abandoned --arg p "$pass" --arg r "$reason" '{pass:$p, reason:$r}'
    ;;
  fixed)
    _need_owned_run; _arg pass pass "${2:-}"; extra="${3:-}"; [[ -n "$extra" ]] || extra='{}'
    _object "$extra"
    _need_current "$pass"
    # A fix may follow the findings, a fix agent, an earlier fix (a round
    # in several steps), or a dirty tree (provenance's restore).
    _need_last_in "fixed" findings_received fix_dispatched fixes_applied reviewer_left_tree_dirty
    _event fixes_applied --argjson x "$extra" --arg p "$pass" '$x + {pass:$p}'
    ;;
  tree-dirty)
    _need_owned_run; _arg pass pass "${2:-}"
    _need_current "$pass"
    _need_last_in "tree-dirty" findings_received
    _event reviewer_left_tree_dirty --arg p "$pass" '{pass:$p}'
    ;;
  reopen)
    # A change after a pass was done goes back through that pass and every
    # later one: drop them (and any advisory read) from completed_passes, so
    # `pass` starts again at <pass>. The trail keeps the dropped results.
    _need_owned_run; _arg pass pass "${2:-}"
    _blocking_pass "$pass"
    _need_not_in_flight
    _need_no_open_fix
    jq -e --arg p "$pass" '[.completed_passes[].name] | index($p)' "$STATE" >/dev/null \
      || _refuse "$pass is not done, so there is nothing to reopen"
    dropped="$(jq -c --arg p "$pass" '(.passes | index($p)) as $i | .passes[$i:] as $later
      | [.completed_passes[].name | select(. as $n | $later | index($n))]' "$STATE")"
    _apply '(.passes | index($p)) as $i | .passes[$i:] as $later
      | .completed_passes |= map(select(.name as $n | $later | index($n) | not))
      | .advisory_results = [] | .current_pass = null' --arg p "$pass"
    _event pass_reopened --arg p "$pass" --argjson d "$dropped" '{pass:$p, dropped:$d}'
    ;;
  event)
    _need_owned_run; _arg name event "${2:-}"; extra="${3:-}"; [[ -n "$extra" ]] || extra='{}'
    _object "$extra"
    if jq -en --argjson w "$WINDOW_EVENTS" --arg e "$name" '$w | index($e)' >/dev/null; then
      _bad_arg "$name is a window event, written by its own subcommand"
    fi
    _event "$name" --argjson x "$extra" '$x'
    ;;
  done)
    _need_owned_run; _arg pass pass "${2:-}"; _int iters fix_iterations "${3:-}"; extra="${4:-}"; [[ -n "$extra" ]] || extra='{}'
    _object "$extra"
    _blocking_pass "$pass"
    _need_current "$pass"
    # The last window event must be this pass's findings: anything after
    # them (a re-request, a dispatch, a fix, a dirty tree) is unreviewed.
    verdict="$(_last_window_event | jq -r --arg p "$pass" '
      if (.event != "findings_received" or .pass != $p) then "followed by later activity"
      elif (.counts.critical == 0 and .counts.important == 0) then "clean"
      else "not clean" end')"
    [[ "$verdict" == "clean" ]] || _refuse "$pass cannot be done: its findings are $verdict"
    _apply '.completed_passes += [($x + {name:$p, fix_iterations:$n, completed_at:$t})] | .current_pass = null' \
      --arg p "$pass" --argjson n "$iters" --arg t "$(_now)" --argjson x "$extra"
    _event pass_completed --arg p "$pass" '{pass:$p}'
    ;;
  advisory)
    _need_owned_run
    _int crit critical "${2:-}"; _int imp important "${3:-}"; _int minor minor "${4:-}"
    _arg verdict verdict "${5:-}"
    _need_current excellence
    _need_in_flight
    rec="$(jq -cn --arg t "$(_now)" --argjson c "$crit" --argjson i "$imp" --argjson m "$minor" --arg v "$verdict" \
      '{ts:$t, event:"advisory_pass_completed", pass:"excellence", counts:{critical:$c, important:$i, minor:$m}, verdict:$v}')"
    _apply '.advisory_results += [$r] | .current_pass = null' --argjson r "$rec"
    _append "$rec"
    ;;
  finish)
    _need_owned_run; _arg status status "${2:-}"
    case "$status" in completed|failed|aborted) ;; *) _bad_arg "bad status $status";; esac
    if [[ "$status" == completed ]]; then
      missing="$(jq -r '[.passes[] as $p | select([.completed_passes[].name] | index($p) | not) | $p]
        + (if (.advisory_results | length) == 0 then ["excellence"] else [] end)
        + (if .current_pass != null then ["(" + .current_pass + " is still current)"] else [] end)
        | join(", ")' "$STATE")"
      [[ -z "$missing" ]] || _refuse "the run is not complete: $missing"
    fi
    # Repeating a finish whose archive move failed only retries the move.
    if [[ "$(jq -r '.status' "$STATE")" == in_progress ]]; then
      _apply '.status = $s | .current_pass = null' --arg s "$status"
      _event "supervisor_$status"
    fi
    _archive
    ;;
  open-window)
    # (r5-lock.sh) "<owner> <bead> <event>" if the run has a reviewer or a
    # fix agent in flight; nothing otherwise. Read-only.
    [[ -f "$STATE" ]] && _valid_state || exit 0
    [[ "$(jq -r '.status' "$STATE")" == "in_progress" ]] || exit 0
    last="$(_last_window_event_name)"
    if [[ "$last" == reviewer_dispatched ]]; then event=reviewer
    elif _open_fix; then event=fix-agent
    else exit 0; fi
    printf '%s %s %s\n' "$(jq -r 'if (.session_id // "") == "" then "-" else .session_id end' "$STATE")" \
      "$(jq -r '.target_bead' "$STATE")" "$event"
    ;;
  show) _need_run; cat "$STATE" ;;
  *) _r5_usage "${BASH_SOURCE[0]}" ;;
esac
