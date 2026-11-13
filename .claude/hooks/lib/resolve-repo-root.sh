#!/usr/bin/env bash
# resolve-repo-root.sh — single source of truth for where curamoir's R5
# supervisor keeps its lock and state. Sourced by r5-lock.sh, r5-state.sh
# and r5-parallel-block.sh, so the helpers and the hook can never resolve to
# two different places (two lockfiles would make the lock a no-op).
#
# Ported from nthlayer-ecosystem/.claude/hooks/lib/resolve-ecosystem-root.sh
# (cm-99l). curamoir is one repository, so the root is the MAIN worktree of
# the repository that contains these helpers: resolved through `git
# --git-common-dir`, so a sibling worktree shares its parent's lock and
# state. Resolving from the helpers' own location, not $PWD, matters for the
# hook: a session whose cwd has moved out of the repository (or into another
# one) must still be checked against this repository's run.
#
# Exports: R5_ROOT (absolute path; no trailing slash), and the two files
# every helper shares under it: R5_LOCK_FILE (the run lock, r5-lock.sh) and
# R5_STATE_FILE (the run's audit trail, r5-state.sh).
# Returns non-zero if the root cannot be located — callers must handle.
#
# Resolution order:
#   1. $CURAMOIR_R5_ROOT (explicit override; tests use it).
#   2. The main worktree of the git repository containing this file, if it
#      is a curamoir checkout (.claude-plugin/plugin.json names "curamoir").

# R5_ROOT is always resolved here, never trusted from the environment: an
# inherited value would silently move the lock and the state elsewhere.
unset R5_ROOT R5_LOCK_FILE R5_STATE_FILE

_r5_export_root() {
  R5_ROOT="$1"
  R5_LOCK_FILE="$R5_ROOT/.claude/r5-supervisor.lock"
  R5_STATE_FILE="$R5_ROOT/.claude/r5-state.json"
  export R5_ROOT R5_LOCK_FILE R5_STATE_FILE
}

if [[ -n "${CURAMOIR_R5_ROOT:-}" ]]; then
  if [[ -d "$CURAMOIR_R5_ROOT" ]]; then
    _r5_export_root "$(cd "$CURAMOIR_R5_ROOT" && pwd)"
    return 0 2>/dev/null || exit 0
  fi
  printf 'resolve-repo-root: CURAMOIR_R5_ROOT is not a directory: %s\n' "$CURAMOIR_R5_ROOT" >&2
  return 1 2>/dev/null || exit 1
fi

_here="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
_common_dir="$(git -C "$_here" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || _common_dir=""
if [[ -n "$_common_dir" ]]; then
  _root="$(cd "$(dirname "$_common_dir")" 2>/dev/null && pwd)" || _root=""
  if [[ -n "$_root" && -f "$_root/.claude-plugin/plugin.json" ]] \
      && grep -q '"name": *"curamoir"' "$_root/.claude-plugin/plugin.json" 2>/dev/null; then
    _r5_export_root "$_root"
    return 0 2>/dev/null || exit 0
  fi
fi

printf 'resolve-repo-root: %s is not inside a curamoir checkout\n' "$_here" >&2
return 1 2>/dev/null || exit 1
