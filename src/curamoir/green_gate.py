"""E3-R4: the sweep runs the project's tests before opening a PR, and never
opens a red one (cm-fkd.5).

E3-R4: "The sweep MUST run the project's test command (from config or
detected) before opening the PR. On failure it MUST revert to the last green
state or reduce scope, and MUST NOT open a red PR."

run_test_gate(sandbox, verdict) runs after fix_scope.verify_fix accepted the
fix and before sandbox.patch: the runner's order is fix_scope's module
docstring, "What the runner (cm-fkd.4) must do". It returns a GateResult
whose `paths_for_pr` is the only thing the runner may patch, and whose
`strategy` the ledger records (ledger_fields), because the acceptance
criterion asks which strategy was used.

**Where the tests run** (user decision on cm-fkd, 2026-10-12): inside the
sandbox, so the fixed code is what is tested, never in the user's checkout.
The sandbox is a fresh clone with no venv or node_modules, so the SOURCE
checkout's environment is reused read-only (sandbox_environment): its
`.venv` (or `venv`) by VIRTUAL_ENV and PATH, its `node_modules/.bin` on PATH
and its `node_modules` on NODE_PATH. No network, no provisioning.

**The command** (resolve_test_command) is `sweep.test_command` from the
BASE commit's `.curamoir/curamoir.yaml`, read from git's objects like the
base principles, never the edited tree: a fix that rewrote the config to
`true` would otherwise pass itself. Empty means detect, from the base's
tracked files, and only where the answer is not a guess:

- a Python project marker (scaffold.PYTHON_MARKERS) at the root AND a
  tracked `test_*.py` or `*_test.py` -> `python3 -m pytest -q`. pytest
  also runs unittest-style tests. `python3`, not `python`: a venv has both,
  and macOS without one has only `python3`;
- a root `package.json` whose `scripts.test` is not npm's placeholder
  (`no test specified`) -> `npm test`;
- both, or neither -> no command. A config or detection failure is never
  green: E3-R4 says the tests MUST run before a PR, so the sweep opens none
  (strategy `abandoned`, cause `no_test_command` or `config_invalid`).

It is run by `/bin/sh -c`, as the owner would type it: it is the project
owner's committed text (PLANTED.json's is `PYTHONPATH=src python3 -m pytest
-q`, which needs a shell), and running it through a shell is what "the
project's test command" means. Hardened like git in git_guard: cwd = the
sandbox, its own process group, killed whole at `sweep.test_timeout_seconds`
(a timeout is red), every GIT_* variable dropped, and
PYTHONDONTWRITEBYTECODE=1 so importing from the source's venv writes no
`.pyc` into the user's checkout.

**On red, reduce scope** before abandoning. First the base is run (every
changed path back at its base content): a red base means the tests cannot
judge any fix, so nothing is opened (cause `base_red`). Otherwise
reduce_scope keeps the largest green set it can find, deterministically, a
path at a time (per path because verify_fix accepts and sandbox.patch
carries paths, not hunks): starting from the green base, it tries adding
the candidates as a group, keeps a group whose addition is green, and
splits a red group in halves until a single red path is dropped. A set
already run is not run again, so the full set (known red) is never re-run.
One bad fix among n costs about 2*log2(n) runs; an interaction (two fixes
green alone, red together) keeps the first and drops the second. The set
handed to the PR is always one that a run found green.

`sweep.max_test_runs` (default 12, the full and base runs included) bounds
it: 12 always isolates one bad fix among `sweep.max_files_per_pr` (20)
(tests/test_green_gate.py checks every position). What the cap leaves
untested is dropped, never kept: an untested fix waits for the next sweep,
which finds its violation again.

**The revert is not git's.** E0-R5 denies `git checkout -- <file>`, `git
reset --hard`, `git stash` and `git clean`. Every path's base state (mode
and blob, `git ls-tree`/`cat-file` through git_guard's hardened git) and
fixed state (read from the sandbox) are taken into memory before the first
run, and each run's tree is written from them by this module, with no git
command between runs. Before each run the whole candidate state is
rewritten, so a test that edits a fixed file cannot carry that edit into
the next run or into the PR.

**Limits, stated rather than hidden.**

- The tests run the fixed code, which the fix agent wrote, with the user's
  privileges. E3-R2's review bounds what changed; it cannot prove the
  change harmless, and nothing here is an operating-system sandbox. A test
  command can write wherever the user can, the source's venv included
  (PYTHONDONTWRITEBYTECODE stops only Python's own bytecode).
- The environment is the user's checkout, not necessarily the base's
  lockfile: a venv that is ahead of or behind the base can turn a green
  base red (then nothing is opened) or the reverse.
- NODE_PATH is read by CommonJS `require` only; ES modules ignore it, so a
  project whose tests import packages as ESM may be red in the sandbox.
- The sandbox's git files are held to a snapshot taken before the first
  run (git_guard.verify_git_state); a test that writes `.git`, a
  `.gitattributes` or a nested repository makes the gate raise GateError
  (a refusal), since the patch is built by git in that sandbox next.
- Flaky tests: a flake can drop a good fix (safe) or make the full run
  red and the base green by chance; a green run is trusted once.
- Dropping is per path, so a file with one good and one bad fix loses both.
"""

import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional

from curamoir.config import DEFAULTS, load_config_text
from curamoir.git_guard import (
    GitGuardError,
    git,
    require_sandbox,
    run_in_group,
    scrubbed_environment,
    snapshot_git_state,
    verify_git_state,
)
from curamoir.scaffold import PYTHON_MARKERS

#: Strategies: strings, because the ledger line records them.
GREEN = "green"
REDUCED = "reduced"
ABANDONED = "abandoned"

#: Why a sweep was abandoned.
NO_TEST_COMMAND = "no_test_command"
CONFIG_INVALID = "config_invalid"
BASE_RED = "base_red"
NO_GREEN_SUBSET = "no_green_subset"
#: The cap was reached before the base could be run.
RUN_CAP = "run_cap"

PASSED = "passed"
FAILED = "failed"
TIMED_OUT = "timed_out"

PYTHON_COMMAND = "python3 -m pytest -q"
NODE_COMMAND = "npm test"

_CONFIG = ".curamoir/curamoir.yaml"
_PYTHON_TEST_FILE = re.compile(r"(^test_.*|.*_test)\.py$")
#: Characters of a run's output kept for the PR body and diagnosis.
_TAIL = 4000

_RED_REASON = ("the tests fail with this fix applied on top of the fixes kept before it, "
               "so it is left for a later sweep")


class GateError(GitGuardError):
    """The gate cannot answer: the tree could not be set to a state it must
    test, or the test run changed the sandbox's git files. The runner treats
    it as a refusal, like every GitGuardError: no PR."""


@dataclass(frozen=True)
class GateCommand:
    """The test command resolve_test_command chose: `command` (None when
    there is none), `source` ("config" or "detected"), the `timeout` and
    `max_runs` from the base's config, and, when there is no command, its
    `cause` and `why`."""

    command: Optional[str]
    source: Optional[str]
    timeout: int
    max_runs: int
    cause: Optional[str] = None
    why: str = ""


@dataclass(frozen=True)
class GateRun:
    """One run of the test command: the fixed `paths` applied (every other
    changed path at its base), its `outcome` (passed, failed, timed_out),
    exit `code` (None on timeout), `seconds`, and the `tail` of its output."""

    paths: tuple
    outcome: str
    code: Optional[int]
    seconds: float
    tail: str

    @property
    def passed(self):
        return self.outcome == PASSED


@dataclass(frozen=True)
class DroppedFix:
    """A fixed path left out of the PR, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class GateResult:
    """What the gate decided. `kept` is the green set (sorted paths);
    `paths_for_pr` is it when the strategy is green or reduced and () when
    abandoned: the runner patches `paths_for_pr` and nothing else."""

    strategy: str
    cause: Optional[str]
    command: Optional[str]
    command_source: Optional[str]
    kept: tuple
    dropped: tuple
    runs: tuple
    why: str = ""

    @property
    def paths_for_pr(self):
        return self.kept if self.strategy in (GREEN, REDUCED) else ()

    def render(self):
        if self.strategy == GREEN:
            return f"tests green with all {len(self.kept)} fixed file(s) ({len(self.runs)} run)"
        if self.strategy == REDUCED:
            return (f"tests red with every fix; reduced to {len(self.kept)} fixed file(s) "
                    f"that are green, dropping {', '.join(d.path for d in self.dropped)} "
                    f"({len(self.runs)} runs)")
        return f"no PR: {self.why or self.cause}"

    def ledger_fields(self):
        """Keyword fields for the sweep's one ledger line (job_run.JobRun's
        succeeded/failed/refused take them): `test_gate` and nothing else, so
        cm-fkd.6's per-principle counts sit beside it, not inside it."""
        return {"test_gate": {
            "strategy": self.strategy,
            "cause": self.cause,
            "command": self.command,
            "command_source": self.command_source,
            "kept": list(self.kept),
            "dropped": [{"path": d.path, "reason": d.reason} for d in self.dropped],
            "runs": [{"fixed": len(run.paths), "outcome": run.outcome, "code": run.code,
                      "seconds": round(run.seconds, 3)} for run in self.runs],
        }}


# --- the command ----------------------------------------------------------------------

def resolve_test_command(sandbox):
    """The GateCommand for `sandbox`'s base commit (module docstring): the
    configured `sweep.test_command`, else a detected one, else none."""
    require_sandbox(sandbox, GateError)
    data = sandbox.show(_CONFIG)
    sweep = DEFAULTS["sweep"]
    if data is not None:
        try:
            sweep = load_config_text(data.decode("utf-8"))["sweep"]
        # WHY so broad: load_config_text wraps a schema error and missing
        # PyYAML in ConfigError, but a YAML syntax error reaches here as
        # PyYAML's own type, which cannot be named without importing it.
        # Whatever the failure, the command is unknown, and unknown is red.
        except Exception as error:
            return GateCommand(None, None, sweep["test_timeout_seconds"],
                               sweep["max_test_runs"], CONFIG_INVALID,
                               f"the base commit's {_CONFIG} cannot be loaded, so its test "
                               f"command is unknown: {error}")
    timeout, max_runs = sweep["test_timeout_seconds"], sweep["max_test_runs"]
    configured = sweep["test_command"].strip()
    if configured:
        return GateCommand(configured, "config", timeout, max_runs)
    detected, why = _detect(sandbox)
    if detected is None:
        return GateCommand(None, None, timeout, max_runs, NO_TEST_COMMAND, why)
    return GateCommand(detected, "detected", timeout, max_runs)


def _detect(sandbox):
    names = [name.decode("utf-8", "surrogateescape") for name in
             git(sandbox.path, "ls-tree", "-r", "-z", "--name-only", sandbox.base).split(b"\0")
             if name]
    found = []
    has_marker = any(marker in names for marker in PYTHON_MARKERS)
    has_tests = any(_PYTHON_TEST_FILE.match(PurePosixPath(name).name) for name in names)
    if has_marker and has_tests:
        found.append(PYTHON_COMMAND)
    if "package.json" in names and _npm_test_script(sandbox.show("package.json")):
        found.append(NODE_COMMAND)
    if len(found) == 1:
        return found[0], ""
    if found:
        return None, (f"both {' and '.join(found)} were detected; set sweep.test_command "
                      "to say which (or both, joined with &&)")
    return None, ("no test command is configured (sweep.test_command) and none was detected (a Python "
                  "project marker with test files, or a package.json `test` script); "
                  "set sweep.test_command in .curamoir/curamoir.yaml and commit it")


def _npm_test_script(data):
    try:
        script = json.loads(data.decode("utf-8")).get("scripts", {}).get("test")
    except (AttributeError, UnicodeDecodeError, ValueError):
        return False
    # `npm init` writes a test script that always fails.
    return isinstance(script, str) and bool(script.strip()) \
        and "no test specified" not in script


def sandbox_environment(source):
    """The environment the test command runs with: the inherited one minus
    every GIT_* variable, with the SOURCE checkout's `.venv` (or `venv`: a
    directory holding `pyvenv.cfg` and `bin/`) activated as its activate
    script would (VIRTUAL_ENV, its bin first on PATH, PYTHONHOME unset),
    its `node_modules/.bin` on PATH and `node_modules` first on NODE_PATH,
    and PYTHONDONTWRITEBYTECODE=1. Without a source venv the inherited
    VIRTUAL_ENV and PATH stand: that is the environment the sweep was
    started in. PYTHONPATH is left as it is; a project that needs one says
    so in its command."""
    env = scrubbed_environment()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    prefix = []
    for name in (".venv", "venv"):
        venv = os.path.join(str(source), name)
        if os.path.isfile(os.path.join(venv, "pyvenv.cfg")) and \
                os.path.isdir(os.path.join(venv, "bin")):
            env["VIRTUAL_ENV"] = venv
            env.pop("PYTHONHOME", None)
            prefix.append(os.path.join(venv, "bin"))
            break
    modules = os.path.join(str(source), "node_modules")
    if os.path.isdir(modules):
        if os.path.isdir(os.path.join(modules, ".bin")):
            prefix.append(os.path.join(modules, ".bin"))
        env["NODE_PATH"] = os.pathsep.join(
            [modules] + ([env["NODE_PATH"]] if env.get("NODE_PATH") else []))
    if prefix:
        env["PATH"] = os.pathsep.join([*prefix, env.get("PATH", os.defpath)])
    return env


def _run_command(sandbox, command, env, timeout, paths):
    started = time.monotonic()
    env = dict(env, PWD=str(sandbox.path))
    try:
        done = run_in_group(["/bin/sh", "-c", command], cwd=str(sandbox.path), env=env,
                            timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        return GateRun(paths, TIMED_OUT, None, time.monotonic() - started,
                       _tail(expired.output, expired.stderr)
                       + f"\n[killed after {timeout}s with its process group]")
    except OSError as error:
        raise GateError(f"the test command could not be started: {error}") from error
    return GateRun(paths, PASSED if done.returncode == 0 else FAILED, done.returncode,
                   time.monotonic() - started, _tail(done.stdout, done.stderr))


def _tail(out, err):
    text = (out or b"").decode("utf-8", "replace") + (err or b"").decode("utf-8", "replace")
    return text[-_TAIL:]


# --- the reduction --------------------------------------------------------------------

class Trials:
    """`run(subset) -> bool` (green?), memoised by set and capped at
    `max_runs` real runs. Calling it with a set already run returns that
    answer without running; past the cap it returns None."""

    def __init__(self, run, max_runs):
        self._run = run
        self.max_runs = max_runs
        self.runs = 0
        self._known = {}

    def __call__(self, subset):
        key = frozenset(subset)
        if key in self._known:
            return self._known[key]
        if self.runs >= self.max_runs:
            return None
        self.runs += 1
        self._known[key] = bool(self._run(tuple(sorted(key))))
        return self._known[key]


def reduce_scope(candidates, trials):
    """(kept, dropped): the green subset of `candidates` (sorted paths)
    found by adding groups to the green base and halving red ones (module
    docstring), and a DroppedFix for every other candidate. `trials` is a
    Trials whose base (the empty set) is known green and whose full set is
    known red."""
    kept = []
    dropped = []
    pending = [list(candidates)]
    while pending:
        group = pending.pop(0)
        green = trials(kept + group)
        if green is None:
            reason = (f"not tested: sweep.max_test_runs ({trials.max_runs}) was reached "
                      "first, and an untested fix is never kept")
            dropped.extend(DroppedFix(path, reason)
                           for rest in [group, *pending] for path in rest)
            break
        if green:
            kept.extend(group)
        elif len(group) == 1:
            dropped.append(DroppedFix(group[0], _RED_REASON))
        else:
            half = (len(group) + 1) // 2
            pending[0:0] = [group[:half], group[half:]]
    return tuple(sorted(kept)), tuple(sorted(dropped, key=lambda d: d.path))


# --- the gate -------------------------------------------------------------------------

def run_test_gate(sandbox, verdict, paths=None):
    """E3-R4 for one sweep: run the base's test command in `sandbox` with
    the fix `verdict` (fix_scope.verify_fix's, ok, with paths) accepted,
    reduce scope on red, and return a GateResult. `paths` (default
    `verdict.paths`) is the subset the runner means to open a PR with, if it
    trimmed one: every other path of the verdict is at its base content for
    every run, so the tree tested is the PR's.

    The sandbox is left with the kept paths fixed and every other path of
    the verdict at its base. ValueError if the verdict is not ok, changed
    nothing, or `paths` is not a non-empty subset of it; GateError if the
    sandbox is not one, a path's state cannot be held or restored, or the
    test runs changed the sandbox's git files."""
    require_sandbox(sandbox, GateError)
    if not verdict.ok or not verdict.paths:
        raise ValueError("the test gate runs only on an ok verdict that changed something")
    candidates = tuple(sorted(set(verdict.paths if paths is None else paths)))
    if not candidates or not set(candidates) <= set(verdict.paths):
        raise ValueError("paths must be a non-empty subset of the verdict's paths")
    chosen = resolve_test_command(sandbox)
    if chosen.command is None:
        return GateResult(ABANDONED, chosen.cause, None, None, (), (), (), chosen.why)

    # Everything git is asked happens here, before the first run.
    every = tuple(sorted(verdict.paths))
    fixed = {path: _work_tree_state(sandbox.path, path) for path in every}
    base = {path: _base_state(sandbox, path) for path in every}
    env = sandbox_environment(sandbox.source)
    git_state = snapshot_git_state(sandbox)
    runs = []

    def run(subset):
        _write_states(sandbox.path, {p: fixed[p] if p in subset else base[p] for p in every})
        runs.append(_run_command(sandbox, chosen.command, env, chosen.timeout, subset))
        return runs[-1].passed

    trials = Trials(run, chosen.max_runs)
    kept, dropped, cause, why = (), (), None, ""
    full = trials(candidates)
    if full:
        kept, strategy = candidates, GREEN
    else:
        green_base = None if full is None else trials(())
        if green_base:
            kept, dropped = reduce_scope(candidates, trials)
        strategy = REDUCED if kept else ABANDONED
        if full is None or green_base is None:
            cause, why = RUN_CAP, (f"sweep.max_test_runs ({chosen.max_runs}) allows too few "
                                   "runs to test the fix and the base")
        elif not green_base:
            cause, why = BASE_RED, ("the tests fail at the base commit itself, with no fix "
                                    "applied, so they cannot tell a good fix from a bad one")
        elif not kept:
            cause, why = NO_GREEN_SUBSET, "no fixed file is green on top of the base"
        if strategy == ABANDONED and not dropped:
            dropped = tuple(DroppedFix(path, why) for path in candidates)
    changed = verify_git_state(git_state)
    if changed:
        raise GateError("the test command changed the sandbox's git files ("
                        + ", ".join(f"{c.label} {c.change}" for c in changed)
                        + "), and the patch would be built by git in that sandbox")
    _write_states(sandbox.path, {p: fixed[p] if p in kept else base[p] for p in every})
    return GateResult(strategy, cause, chosen.command, chosen.source, kept, dropped,
                      tuple(runs), why)


# --- file states ----------------------------------------------------------------------

@dataclass(frozen=True)
class _State:
    """A path's content: `data` None for absent, else the bytes of a regular
    file, `executable` or not (all git records of a file's mode)."""

    data: Optional[bytes]
    executable: bool = False


def _checked(path):
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(p in ("", ".", "..", ".git") for p in parts):
        raise GateError(f"{path!r} is not a plain repository-relative path")
    return parts


def _work_tree_state(root, path):
    _checked(path)
    target = os.path.join(str(root), path)
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return _State(None)
    if not stat.S_ISREG(mode):
        raise GateError(f"{path} is not a regular file in the sandbox, so its fixed state "
                        "cannot be held and restored")
    with open(target, "rb") as handle:
        return _State(handle.read(), bool(mode & stat.S_IXUSR))


def _base_state(sandbox, path):
    _checked(path)
    out = git(sandbox.path, "ls-tree", "-z", sandbox.base, "--", path)
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        if name.decode("utf-8", "surrogateescape") != path:
            continue
        mode, kind, blob = meta.decode("ascii").split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise GateError(f"{path} is a {mode} {kind} at the base, not a regular file, so "
                            "it cannot be restored here")
        return _State(git(sandbox.path, "cat-file", "blob", blob), mode == "100755")
    return _State(None)


def _write_states(root, states):
    for path, state in sorted(states.items()):
        _write_state(str(root), path, state)


def _write_state(root, path, state):
    """Make `path` under `root` hold `state`, following no symlink: each
    directory on the way must be a real one (made if missing), and the file
    is replaced, never written through."""
    parts = _checked(path)
    directory = root
    for part in parts[:-1]:
        directory = os.path.join(directory, part)
        try:
            mode = os.lstat(directory).st_mode
        except FileNotFoundError:
            if state.data is None:
                return  # absent already
            os.mkdir(directory)
            continue
        if not stat.S_ISDIR(mode):
            raise GateError(f"{directory} is not a directory, so {path} cannot be restored")
    target = os.path.join(directory, parts[-1])
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if stat.S_ISDIR(mode):
            raise GateError(f"{path} is a directory in the sandbox, so it cannot be restored")
        os.unlink(target)
    if state.data is None:
        return
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(handle, 0o755 if state.executable else 0o644)
        view = memoryview(state.data)
        while view:
            view = view[os.write(handle, view):]
    finally:
        os.close(handle)
