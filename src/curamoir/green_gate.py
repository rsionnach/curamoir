"""The E3-R4 test gate (hence the module's name: nothing goes to a PR
until the tests are green): the sweep runs the project's tests before
opening a PR, and never opens a red one (cm-fkd.5).

E3-R4: "The sweep MUST run the project's test command (from config or
detected) before opening the PR. On failure it MUST revert to the last green
state or reduce scope, and MUST NOT open a red PR."

The runner calls run_test_gate(sandbox, verdict) after fix_scope.verify_fix
has accepted the fix and before GateResult.patch (fix_scope's module
docstring, "What the runner does", step 7, gives the order).
It returns a GateResult whose `paths_for_pr` is the only thing the runner
may patch (through GateResult.patch, which builds the patch from the bytes
that were tested), and whose `strategy` the ledger records
(ledger_fields), because the acceptance criterion asks which strategy was
used.

**Outcomes.** `strategy` is `green` (every fix kept), `reduced` (some
dropped) or `abandoned` (no PR), and `cause` is set exactly when it is
abandoned, one of:

- `no_test_command`: none configured and none detected;
- `config_invalid`: the base's `.curamoir/curamoir.yaml` cannot be loaded;
- `imports_the_checkout`: the canary found the checkout ahead of the
  sandbox on sys.path, or a package of the project imported from outside
  the sandbox (below);
- `command_not_runnable`: the full and the base runs both exited 126 or 127,
  the shell's "cannot execute" and "not found", or both exited 1 with a
  missing runner's message (`No module named pytest`, `command not found`);
- `test_timeout`: the full and the base runs both timed out, so the
  timeout, not the code, decided;
- `output_cap`: the full and the base runs were both killed for writing
  more than _OUTPUT_CAP bytes of output, so the cap, not the code, decided;
- `base_red`: the base itself is red (a timeout or an output cap of one of
  the two runs counts as red);
- `no_green_subset`: every fixed file was tested on the green base and each
  one alone is red;
- `run_cap`: `sweep.max_test_runs` was reached before the fix and the base
  were both run, or before any fixed file was found green.

**Where the tests run** (user decision on cm-fkd, 2026-10-12): inside the
sandbox, so the fixed code is what is tested, never in the user's checkout.
The sandbox is a fresh clone with no venv or node_modules, so the SOURCE
checkout's are reused read-only: its `.venv` (or `venv`) by VIRTUAL_ENV and
PATH, its `node_modules/.bin` on PATH and its `node_modules` on NODE_PATH.
Nothing is provisioned, and the OS sandbox (below) denies the network.

A reused venv can hold an editable install of the checkout itself (`pip
install -e`, `uv sync`; this repository's own is hatch's
`_editable_impl_curamoir.pth`), which points `import pkg` at the UNFIXED
`<checkout>/src`: a false green, then a red PR. Two defences:

- PYTHONPATH is the sandbox's `src` (when the base tracks a top-level
  `src/`) then the sandbox root. PYTHONPATH precedes site-packages on
  sys.path, and an ordinary `.pth` line (pip's and hatch's editable path
  files) is appended after it; setuptools' `__editable___*_finder` is
  appended to sys.meta_path, after the path finder that reads sys.path.
  Either way the sandbox's copy is found first.
- The canary (_canary): before any test run, `python3` (the tests'
  environment, cwd the sandbox) prints its sys.path and imports each
  top-level package of the base (import_names). An entry inside the
  checkout before the sandbox's first PYTHONPATH entry (a `.pth` that
  inserts itself FIRST, as old setuptools' easy-install.pth does), which
  shadows every layout, single modules and namespace packages included,
  or a package whose `__file__` is outside the sandbox (a meta-path
  finder ahead of the path finder), abandons the sweep,
  `imports_the_checkout`: never green. An ordinary appended `.pth` passes:
  PYTHONPATH is ahead of it. A package that does not import is not
  evidence; no `python3` (a Node project) checks nothing, and
  GateResult.canary says which.

**The environment** is an ALLOWLIST (sandbox_environment), not the user's
minus a denylist, so a credential whose name announces nothing (a token in
`MY_SERVICE`) stays out as surely as `GH_TOKEN`, `AWS_*`, `ANTHROPIC_*`,
`SSH_AUTH_SOCK` or `KUBECONFIG`. Kept: USER, LOGNAME, LANG, TZ and LC_*,
and the version managers' roots (PYENV_ROOT, ASDF_DATA_DIR, ASDF_DIR,
VOLTA_HOME, NVM_DIR; directories, not secrets), defaulted to their place
under the real HOME when unset, so a pyenv, asdf or volta shim on PATH
still finds its installs. PYTHONUSERBASE is the user base that PATH's
`python3` reports in the sweep's own environment, so a pytest installed
with `pip install --user` (macOS's /usr/bin/python3 has no other) is still
found under the fresh HOME.
Built: PATH (the reused venv's bin and `node_modules/.bin` first, then the
user's PATH whole: PATH names directories, not secrets, and pruning those
under $HOME would break a pyenv, nvm or ~/.local python3 while protecting
nothing, since the test code can name any binary by its full path),
VIRTUAL_ENV, NODE_PATH, PYTHONPATH (above), PYTHONDONTWRITEBYTECODE=1 (so
importing from the source's venv writes no `.pyc` into the checkout),
CI=true (jest and npm then run non-interactively and jest neither writes
nor deletes snapshots, so a run cannot pass by rewriting its own
expectations), and HOME and TMPDIR, a fresh directory each per run inside
the run's scratch directory (outside the clone; the OS sandbox lets the
run write there) and removed after it (no `.npmrc`, `.netrc`, git
credential helper or cloud config of the user's is read). TERM and SHELL are dropped:
the output is captured, not shown, and the command is run by `/bin/sh`.

**The OS sandbox** (cm-p2f; os_sandbox): every run, the canary's
included, is wrapped by os_sandbox.wrap, so the kernel confines it: writes
only inside the clone and the run's own scratch directory (which holds its
HOME and TMPDIR); no reads under the user's home but the paths the run
needs (_read_only); no network (on macOS not even loopback unless the
base's `sweep.test_loopback` is true); no connection to a Unix socket
outside those two; on macOS no Mach service that acts outside the sandbox
(LaunchServices, launchd, AppleEvents, mDNSResponder). macOS uses `sandbox-exec` with the shipped profile
(os_sandbox.sb), Linux bubblewrap (`bwrap`, which needs unprivileged user
namespaces); any other platform, or the tool missing, refuses. Before the
first run of each gate, os_sandbox.self_test proves it through the same
argv path (os_sandbox._CHECKS: writes, network, DNS, Unix sockets, reads
under the home, and on macOS launchd and LaunchServices). On Linux the run
sees only the system directories and what _read_only names (PATH, the venv
and its interpreter, the version managers), read-only. Failing either is a GateError, so the sweep refuses
(TEST_GATE, "the test command was not run: no OS sandbox ..."): there is
no unsandboxed fallback. OS_SANDBOX is the seam tests replace.

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
  green: E3-R4 says the tests MUST run before a PR, so the sweep opens none.

It is run by `/bin/sh -c`, as the owner would type it: it is the project
owner's committed text (PLANTED.json's is `PYTHONPATH=src python3 -m pytest
-q`, which needs a shell), and running it through a shell is what "the
project's test command" means. Hardened like git in git_guard: cwd = the
sandbox, its own process group, killed whole at `sweep.test_timeout_seconds`
(1 to 86400; a timeout is red). Its output goes to temporary files, not
pipes, and only the last 4000 characters of stdout and of stderr are kept
(git_guard.run_in_group's `tail_bytes`), so memory is bounded however much
it prints; disk is too: past 256 MiB of output (_OUTPUT_CAP) the group is
killed and the run is red, saying why.

**On red, reduce scope** before abandoning. First the base is run (every
changed path back at its base content): a red base means the tests cannot
judge any fix, so nothing is opened. Otherwise reduce_scope keeps the
largest green set it can find, deterministically, a path at a time (per
path because verify_fix accepts and sandbox.patch carries paths, not
hunks): starting from the green base, it tries adding the candidates as a
group, keeps a group whose addition is green, and splits a red group in
halves until a single red path is dropped. A set already run is not run
again, so the full set (known red) is never re-run. One bad fix among n
costs about 2*log2(n) runs; an interaction (two fixes green alone, red
together) keeps the first and drops the second. The set handed to the PR
is always one that a run found green.

`sweep.max_test_runs` (default 12, the full and base runs included) bounds
it: 12 always isolates one bad fix among `sweep.max_files_per_pr` (20)
(tests/test_green_gate.py checks every position); in general it needs about
2 + 2*ceil(log2(max_files_per_pr)). What the cap leaves untested is
dropped, never kept: an untested fix waits for the next sweep, which finds
its violation again.

**The revert is not git's.** E0-R5 denies `git checkout -- <file>`, `git
reset --hard`, `git stash` and `git clean`. Every verdict path's base state
(mode and blob, `git ls-tree`/`cat-file` through git_guard's hardened git)
and fixed state (read from the sandbox) are taken into memory before the
first run, and each run's tree is written from them by this module, with no
git command between runs. Before each run the whole candidate state is
rewritten, so a test that edits a fixed file cannot carry that edit into
the next run or into the PR; reverting an added file also removes the
directories it leaves empty.

Everything else in the tree is held to a manifest (a digest per entry)
taken before the first run. After each run, an entry the run added (a
`.pytest_cache`, a new jest snapshot, a leftover file) is removed, never
following a symlink, and an entry the run changed or removed (a test that
deletes `src/` or edits an unchanged tracked file) makes the gate raise
GateError: restoring it would need every tracked file's content read up
front (memory without bound on a large repository) or git between runs.

**Limits, stated rather than hidden.**

- The tests run the fixed code, which the fix agent wrote, as the user,
  confined by the OS sandbox (above). On macOS it can still READ outside
  the user's home (system files, /opt, other volumes) and what it was
  granted inside it (the reused venv), but cannot write outside the clone
  and the run's directory nor send what it read anywhere. The allowlist
  keeps the user's credentials out of its environment; the sandbox keeps
  the home's files out of its reach.
- The OS sandbox exists on macOS and Linux only; elsewhere, or with the
  tool missing, or with Linux's unprivileged user namespaces off, the gate
  refuses (no test command runs, no PR).
- The environment is the user's checkout, not necessarily the base's
  lockfile: a venv that is ahead of or behind the base can turn a green
  base red (then nothing is opened) or the reverse. The canary checks the
  project's own packages, not its dependencies.
- NODE_PATH is read by CommonJS `require` only; ES modules ignore it, so a
  project whose tests import packages as ESM may be red in the sandbox.
- A fresh HOME means no user git identity or tool config: a test suite
  that needs either must set it up itself.
- The sandbox's git files are held to a snapshot taken before the first
  run (git_guard.verify_git_state); a test that writes `.git` makes the
  gate raise GateError (a refusal), since the patch is built by git in that
  sandbox next.
- Each run walks the whole work tree (the manifest) and re-hashes only the
  files whose stat (size, mtime, ctime, inode, mode) changed, which costs
  time in proportion to the repository, not memory.
- The canary asks `python3` only. A command that runs another interpreter
  (`tox`, `poetry run`, `.venv/bin/python`) is protected by PYTHONPATH
  from an appended `.pth`, but a front-inserting one there is not checked.
- PYTHONUSERBASE is that of the `python3` on PATH: a command that runs
  another interpreter whose user base differs (a macOS framework
  python3.12 beside a 3.9) does not see its `--user` installs. asdf's
  global versions live in ~/.tool-versions, under the HOME that is not
  the user's: set them in the repository's own `.tool-versions`.
- GateResult.patch checks the kept files before and after git builds the
  patch; a writer that changes a file and changes it back between those
  checks, while git reads it, is not seen.
- A process that leaves the group with setsid() escapes the kill at the
  timeout and lives on. It cannot hold a run open: the run ends when the
  command's own process does, because the output goes to files, not pipes
  a daemon could keep open.
- Flaky tests: a green run is trusted once. A flake can drop a good fix
  (safe; the ledger records it as red, and cannot tell a flake from a real
  failure) or make the full run red and the base green by chance.
- Dropping is per path, so a file with one good and one bad fix loses both.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Optional

from curamoir import os_sandbox
from curamoir.config import DEFAULTS, load_config_text
from curamoir.git_guard import (
    GitGuardError,
    OutputCapExceeded,
    git,
    remove_tree,
    require_sandbox,
    run_in_group,
    snapshot_git_state,
    verify_git_state,
)
from curamoir.scaffold import PYTHON_MARKERS

#: Strategies: strings, because the ledger line records them.
GREEN = "green"
REDUCED = "reduced"
ABANDONED = "abandoned"

#: Why a sweep was abandoned (GateResult.cause; the module docstring says
#: when each is given). A closed vocabulary: the ledger records it.
NO_TEST_COMMAND = "no_test_command"
CONFIG_INVALID = "config_invalid"
IMPORTS_THE_CHECKOUT = "imports_the_checkout"
COMMAND_NOT_RUNNABLE = "command_not_runnable"
TEST_TIMEOUT = "test_timeout"
OUTPUT_CAP = "output_cap"
BASE_RED = "base_red"
NO_GREEN_SUBSET = "no_green_subset"
RUN_CAP = "run_cap"
CAUSES = (NO_TEST_COMMAND, CONFIG_INVALID, IMPORTS_THE_CHECKOUT, COMMAND_NOT_RUNNABLE,
          TEST_TIMEOUT, OUTPUT_CAP, BASE_RED, NO_GREEN_SUBSET, RUN_CAP)

PASSED = "passed"
FAILED = "failed"
TIMED_OUT = "timed_out"

PYTHON_COMMAND = "python3 -m pytest -q"
NODE_COMMAND = "npm test"

_CONFIG = ".curamoir/curamoir.yaml"
_PYTHON_TEST_FILE = re.compile(r"(^test_.*|.*_test)\.py$")
#: Characters of a run's output kept for the PR body and diagnosis.
_TAIL = 4000
#: Bytes a run may write to its two output files together before its group
#: is killed and the run counted red: disk, like memory, is bounded. 256 MiB
#: is far past any test suite's honest output and well under a laptop's
#: free space; a constant, not a setting, because no project should need it.
_OUTPUT_CAP = 256 << 20
#: The shell's exits for "no such command" (127) and "cannot execute" (126).
_NOT_RUNNABLE = frozenset({126, 127})
#: What a missing runner prints when it exits 1 rather than 127: `python3
#: -m pytest` without pytest ("No module named pytest"), and an npm script
#: or a wrapper whose tool is not installed ("command not found").
_MISSING_RUNNER = re.compile(r"No module named '?pytest'?|command not found")

#: The OS sandbox every run is wrapped in (os_sandbox: its wrap() and
#: self_test()). A module attribute so tests can put a stand-in in its place
#: (tests/os_sandbox_stand_in.py); nothing in the package does.
OS_SANDBOX = os_sandbox

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
    `cause` and `why`. `loopback` is the base's sweep.test_loopback: whether
    the OS sandbox lets the run use loopback (os_sandbox.wrap)."""

    command: Optional[str]
    source: Optional[str]
    timeout: int
    max_runs: int
    cause: Optional[str] = None
    why: str = ""
    loopback: bool = False


@dataclass(frozen=True)
class GateRun:
    """One run of the test command: the fixed `paths` applied (every other
    changed path at its base), its `outcome` (passed, failed, timed_out),
    exit `code` (None on timeout or cap), `seconds`, the last _TAIL
    characters of its stdout and of its stderr, kept apart, and `capped`:
    killed for passing _OUTPUT_CAP (outcome failed)."""

    paths: tuple
    outcome: str
    code: Optional[int]
    seconds: float
    stdout_tail: str
    stderr_tail: str
    capped: bool = False

    @property
    def passed(self):
        return self.outcome == PASSED

    @property
    def tail(self):
        """Both tails, stdout first: what a reader of the PR or ledger wants."""
        return self.stdout_tail + self.stderr_tail


@dataclass(frozen=True)
class DroppedFix:
    """A fixed path left out of the PR, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class GateResult:
    """What the gate decided. `strategy` is one of GREEN, REDUCED, ABANDONED,
    and `cause` (one of CAUSES) is set if and only if it is ABANDONED, with
    `why` saying it in a sentence. `kept` is the green set (sorted paths);
    `paths_for_pr` is it when the strategy is green or reduced and () when
    abandoned: the runner patches them with `patch(sandbox)`, never
    `sandbox.patch` directly, and nothing else.

    `canary` says what the canary (module docstring) checked, in one of
    three forms: "" (not reached: no test command), `checked: sys.path`
    followed by `; imported <names>` and/or `; could not import <names>`
    for the base's packages, or `skipped: <why>` (python3 did not run).

    `tested` is, per kept path, (path, executable, sha256 hex) of the
    content the final green run tested, (path, None, None) for a path that
    run had absent: what verify_kept holds the sandbox to. `held` is, per
    kept path, (path, executable, bytes or None): that content itself, what
    patch() is built from."""

    strategy: str
    cause: Optional[str]
    command: Optional[str]
    command_source: Optional[str]
    kept: tuple
    dropped: tuple
    runs: tuple
    why: str = ""
    canary: str = ""
    tested: tuple = ()
    held: tuple = field(default=(), repr=False)

    @property
    def paths_for_pr(self):
        return self.kept if self.strategy in (GREEN, REDUCED) else ()

    def patch(self, sandbox):
        """The PR's patch of paths_for_pr, `git apply`-able on the base and
        byte-identical to sandbox.patch(paths_for_pr) when the work tree
        holds what was tested, but built from the HELD bytes (`held`), never
        from the work tree: each is written into git's object store and
        staged by mode and id in a throwaway index, an absent path removed
        from it, and the index diffed against the base. WHY: a process a test
        started with setsid() outlives the run and can rewrite a kept file
        after the gate returned, even only while git reads it; built from
        the work tree, the patch could carry code no run tested.

        Refused (GateError) unless the work tree still matches what was
        tested before and after: the patch would be right, but a mismatch
        means something is still writing the sandbox the runner is about to
        push from. Blobs are hashed with the path's attributes (`--path`), so
        a base's text/eol conversion is applied exactly as `git add` would:
        the patch is git's own, not the work tree's converted line endings."""
        self.verify_kept(sandbox)
        built = _patch_of(sandbox, self.held)
        self.verify_kept(sandbox)
        return built

    def verify_kept(self, sandbox):
        """GateError naming each kept path whose state is not the tested one."""
        changed = []
        for path, executable, digest in self.tested:
            try:
                state = _work_tree_state(sandbox.path, path)
            except (OSError, GateError):
                changed.append(path)
                continue
            if _fingerprint(state) != (executable, digest):
                changed.append(path)
        if changed:
            raise GateError(", ".join(changed) + " changed after the green run (a process the "
                            "tests left running may still be writing the sandbox), so the "
                            "patch would carry content no run tested")

    def render(self):
        """One line for the PR body (green, reduced) or for the ledger
        line's `message` (abandoned: `no PR: ` and the why)."""
        if self.strategy == GREEN:
            return f"tests green with all {len(self.kept)} fixed file(s) ({len(self.runs)} run)"
        if self.strategy == REDUCED:
            return (f"tests red with every fix; reduced to {len(self.kept)} fixed file(s) "
                    f"that are green, dropping {', '.join(d.path for d in self.dropped)} "
                    f"({len(self.runs)} runs)")
        return f"no PR: {self.why or self.cause}"

    def ledger_fields(self):
        """Keyword fields for the sweep's one ledger line (job_run.JobRun's
        succeeded and failed take them; a refusal has no GateResult):
        `test_gate` and nothing else, so cm-fkd.6's fix records, summary
        and found delta (fix_counts.ledger_fields) sit beside it, not
        inside it. Its keys: strategy (str), cause (str or None), why
        (str, "" unless abandoned; render() carries it into the message
        too), command and command_source (str or None), kept (list of
        paths), dropped (list of {path, reason}), runs (list of
        {fixed_count: int, the fixed paths applied; outcome: str; code: int
        or None; seconds: float}) and canary (str)."""
        return {"test_gate": {
            "strategy": self.strategy,
            "cause": self.cause,
            "why": self.why,
            "command": self.command,
            "command_source": self.command_source,
            "kept": list(self.kept),
            "dropped": [{"path": d.path, "reason": d.reason} for d in self.dropped],
            "runs": [{"fixed_count": len(run.paths), "outcome": run.outcome,
                      "code": run.code, "seconds": round(run.seconds, 3)} for run in self.runs],
            "canary": self.canary,
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
    loopback = sweep.get("test_loopback", False) is True
    configured = sweep["test_command"].strip()
    if configured:
        return GateCommand(configured, "config", timeout, max_runs, loopback=loopback)
    detected, why = _detect(sandbox)
    if detected is None:
        return GateCommand(None, None, timeout, max_runs, NO_TEST_COMMAND, why)
    return GateCommand(detected, "detected", timeout, max_runs, loopback=loopback)


def _base_names(sandbox):
    """Every path the base commit tracks."""
    return [name.decode("utf-8", "surrogateescape") for name in
            git(sandbox.path, "ls-tree", "-r", "-z", "--name-only", sandbox.base).split(b"\0")
            if name]


def _detect(sandbox):
    names = _base_names(sandbox)
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


#: Inherited as they are: who the user is and how text and time are read.
_INHERITED = ("USER", "LOGNAME", "LANG", "TZ")
#: Version managers' roots: a shim on PATH (pyenv's `python3`, asdf's,
#: volta's `node`) finds its installs through it, and defaults it to a path
#: under HOME, which the fresh HOME would break. Maps each name to its
#: default directory under the real HOME, used when the variable is unset
#: and that directory exists; None means inherit only (nvm puts the node bin
#: on PATH itself, and ASDF_DIR is where asdf's own code is, if moved). An
#: inherited value is kept only if it is an absolute directory (_directory).
_VERSION_MANAGER_ROOTS = {"PYENV_ROOT": ".pyenv", "ASDF_DATA_DIR": ".asdf", "ASDF_DIR": None,
                          "VOLTA_HOME": ".volta", "NVM_DIR": None}
_USER_BASE = "import site, sys; sys.stdout.write(site.getuserbase())"


def _directory(value):
    """`value` if it is an absolute path to a directory, else None. WHY: the
    variables that name directories (VIRTUAL_ENV, the version managers'
    roots, PYTHONUSERBASE) are allowlisted for being directories, so a value
    that is not one (a token under a misleading name, a relative path the
    sandbox's cwd would reinterpret) is not passed on. The limit: a secret
    that happens to be an existing absolute directory's path would pass,
    and a directory's name says nothing a listing of the disk does not."""
    if value and os.path.isabs(value) and os.path.isdir(value):
        return value
    return None


def _user_base(env):
    """The user base (`pip install --user`'s prefix) of the `python3` the
    tests will find on env's PATH, or None. Asked in `env` (the allowlist
    built so far) plus the real HOME and an inherited PYTHONUSERBASE that is
    a directory, which are all it depends on: the probe is a program off
    PATH run before any run's sandboxing, so it gets no inherited secret
    either. WHY asked, not derived: it depends on the interpreter (a macOS
    framework build's is ~/Library/Python/X.Y, others' ~/.local) and on
    PYTHONUSERBASE, and the fresh HOME would move it. Kept only if it is an
    absolute directory: one that does not exist holds no install."""
    probe = dict(env, HOME=os.path.expanduser("~"))
    inherited = _directory(os.environ.get("PYTHONUSERBASE"))
    if inherited:
        probe["PYTHONUSERBASE"] = inherited
    try:
        done = subprocess.run(["python3", "-c", _USER_BASE], env=probe,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return _directory(done.stdout.decode("utf-8", "replace")) if done.returncode == 0 else None


def sandbox_environment(source, root=None, src=False):
    """The environment the test command runs with, built from an ALLOWLIST
    (module docstring, "The environment"), never the inherited one minus a
    denylist: whatever secret a variable name does not announce stays out.

    PATH is the inherited PATH with the SOURCE checkout's `.venv` (or
    `venv`: a directory holding `pyvenv.cfg` and `bin/`) bin and its
    `node_modules/.bin` first; VIRTUAL_ENV names that venv (the inherited
    one when the source has none: the environment the sweep was started
    in, if it is an absolute directory); NODE_PATH is the source's
    `node_modules`. With `root` (the
    sandbox), PYTHONPATH is the sandbox's `src` (when `src`: the base tracks
    a top-level `src/`) then `root`, so an editable install of the
    checkout in the reused venv cannot shadow the fixed code. Plus
    PYTHONDONTWRITEBYTECODE=1 and CI=true; PYTHONUSERBASE is the user
    base of that PATH's `python3` (_user_base) and the version managers'
    roots are kept (_VERSION_MANAGER_ROOTS), so a `pip install --user`
    pytest and a pyenv or asdf shim still resolve under the fresh HOME and
    TMPDIR that are added per run (_run_command), each removed after it."""
    env = {key: value for key, value in os.environ.items()
           if key in _INHERITED or key.startswith("LC_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CI"] = "true"
    prefix = []
    for name in (".venv", "venv"):
        venv = os.path.join(str(source), name)
        if os.path.isfile(os.path.join(venv, "pyvenv.cfg")) and \
                os.path.isdir(os.path.join(venv, "bin")):
            env["VIRTUAL_ENV"] = venv
            prefix.append(os.path.join(venv, "bin"))
            break
    else:
        if _directory(os.environ.get("VIRTUAL_ENV")):
            env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    modules = os.path.join(str(source), "node_modules")
    if os.path.isdir(modules):
        if os.path.isdir(os.path.join(modules, ".bin")):
            prefix.append(os.path.join(modules, ".bin"))
        env["NODE_PATH"] = modules
    env["PATH"] = os.pathsep.join([*prefix, os.environ.get("PATH", os.defpath)])
    home = os.path.expanduser("~")
    for name, default in _VERSION_MANAGER_ROOTS.items():
        if os.environ.get(name):
            if _directory(os.environ[name]):
                env[name] = os.environ[name]
        elif default and os.path.isdir(os.path.join(home, default)):
            env[name] = os.path.join(home, default)
    user_base = _user_base(env)
    if user_base:
        env["PYTHONUSERBASE"] = user_base
    if root is not None:
        env["PYTHONPATH"] = os.pathsep.join(
            ([os.path.join(str(root), "src")] if src else []) + [str(root)])
    return env


def _run_command(sandbox, argv, env, timeout, paths=(), loopback=False):
    """One run of `argv` in the sandbox, confined by the OS sandbox
    (OS_SANDBOX.wrap: writes only in the clone and this run's scratch
    directory, no network): a GateRun. HOME and TMPDIR are a fresh
    directory each inside that scratch directory, outside the clone and
    removed afterwards, so a run neither reads the user's dotfiles (an
    `.npmrc` token, a git credential helper) nor hands state to the next
    run. GateError, and nothing run, if no OS sandbox is available."""
    scratch = tempfile.mkdtemp(prefix="curamoir-test-run-")
    try:
        env = _scratch_env(sandbox, env, scratch)
        wrapped = _wrapped(argv, sandbox, scratch, env, loopback)
        started = time.monotonic()
        try:
            # 4 bytes per character: UTF-8's widest, so the last _TAIL
            # characters are inside the bytes kept whatever the text.
            done = run_in_group(wrapped, cwd=str(sandbox.path), env=env, timeout=timeout,
                                tail_bytes=4 * _TAIL, max_output_bytes=_OUTPUT_CAP)
        except OutputCapExceeded as capped:
            return GateRun(paths, FAILED, None, time.monotonic() - started,
                           _tail(capped.output),
                           _tail(capped.stderr) + f"\n[killed with its process group: its "
                           f"output passed {_OUTPUT_CAP >> 20} MiB, so it counts as red]", capped=True)
        except subprocess.TimeoutExpired as expired:
            return GateRun(paths, TIMED_OUT, None, time.monotonic() - started,
                           _tail(expired.output),
                           _tail(expired.stderr) + f"\n[killed after {timeout}s with its "
                           "process group]")
        except OSError as error:
            raise GateError(f"the test command could not be started: {error}") from error
        return GateRun(paths, PASSED if done.returncode == 0 else FAILED, done.returncode,
                       time.monotonic() - started, _tail(done.stdout), _tail(done.stderr))
    finally:
        remove_tree(scratch, what="the test run's scratch directory (its HOME and TMPDIR)")


def _tail(data):
    return (data or b"").decode("utf-8", "replace")[-_TAIL:]


def _scratch_env(sandbox, env, scratch):
    """`env` with HOME and TMPDIR made fresh inside `scratch` (the run's
    directory, which the OS sandbox lets it write) and PWD the sandbox."""
    home, tmp = os.path.join(scratch, "home"), os.path.join(scratch, "tmp")
    os.mkdir(home)
    os.mkdir(tmp)
    return dict(env, PWD=str(sandbox.path), HOME=home, TMPDIR=tmp)


_NO_SANDBOX = "the test command was not run: no OS sandbox confines it here"


def _wrapped(argv, sandbox, scratch, env, loopback):
    """`argv` wrapped in the OS sandbox for the clone and `scratch`;
    GateError (the sweep refuses, TEST_GATE) where there is none. Never
    the bare argv: there is no unsandboxed fallback."""
    try:
        return OS_SANDBOX.wrap(argv, clone=str(sandbox.path), run_dir=scratch,
                               read_only=_read_only(env), loopback=loopback)
    except os_sandbox.OsSandboxUnavailable as error:
        raise GateError(f"{_NO_SANDBOX}: {error}") from error


def _read_only(env):
    """What a run may read that the OS sandbox otherwise hides
    (os_sandbox.wrap's `read_only`; on macOS, anything under the user's
    home; on Linux, anything but the system directories): PATH's
    directories, the reused venv and the installation its interpreter links
    to (_installation), NODE_PATH, the user base and the version managers'
    roots, as os_sandbox._granted keeps them (real, existing, never a home
    nor in or holding a credential tree).
    Read-only, and only the paths named, so a tool on PATH whose link points
    elsewhere under the home (outside these) cannot be read and fails:
    then the base is red or the command not runnable, and no PR opens."""
    paths = [entry for name in ("PATH", "NODE_PATH")
             for entry in env.get(name, "").split(os.pathsep)]
    paths += [env.get(name, "") for name in ("VIRTUAL_ENV", "PYTHONUSERBASE",
                                             *_VERSION_MANAGER_ROOTS)]
    venv = env.get("VIRTUAL_ENV")
    interpreters = [os.path.join(venv, "bin", "python3")] if venv else []
    # An interpreter on PATH is often a link into an installation elsewhere
    # (a venv's python3 into uv's or pyenv's, a node into nvm's): the run
    # must read that installation, or exec fails and the shell silently
    # moves on to the next python3 on PATH.
    for directory in env.get("PATH", "").split(os.pathsep):
        if os.path.isabs(directory):
            interpreters += [os.path.join(directory, name) for name in _INTERPRETERS]
            # A venv's bin on PATH: its site-packages are next to it.
            if os.path.isfile(os.path.join(os.path.dirname(directory), "pyvenv.cfg")):
                paths.append(os.path.dirname(directory))
    for interpreter in interpreters:
        if os.path.islink(interpreter):
            paths.append(_installation(interpreter))
    return os_sandbox._granted(path for path in paths if path and os.path.isabs(path))


#: The interpreters on PATH whose installation _read_only grants.
_INTERPRETERS = ("python3", "python", "node")
#: What marks an installation prefix, per interpreter: a CPython's
#: lib/pythonX.Y (X.Yt for a free-threaded build), node's lib/node_modules.
_PYTHON_LIB = re.compile(r"python\d+\.\d+t?")
#: The only direct children of a home granted as an installation prefix:
#: version managers' roots (pyenv, nvm, asdf, volta, rustup, cargo, rbenv,
#: sdkman). uv's and mise's live deeper (~/.local/share/uv, .../mise), so
#: this rule does not reach them; ~/.local itself holds keyrings and is
#: refused by os_sandbox._granted.
_VERSION_MANAGER_DIRS = (".pyenv", ".nvm", ".asdf", ".volta", ".rustup", ".cargo", ".rbenv",
                         ".sdkman")


def _installation(interpreter):
    """The installation prefix `interpreter` (a link) resolves into, to be
    granted, or None. WHY checked: the prefix is two levels above the
    resolved binary, which for a binary in ~/bin is the home itself and for
    one planted in a credential tree is that tree. So only a real
    installation (lib/pythonX.Y beside a python, lib/node_modules beside
    node) is granted, never a home, and a direct child of a home only if it
    is a version manager's root (_VERSION_MANAGER_DIRS). A refused prefix
    is left out silently: a run that needed it fails, so no PR opens."""
    prefix = os.path.dirname(os.path.dirname(os.path.realpath(interpreter)))
    lib = os.path.join(prefix, "lib")
    if os.path.basename(interpreter) == "node":
        if not os.path.isdir(os.path.join(lib, "node_modules")):
            return None
    else:
        try:
            names = os.listdir(lib)
        except OSError:
            return None
        if not any(_PYTHON_LIB.fullmatch(name) and os.path.isdir(os.path.join(lib, name))
                   for name in names):
            return None
    for home in os_sandbox.homes():
        if prefix == home or (os.path.dirname(prefix) == home
                              and os.path.basename(prefix) not in _VERSION_MANAGER_DIRS):
            return None
    return prefix


def _sandbox_self_test(sandbox, env, loopback):
    """OS_SANDBOX.self_test for this gate's clone, with a scratch directory
    and environment shaped like a run's; GateError if it fails. Run once per
    gate, before the manifest is taken (its probe file in the clone is
    removed again) and before any command of the project's."""
    scratch = tempfile.mkdtemp(prefix="curamoir-test-run-")
    try:
        try:
            OS_SANDBOX.self_test(str(sandbox.path), scratch,
                                 env=_scratch_env(sandbox, env, scratch),
                                 read_only=_read_only(env), loopback=loopback)
        except os_sandbox.OsSandboxUnavailable as error:
            raise GateError(f"{_NO_SANDBOX}: {error}") from error
    finally:
        remove_tree(scratch, what="the sandbox self-test's scratch directory")


# --- the canary -----------------------------------------------------------------------

_CANARY = r"""
import importlib, json, sys
path = list(sys.path)
found = {}
for name in sys.argv[1:]:
    try:
        found[name] = getattr(importlib.import_module(name), "__file__", None)
    except BaseException:
        found[name] = None
sys.stdout.write("\n" + json.dumps({"path": path, "found": found}) + "\n")
"""


def import_names(names):
    """The top-level packages the base tracks, which a Python test imports:
    the directories directly under `src/` holding an `__init__.py`, or,
    when there is none, those directly under the root. Packages only, not
    modules: a root `setup.py` or `conftest.py` is not safe to import."""
    for prefix in ("src/", ""):
        found = sorted({PurePosixPath(name).parts[-2] for name in names
                        if name.startswith(prefix) and name.endswith("/__init__.py")
                        and len(PurePosixPath(name[len(prefix):]).parts) == 2
                        and PurePosixPath(name).parts[-2].isidentifier()})
        if found:
            return tuple(found)
    return ()


def _inside(path, root):
    return os.path.commonpath([root, path]) == root


def _canary(sandbox, env, timeout, names, loopback=False):
    """(outside, said): what `python3`, in the tests' environment with cwd
    the sandbox, resolves from the source checkout ahead of the sandbox, as
    (what, where) pairs, and the GateResult.canary text.

    Two layers. sys.path: an entry inside the checkout (and not inside the
    sandbox) before the first PYTHONPATH entry shadows the sandbox for every
    layout, single modules and namespace packages included. '' is skipped:
    `python3 -c` puts the cwd first itself, and a test runner's sys.path[0]
    is its own. Then the import: each of `names` whose `__file__` is
    outside the sandbox (a meta-path finder, which sys.path does not show).
    No python3, or output that is not the canary's, checks nothing."""
    run = _run_command(sandbox, ["/bin/sh", "-c", 'exec python3 -c "$0" "$@"', _CANARY, *names],
                       env, timeout, loopback=loopback)
    try:
        report = json.loads(run.stdout_tail.strip().splitlines()[-1])
        path, found = report["path"], report["found"]
        if not (isinstance(path, list) and isinstance(found, dict)):
            raise ValueError(report)
    except (IndexError, KeyError, TypeError, ValueError):
        return (), "skipped: python3 did not run the canary in the test environment, so " \
                   "neither sys.path nor the imports were checked"
    root = os.path.realpath(str(sandbox.path))
    source = os.path.realpath(str(sandbox.source))
    first = os.path.realpath(env["PYTHONPATH"].split(os.pathsep)[0])
    outside = []
    for entry in (e for e in path if isinstance(e, str) and e):
        resolved = os.path.realpath(os.path.join(root, entry))
        if resolved == first:
            break
        if _inside(resolved, source) and not _inside(resolved, root):
            outside.append(("sys.path", entry))
    imported = sorted(name for name, file in found.items() if isinstance(file, str))
    missing = sorted(set(names) - set(imported))
    outside.extend((name, found[name]) for name in imported
                   if not _inside(os.path.realpath(found[name]), root))
    said = "checked: sys.path" + (f"; imported {', '.join(imported)}" if imported else "") \
        + (f"; could not import {', '.join(missing)}" if missing else "")
    return tuple(outside), said


# --- the reduction --------------------------------------------------------------------

class Trials:
    """`trials(subset) -> bool` (green?), memoised by set and capped at
    `max_runs` real runs. Calling it with a set already run returns that
    answer without running; past the cap it returns None and sets
    `capped`. `used` counts the real runs."""

    def __init__(self, run, max_runs):
        self._run = run
        self.max_runs = max_runs
        self.used = 0
        self.capped = False
        self._known = {}

    def __call__(self, subset):
        key = frozenset(subset)
        if key in self._known:
            return self._known[key]
        if self.used >= self.max_runs:
            self.capped = True
            return None
        self.used += 1
        self._known[key] = bool(self._run(tuple(sorted(key))))
        return self._known[key]


def reduce_scope(candidates, trials):
    """(kept, dropped): the green subset of `candidates` (sorted paths)
    found by adding groups to the green base and halving red ones (module
    docstring), and a DroppedFix for every other candidate. `trials` is a
    Trials whose base (the empty set) is known green and whose full set is
    known red.

    Invariant: `kept` only ever grows by a group whose run, on top of the
    kept set, was green, so the final `kept` is exactly a set a run found
    green."""
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
    sandbox is not one, a path's state cannot be held or restored, a run
    changed a file outside the verdict's paths, or the runs changed the
    sandbox's git files."""
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
    try:
        fixed = {path: _work_tree_state(sandbox.path, path) for path in every}
    except OSError as error:
        raise GateError(f"a fixed file cannot be read, so it cannot be held: {error}") from error
    base = {path: _base_state(sandbox, path) for path in every}
    names = _base_names(sandbox)
    env = sandbox_environment(sandbox.source, sandbox.path,
                              src=any(name.startswith("src/") for name in names))
    # Before anything of the project's runs, and before the manifest: no
    # proven OS sandbox, no run (GateError, the sweep refuses).
    _sandbox_self_test(sandbox, env, chosen.loopback)
    git_state = snapshot_git_state(sandbox)
    held = _held(every)
    manifest = _tree(str(sandbox.path), held)
    runs = []

    canary = ""

    def settle():
        _settle(str(sandbox.path), manifest, held)

    def abandoned(cause, why, dropped=()):
        return GateResult(ABANDONED, cause, chosen.command, chosen.source, (),
                          dropped or tuple(DroppedFix(path, why) for path in candidates),
                          tuple(runs), why, canary)

    outside, canary = _canary(sandbox, env, chosen.timeout, import_names(names),
                              chosen.loopback)
    settle()
    if outside:
        result = abandoned(IMPORTS_THE_CHECKOUT, "the test environment resolves " + "; ".join(
            f"{what} to {where}" for what, where in outside) + " in your checkout ahead of "
            "the sandbox, so a green run would test unfixed code (an editable install of "
            "your checkout in its venv that puts itself first on sys.path)")
        return _finish(sandbox, git_state, every, fixed, base, (), result)

    def run(subset):
        _write_states(sandbox.path, {p: fixed[p] if p in subset else base[p] for p in every})
        runs.append(_run_command(sandbox, ["/bin/sh", "-c", chosen.command], env,
                                 chosen.timeout, subset, chosen.loopback))
        settle()
        return runs[-1].passed

    trials = Trials(run, chosen.max_runs)
    full = trials(candidates)
    if full is None:
        result = abandoned(RUN_CAP, f"sweep.max_test_runs ({chosen.max_runs}) allows no run, "
                                    "so the fix cannot be tested")
    elif full:
        result = GateResult(GREEN, None, chosen.command, chosen.source, candidates, (),
                            tuple(runs), "", canary)
    else:
        green_base = trials(())
        if green_base is None:
            result = abandoned(RUN_CAP, f"sweep.max_test_runs ({chosen.max_runs}) allows too "
                                        "few runs to test the fix and the base")
        elif not green_base and all(run.outcome == TIMED_OUT for run in runs):
            result = abandoned(TEST_TIMEOUT, (
                f"the tests timed out both with the fix and at the base, after "
                f"sweep.test_timeout_seconds ({chosen.timeout}s), so nothing was judged; "
                "raise it"))
        elif not green_base and all(run.capped for run in runs):
            result = abandoned(OUTPUT_CAP, (
                f"the tests wrote more than {_OUTPUT_CAP >> 20} MiB of output both with the "
                "fix and at the base, and were killed, so nothing was judged"))
        elif not green_base and _not_runnable(runs):
            result = abandoned(COMMAND_NOT_RUNNABLE, (
                f"the test command `{chosen.command}` could not be run (exit "
                f"{runs[-1].code}: no such command or test runner, or it could not be "
                "executed), so nothing was tested: " + runs[-1].tail.strip()[-300:]))
        elif not green_base:
            result = abandoned(BASE_RED, "the tests fail at the base commit itself, with no fix "
                                         "applied, so they cannot tell a good fix from a bad one")
        else:
            kept, dropped = reduce_scope(candidates, trials)
            if kept:
                result = GateResult(REDUCED, None, chosen.command, chosen.source, kept,
                                    dropped, tuple(runs), "", canary)
            elif trials.capped:
                result = abandoned(RUN_CAP, (
                    f"sweep.max_test_runs ({chosen.max_runs}) was reached before any fixed "
                    "file was found green; raise it with sweep.max_files_per_pr"), dropped)
            else:
                result = abandoned(NO_GREEN_SUBSET, (
                    "every fixed file was tested on top of the green base, and each one "
                    "alone turns the tests red"), dropped)
    return _finish(sandbox, git_state, every, fixed, base, result.kept, result)


def _not_runnable(runs):
    """The full and the base run both say the runner is missing: both exited
    126 or 127 (the shell's), or both exited 1 with a missing runner's
    message (_MISSING_RUNNER). A mix (one 127, the other 1 with the message)
    does not count, and neither does one run alone, so a real failure that
    merely prints "command not found" once is still red."""
    return all(run.code in _NOT_RUNNABLE for run in runs) or all(
        run.code == 1 and _MISSING_RUNNER.search(run.tail) for run in runs)


def _finish(sandbox, git_state, every, fixed, base, kept, result):
    """Refuse a run that changed git's files, then leave the sandbox with
    `kept` fixed and every other path of the verdict at its base, and record
    in the result what that is (GateResult.tested)."""
    changed = verify_git_state(git_state)
    if changed:
        raise GateError("the test command changed the sandbox's git files ("
                        + ", ".join(f"{c.label} {c.change}" for c in changed)
                        + "), and the patch would be built by git in that sandbox")
    _write_states(sandbox.path, {p: fixed[p] if p in kept else base[p] for p in every})
    return replace(result, tested=tuple((path, *_fingerprint(fixed[path])) for path in kept),
                   held=tuple((path, fixed[path].executable, fixed[path].data)
                              for path in kept))


def _patch_of(sandbox, held):
    """The binary patch, on the base, that sets each (path, executable,
    data) of `held`, built in a throwaway index (GIT_INDEX_FILE, as
    Sandbox.patch) with index-only git commands: the work tree is never
    read, and the sandbox's own index and HEAD are untouched. GateError if
    a path would be left out (no change from the base)."""
    if not held:
        return b""
    scratch = tempfile.mkdtemp(prefix="curamoir-patch-")
    try:
        index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        git(sandbox.path, "read-tree", sandbox.base, extra_env=index)
        for number, (path, executable, data) in enumerate(held):
            _checked(path)
            if data is None:
                git(sandbox.path, "update-index", "--force-remove", "--", path,
                    extra_env=index)
                continue
            blob = os.path.join(scratch, str(number))
            with open(blob, "xb") as handle:
                handle.write(data)
            # --path applies the base's attributes for `path` (text/eol), so the
            # blob is what `git add` would store and the patch is git's own.
            # Only git's built-in conversions can run here: the sandbox has no
            # filter.* config, and system/global config and attributes are off.
            digest = git(sandbox.path, "hash-object", "-w", f"--path={path}", "--", blob)
            # One "mode,id,path" argument: never read as an option, whatever the path.
            git(sandbox.path, "update-index", "--add", "--cacheinfo",
                f"{'100755' if executable else '100644'},"
                f"{digest.decode('ascii').strip()},{path}", extra_env=index)
        specs = [f":(literal){path}" for path, _, _ in held]
        staged = git(sandbox.path, "diff", "--cached", "--name-only", "-z", "--no-renames",
                     sandbox.base, "--", *specs, extra_env=index)
        left_out = sorted({path for path, _, _ in held}
                          - {name.decode("utf-8", "surrogateescape")
                             for name in staged.split(b"\0") if name})
        if left_out:
            raise GateError(f"{', '.join(left_out)} would be no change from the base, so a "
                            "patch would leave it out")
        return git(sandbox.path, "diff", "--cached", "--binary", "--full-index",
                   "--no-renames", "--no-ext-diff", "--no-textconv", sandbox.base, "--",
                   *specs, extra_env=index)
    finally:
        remove_tree(scratch, what="the patch's scratch directory")


# --- the tree outside the verdict ----------------------------------------------------

def _held(every):
    """The labels a run may change because this module rewrites them: the
    verdict's paths and every directory above one (made or emptied when an
    added path is written or reverted)."""
    held = set(every)
    for path in every:
        parts = PurePosixPath(path).parts
        held.update("/".join(parts[:i]) for i in range(1, len(parts)))
    return frozenset(held)


def _tree(root, held=frozenset(), previous=None):
    """label -> (stat key, digest) of every entry under `root` but its own
    `.git`, directories included ("dir"), symlinks recorded and never
    followed. Digests, not contents: one line per file, whatever the
    repository's size. GateError if a directory cannot be listed or a file
    read.

    A file of `held` is not hashed (the gate rewrites it; nothing compares
    it), and one whose stat key is `previous`'s takes its digest from there
    rather than being read again. The key holds st_ctime_ns, which the
    kernel sets on every write and metadata change and no process can set
    back (utime sets mtime, not ctime), so a rewrite that keeps size and
    mtime still changes it; only a clock reset by root could hide one."""
    found = {}

    def unlistable(error):
        raise GateError(f"{error.filename} cannot be listed after a test run ({error}), so "
                        "the tree the next run tests cannot be known")

    for directory, subdirs, files in os.walk(root, onerror=unlistable):
        if directory == root:
            subdirs[:] = [d for d in subdirs if d != ".git"]
        for name in subdirs + files:
            target = os.path.join(directory, name)
            label = os.path.relpath(target, root).replace(os.sep, "/")
            found[label] = _entry(target, label in held, (previous or {}).get(label))
    return found


def _entry(target, held, previous):
    try:
        info = os.lstat(target)
        mode = info.st_mode
        key = (mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino,
               info.st_dev)
        if stat.S_ISLNK(mode):
            return key, "link:" + os.readlink(target)
        if stat.S_ISDIR(mode):
            return key, "dir"
        if not stat.S_ISREG(mode):
            return key, f"special:{stat.S_IFMT(mode):o}"
        if held:
            return key, "held"
        if previous is not None and previous[0] == key:
            return previous
        digest = hashlib.sha256()
        with open(target, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return key, ("x:" if mode & stat.S_IXUSR else "f:") + digest.hexdigest()
    except OSError as error:
        raise GateError(f"{target} cannot be read after a test run ({error}), so the tree "
                        "the next run tests cannot be known") from error


def _settle(root, manifest, held):
    """After a run: remove every entry it added outside `held` (a cache, a
    new snapshot, a leftover file), then refuse if it changed or removed one
    that was there before the first run. One walk is enough: removing an
    added entry can change only a directory's stat, and directories are
    compared as "dir", not by stat. WHY refuse rather than restore: the content to
    restore would have to be read up front for every tracked file
    (unbounded memory) or asked of git between runs (which a run could have
    planted for); a test suite that rewrites its own tracked files is rare,
    and a guessed tree is never tested."""
    now = _tree(root, held, manifest)
    for label in sorted(set(now) - set(manifest) - held):
        target = os.path.join(root, label)
        try:
            if not os.path.lexists(target):
                continue  # inside a directory removed above
            if stat.S_ISDIR(os.lstat(target).st_mode):
                remove_tree(target, what=f"{label}, which a test run left behind,")
            else:
                os.unlink(target)
        except OSError as error:
            raise GateError(f"{label}, which a test run left behind, cannot be removed: "
                            f"{error}") from error
    drifted = sorted(label for label, (_, digest) in manifest.items()
                     if label not in held and (label not in now or now[label][1] != digest))
    if drifted:
        raise GateError("the test command changed " + ", ".join(drifted[:10])
                        + (f" and {len(drifted) - 10} more" if len(drifted) > 10 else "")
                        + ", outside the fixed paths; the gate holds only those, so the next "
                        "run would not test the tree it means to")


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


def _fingerprint(state):
    """(executable, sha256 hex) of a state, (None, None) for absent."""
    if state.data is None:
        return None, None
    return state.executable, hashlib.sha256(state.data).hexdigest()


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
        try:
            _write_state(str(root), path, state)
        except OSError as error:
            raise GateError(f"{path} cannot be set to the state a run must test: "
                            f"{error}") from error


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
                _prune(root, parts)
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
        _prune(root, parts)
        return
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(handle, 0o755 if state.executable else 0o644)
        view = memoryview(state.data)
        while view:
            view = view[os.write(handle, view):]
    finally:
        os.close(handle)


def _prune(root, parts):
    """Remove the now-empty directories above an absent path, deepest first.
    git tracks no empty directory, so a clone has none: one emptied here was
    made by the fix (an added file) or by this module, and the patch would
    not carry it either. Stops at the first that is not empty."""
    for depth in range(len(parts) - 1, 0, -1):
        try:
            os.rmdir(os.path.join(root, *parts[:depth]))
        except FileNotFoundError:
            continue
        except OSError:
            return
