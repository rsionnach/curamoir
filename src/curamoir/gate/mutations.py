"""R5-R30 to R5-R34: the mutation runner, the kill check run as code.

For each entry of the brief's ``r5-mutations`` block (gate/mutation_list.py)
the runner makes a worktree at HEAD (gate/worktrees.py), runs the entry's
``expect_red`` selection there unmutated, makes the mutation, runs the
selection again, and records one result:

- ``red``: the selection passed unmutated and fails with the mutation. The
  mutation was killed; the tests guard the behaviour.
- ``green``: the selection still passes with the mutation. The mutant
  survived; R5-R32 makes this CRITICAL on the behaviour the entry names,
  with the mutation's diff as evidence.
- ``stale``: the entry's locate, or its mutation, no longer fits the code
  (gate/mutation_list.Stale): the brief and the code have drifted (R5-R31).
- ``inconclusive``: the kill check did not run. The selection failed or
  errored unmutated (d-cm-8ap.19-10), errored rather than failed under the
  mutation, timed out, or ran no test at all (every test skipped or
  deselected, or none collected: R5-R32). Never a pass.

Green, stale and inconclusive all block.

**Reading a selection** (d-cm-8ap.19-11). A pytest selection (``pytest``
or ``python -m pytest`` as its command) is run with ``--junitxml`` added:
it is red only when it exits 1 having run a test that failed, green only
when it exits 0 having run at least one test, and inconclusive otherwise
(exit 5, no test collected; 2, 3 or 4, interrupted or misused; a run whose
tests were all skipped). Any other command is read by its exit code alone:
0 green, 1 red, anything else inconclusive.

**Isolation** (R5-R44, d-cm-8ap.19-12). Each run is launched through
green_gate's own wrapping (``green_gate.isolated_argv``): locally in the OS
sandbox with the worktree and the run's scratch directory the only
writable places, and a missing sandbox refuses the whole run before any
entry (``SandboxMissing``); on a hosted runner as it is. Its environment is
green_gate's ``sandbox_environment`` with the worktree first on PYTHONPATH,
so the mutated code is the code imported. The runner holds each process
group itself, so a terminated run kills its selections and removes its
worktrees (gate/worktrees.py) rather than waiting for them.

**Never the real checkout** (R5-R34). Every selection runs in a worktree
the runner made; there is no other path.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import difflib
import os
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass
from pathlib import Path

from curamoir import green_gate
from curamoir.gate import mutation_list, worktrees
from curamoir.termination import TerminationSignals

RED, GREEN, STALE, INCONCLUSIVE = "red", "green", "stale", "inconclusive"
RESULTS = (RED, GREEN, STALE, INCONCLUSIVE)
BLOCKING = (GREEN, STALE, INCONCLUSIVE)

#: What a result keeps of its selection's output: the last this many
#: characters, as a round-0 step's record does.
OUTPUT_TAIL = 4000

#: pytest's exit codes (pytest.ExitCode): a run whose tests failed, and the
#: one that collected nothing.
PYTEST_FAILED = 1
PYTEST_NO_TESTS = 5


class SandboxMissing(Exception):
    """No OS sandbox confines a selection here, so nothing was run."""


@dataclass
class Result:
    """One entry's result: what the JSON line carries."""

    index: int
    total: int
    path: str
    locate: str
    mutation: str
    behaviour: str | None
    result: str
    reason: str
    worktree: str | None = None
    diff: str = ""
    output_tail: str = ""
    duration_s: float = 0.0

    def line(self) -> dict:
        return asdict(self)


def run_mutations(repo, block: str, *, base: str, head: str, runner, cap: int,
                  timeout: float, scope: str | None = None, on_result=None,
                  notices=None) -> list:
    """Every entry of ``block`` (the block's content), run in worktrees at
    ``head``, at most ``cap`` at once; the Results in entry order.
    ``on_result(result)`` is called as each finishes; ``notices(text)`` gets
    the run's notices (orphans from dead runs). MutationListError for a
    block the contract refuses and SandboxMissing for no OS sandbox, both
    before any worktree is made; WorktreeError when the run directory
    cannot be made, before any entry runs (a worktree that cannot be made
    for one entry is that entry's INCONCLUSIVE)."""
    entries = mutation_list.parse_entries(block)
    repo = Path(repo)
    hosted = runner.kind.value == "hosted"
    _require_sandbox(repo, hosted)
    for sentence in worktrees.orphan_sentences(repo):
        if notices is not None:
            notices(sentence)
    total = len(entries)
    groups = _Groups()
    results = {}
    with TerminationSignals(), worktrees.RunWorktrees(repo, scope, base=base,
                                                      head=head) as trees:
        adding = threading.Lock()

        def one(entry):
            # A worktree that cannot be made for one entry is that entry's
            # INCONCLUSIVE; the run goes on (Rob, 2026-11-24). Only the run
            # directory, made before any entry, refuses the whole run.
            try:
                with adding:
                    path = trees.add()
            except worktrees.WorktreeError as error:
                result = Result(entry.index, total, entry.path, entry.describe_locate(),
                                entry.describe_mutation(), entry.behaviour, INCONCLUSIVE,
                                f"no worktree for the entry: {error}")
            else:
                result = _run_entry(repo, entry, total, path, hosted, timeout, groups)
            if on_result is not None:
                on_result(result)
            return result

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, cap))
        try:
            futures = {pool.submit(one, entry): entry for entry in entries}
            for future in concurrent.futures.as_completed(futures):
                results[futures[future].index] = future.result()
        except BaseException:
            # Terminated, ^C or a failure: kill what runs, start nothing
            # more; leaving the block then removes every worktree.
            groups.stop()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
    return [results[entry.index] for entry in entries]


def _require_sandbox(repo: Path, hosted: bool) -> None:
    if hosted:
        return
    with tempfile.TemporaryDirectory(prefix="curamoir-mutation-probe-") as scratch:
        try:
            green_gate.isolated_argv(["true"], str(repo), scratch, {}, hosted=False)
        except green_gate.GateError as error:
            raise SandboxMissing(str(error)) from error


def _run_entry(repo, entry, total, path, hosted, timeout, groups) -> Result:
    start = time.monotonic()

    def result(kind, reason, diff="", tail=""):
        return Result(entry.index, total, entry.path, entry.describe_locate(),
                      entry.describe_mutation(), entry.behaviour, kind, reason, str(path),
                      diff, tail[-OUTPUT_TAIL:], round(time.monotonic() - start, 3))

    target = _confined(path, entry.path)
    if isinstance(target, str):
        return result(STALE, target)
    try:
        source = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return result(STALE, f"{entry.path} cannot be read at head: {error}")
    try:
        first, last = mutation_list.region(source, entry)
        mutated = mutation_list.mutate(source, entry, first, last)
    except mutation_list.Stale as stale:
        return result(STALE, str(stale))
    diff = "".join(difflib.unified_diff(source.splitlines(keepends=True),
                                        mutated.splitlines(keepends=True),
                                        f"a/{entry.path}", f"b/{entry.path}"))
    try:
        argv = shlex.split(entry.expect_red)
    except ValueError as error:
        return result(INCONCLUSIVE, f"expect_red does not parse as a command: {error}", diff)
    baseline = _run_selection(repo, path, argv, hosted, timeout, groups)
    if baseline.verdict != "pass":
        return result(INCONCLUSIVE, f"the selection does not pass unmutated "
                                    f"({baseline.why}), so a red mutant would prove nothing",
                      diff, baseline.output)
    try:
        _write_no_follow(target, mutated)
    except OSError as error:
        return result(INCONCLUSIVE, f"the mutation could not be written: {error}", diff)
    mutant = _run_selection(repo, path, argv, hosted, timeout, groups)
    if mutant.verdict == "fail":
        return result(RED, f"the mutation was killed ({mutant.why})", diff, mutant.output)
    if mutant.verdict == "pass":
        named = entry.behaviour or f"{entry.path} {entry.describe_locate()}"
        return result(GREEN, f"the mutant survived: the selection still passes with "
                             f"{entry.describe_mutation()} made, so nothing guards "
                             f"{named!r}", diff, mutant.output)
    return result(INCONCLUSIVE, f"the mutant run did not show a failure ({mutant.why})",
                  diff, mutant.output)


def _confined(worktree: Path, relative: str):
    """The entry's file in the worktree as a Path, or why it cannot be one.
    The runner reads and writes it outside the sandbox, so no component of
    the path may be a symlink (a committed link to ~/.ssh, a linked
    directory), and the file must be a regular file inside the worktree."""
    current = worktree
    for part in relative.split("/"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError as error:
            return f"{relative} is not in the tree at head: {error.strerror or error}"
        if stat.S_ISLNK(mode):
            return (f"{relative} goes through a symlink ({current.relative_to(worktree)}), "
                    "which the runner does not follow")
    if not stat.S_ISREG(mode):
        return f"{relative} is not a regular file at head"
    if not current.resolve().is_relative_to(worktree.resolve()):
        return f"{relative} resolves outside the worktree"
    return current


def _write_no_follow(target: Path, text: str) -> None:
    """Write `text` over `target`, refusing to follow a symlink put there
    since it was checked."""
    descriptor = os.open(target, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


@dataclass(frozen=True)
class _Run:
    verdict: str        # pass | fail | neither
    why: str
    output: str


def _is_pytest(argv) -> bool:
    if not argv:
        return False
    if os.path.basename(argv[0]) in ("pytest", "py.test"):
        return True
    return any(argv[i] == "-m" and argv[i + 1] == "pytest" for i in range(len(argv) - 1))


def _run_selection(repo, path, argv, hosted, timeout, groups) -> _Run:
    with tempfile.TemporaryDirectory(prefix="curamoir-mutation-run-") as scratch:
        junit = Path(scratch) / "junit.xml"
        pytest = _is_pytest(argv)
        command = [*argv, f"--junitxml={junit}"] if pytest else list(argv)
        env = green_gate.sandbox_environment(repo, root=str(path),
                                             src=(path / "src").is_dir())
        try:
            env = green_gate.scratch_environment(path, env, scratch)
            wrapped = green_gate.isolated_argv(command, str(path), scratch, env,
                                               hosted=hosted)
        except (green_gate.GateError, OSError) as error:
            return _Run("neither", f"could not be launched: {error}", "")
        code, output = groups.run(wrapped, cwd=str(path), env=env, timeout=timeout)
        if code is None:
            return _Run("neither", output.splitlines()[-1] if output else "stopped", output)
        if not pytest:
            if code == 0:
                return _Run("pass", "exit 0", output)
            if code == 1:
                return _Run("fail", "exit 1", output)
            return _Run("neither", f"exit {code}", output)
        ran, failed = _junit_counts(junit)
        if code == PYTEST_NO_TESTS or ran == 0:
            return _Run("neither", "no test ran (none collected, or every one skipped or "
                                   "deselected)", output)
        if code == 0:
            return _Run("pass", f"{ran} test(s) passed", output)
        if code == PYTEST_FAILED and failed:
            return _Run("fail", f"{failed} of {ran} test(s) failed", output)
        return _Run("neither", f"pytest exit {code}", output)


def _junit_counts(junit: Path) -> tuple:
    """(tests that ran, tests that failed) in pytest's JUnit XML; (0, 0)
    when there is none to read."""
    try:
        data = junit.read_bytes()
    except OSError:
        return 0, 0
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return 0, 0
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return 0, 0
    ran = failed = 0
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        if "skipped" in tags:
            continue
        ran += 1
        if "failure" in tags:
            failed += 1
    return ran, failed


class _Groups:
    """The process groups of the selections running now, so that a stopped
    run can kill them all (module docstring, "Isolation")."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = set()
        self._stopped = threading.Event()

    def run(self, argv, *, cwd, env, timeout) -> tuple:
        """(exit code or None, combined output). None when it timed out, was
        stopped or could not start, with the reason last in the output."""
        if self._stopped.is_set():
            return None, "stopped: the run is ending"
        with tempfile.TemporaryFile() as out:
            try:
                process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=subprocess.STDOUT,
                                           start_new_session=True)
            except OSError as error:
                return None, f"could not start: {error}"
            with self._lock:
                self._running.add(process)
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if self._stopped.is_set():
                        _kill(process)
                        return None, _read(out) + "\nstopped: the run is ending"
                    if time.monotonic() > deadline:
                        _kill(process)
                        return None, _read(out) + f"\ntimed out after {timeout:g}s"
                    time.sleep(0.05)
                _kill(process)      # anything it left in its group goes with it
                return process.returncode, _read(out)
            finally:
                with self._lock:
                    self._running.discard(process)

    def stop(self):
        self._stopped.set()
        with self._lock:
            running = list(self._running)
        for process in running:
            _kill(process)


def _kill(process):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def _read(handle) -> str:
    handle.seek(0)
    data = handle.read()
    return data[-4 * OUTPUT_TAIL:].decode("utf-8", "replace")


def summary(results) -> str:
    """One sentence for the run's outcome."""
    counts = {kind: sum(1 for r in results if r.result == kind) for kind in RESULTS}
    if not counts[GREEN] and not counts[STALE] and not counts[INCONCLUSIVE]:
        return f"all {len(results)} mutation(s) were killed"
    parts = [f"{counts[kind]} {kind}" for kind in (GREEN, STALE, INCONCLUSIVE) if counts[kind]]
    return (f"{', '.join(parts)} of {len(results)} mutation(s): "
            + "; ".join(f"entry {r.index} {r.result}: {r.reason}" for r in results
                        if r.result in BLOCKING))

