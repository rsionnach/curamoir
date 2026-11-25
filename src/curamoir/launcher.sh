# Interpreter selection for every cúramóir launcher in scripts/ (cm-6f0).
# Sourced, never run: the five hook launchers, scripts/curamoir and
# scripts/curamoir-lint all choose and start Python here, and a test asserts
# that no file in scripts/ starts an interpreter itself. It lives in src/, not
# scripts/, so the lint's standalone layout stays `scripts/curamoir-lint` and
# the `src/` beside it (E2-R5). Every launcher guards the source: a source of
# a missing file that went on would exit 1, which the lint reads as
# violations. Not needed by the installed package, which has no launchers.
#
# Why every launch is `-P -E` with a bootstrap that inserts our own src/:
# `python -m` and `python -c` put the working directory first on sys.path,
# and a hook's working directory is whatever repository is open (or the
# sweep's clone). A `curamoir/` package or a `yaml.py` committed there was
# imported, and executed with the developer's privileges, in place of ours.
# `-P` drops that entry. `-E` ignores every PYTHON* variable, so a relative
# PYTHONPATH entry (read against that same working directory) cannot do the
# same for a third-party import. `-I` would also do both, but it drops the
# user's site-packages too, where PyYAML often lives, and a hook that cannot
# import fails open. So the user's PYTHONPATH is no longer honoured by any
# launcher; /curamoir:doctor says so when it is set.
#
# Which interpreter is chosen through PATH stays a stated limit: whoever
# controls PATH controls which `python3.14` runs.

# Most specific first. The loop used to be `python3 python`, which takes the
# first interpreter that EXISTS rather than one that can run this code — so on
# a machine whose `python3` predates the floor in pyproject.toml, every hook
# died on an import and, because command hooks fail OPEN, protected nothing
# while looking installed. /curamoir:doctor probes the same list, in the same
# order (doctor.WRAPPER_INTERPRETERS; a test asserts they agree).
CURAMOIR_PYTHON_CANDIDATES="python3.14 python3.13 python3.12 python3 python"

# The floor, as pyproject.toml's requires-python states it. Shown in the one
# line the CLI and the lint print when nothing qualifies.
CURAMOIR_PYTHON_FLOOR="3.12"

# The versioned names are at or above the floor by name and all accept -P, so
# they are taken unprobed: a hook runs on every Bash call and a probe costs
# tens of milliseconds. `python3` and `python` could be anything, so they are
# probed, in one call, for both: an interpreter without -P (before 3.11)
# exits 2 before running anything, and one below the floor exits 1. Taking one
# that lacks -P is not an option: Python exits 2 on the unknown flag, and a
# hook that exits 2 BLOCKS (every Bash call, or every attempt to stop).
_curamoir_qualifies() {
  case "$1" in
    python3.14 | python3.13 | python3.12) return 0 ;;
  esac
  "$1" -P -E -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
    </dev/null >/dev/null 2>&1
}

# curamoir_choose_python [prefer-yaml]
# Sets CURAMOIR_PYTHON to the first candidate that exists and qualifies, or to
# the empty string when none does. With `prefer-yaml`, the first qualifying
# one that can import PyYAML wins, falling back to the first qualifying one:
# a bare Homebrew python3.14 beside a python3 that has PyYAML is ordinary
# (cm-kry), and every command that reads curamoir.yaml needs it, while the
# fallback still runs far enough to say what is missing. The hooks do not ask
# for it: one probe per candidate is affordable for a command, not on every
# Bash call. The PyYAML probe runs with the same flags as every launch, so a
# planted yaml.py in the working directory is not imported by the probe either.
curamoir_choose_python() {
  CURAMOIR_PYTHON=""
  local interpreter
  for interpreter in $CURAMOIR_PYTHON_CANDIDATES; do
    command -v "$interpreter" >/dev/null 2>&1 || continue
    _curamoir_qualifies "$interpreter" || continue
    if [ "${1:-}" != "prefer-yaml" ]; then
      CURAMOIR_PYTHON="$interpreter"
      return 0
    fi
    [ -n "$CURAMOIR_PYTHON" ] || CURAMOIR_PYTHON="$interpreter"
    if "$interpreter" -P -E -c "import yaml" </dev/null >/dev/null 2>&1; then
      CURAMOIR_PYTHON="$interpreter"
      return 0
    fi
  done
  [ -n "$CURAMOIR_PYTHON" ]
}

# The one line the CLI and the lint print when no interpreter qualifies,
# after their own prefix. printf is a builtin: this runs on a bare PATH.
curamoir_no_python_message() {
  printf 'needs Python %s or later on PATH (as python3.14, python3.13, python3.12, python3 or python), and found none that qualifies.' \
    "$CURAMOIR_PYTHON_FLOOR"
}

# curamoir_exec_module <src dir> <module> [args...]
# Replaces this shell with CURAMOIR_PYTHON running <module> as __main__, with
# <src dir> first on sys.path and nothing taken from the working directory or
# the PYTHON* variables.
curamoir_exec_module() {
  local src="$1" module="$2"
  shift 2
  exec "$CURAMOIR_PYTHON" -P -E -c \
    'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module(sys.argv.pop(1), run_name="__main__", alter_sys=True)' \
    "$src" "$module" "$@"
}
