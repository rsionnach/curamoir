#!/usr/bin/env bash
# usage.sh — print a helper's usage from its header comment (cm-99l), so the
# usage text cannot drift from the documentation. Sourced, not run.
#
# _r5_usage <script>: prints the script's comment lines from line 2 up to
# the first non-comment line, to stderr, and exits 1.
_r5_usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$1" >&2
  exit 1
}
