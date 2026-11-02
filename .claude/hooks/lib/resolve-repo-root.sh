#!/usr/bin/env bash
# resolve-repo-root.sh — single source of truth for where curamoir's R5
# supervisor keeps its lock and state. Sourced by r5-lock.sh and
# r5-parallel-block.sh so the helper and the hook can never resolve to two
# different places (two lockfiles would make the mutex a no-op).
#
# Ported from nthlayer-ecosystem/.claude/hooks/lib/resolve-ecosystem-root.sh
# (cm-99l). curamoir is one repository, so the root is the MAIN worktree of
# the repository containing $PWD: resolved through `git --git-common-dir`,
# so a sibling worktree shares its parent's lock and state.
#
# Exports: R5_ROOT (absolute path; no trailing slash).
# Returns non-zero if the root cannot be located — callers must handle.
#
# Resolution order:
#   1. $CURAMOIR_R5_ROOT (explicit override; tests use it).
#   2. The main worktree of the git repository containing $PWD, if it is a
#      curamoir checkout (.claude-plugin/plugin.json names "curamoir").

# R5_ROOT is always resolved here, never trusted from the environment: an
# inherited value would silently move the lock and the state elsewhere.
unset R5_ROOT

if [[ -n "${CURAMOIR_R5_ROOT:-}" ]]; then
  if [[ -d "$CURAMOIR_R5_ROOT" ]]; then
    R5_ROOT="$(cd "$CURAMOIR_R5_ROOT" && pwd)"
    export R5_ROOT
    return 0 2>/dev/null || exit 0
  fi
  printf 'resolve-repo-root: CURAMOIR_R5_ROOT is not a directory: %s\n' "$CURAMOIR_R5_ROOT" >&2
  return 1 2>/dev/null || exit 1
fi

_common_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || _common_dir=""
if [[ -n "$_common_dir" ]]; then
  _root="$(cd "$(dirname "$_common_dir")" 2>/dev/null && pwd)" || _root=""
  if [[ -n "$_root" && -f "$_root/.claude-plugin/plugin.json" ]] \
      && grep -q '"name": *"curamoir"' "$_root/.claude-plugin/plugin.json" 2>/dev/null; then
    R5_ROOT="$_root"
    export R5_ROOT
    return 0 2>/dev/null || exit 0
  fi
fi

printf 'resolve-repo-root: not inside a curamoir checkout (cwd %s)\n' "$PWD" >&2
return 1 2>/dev/null || exit 1
