"""gate/mutations.py and gate/worktrees.py (cm-8ap.19): the mutation runner.

Spec R5-R30 to R5-R34 and R5-R38 to R5-R44, §10 bead 4 (v1.15): a planted
surviving mutation is reported green and a planted selection that runs no
test INCONCLUSIVE, both with no model; a killed run leaves no worktree; the
hosted path runs without an OS sandbox while the local path refuses without
one. Every run here is real: real git worktrees, real pytest, and on the
local runner the real OS sandbox (bwrap or sandbox-exec), so "red", "green"
and "inconclusive" are what the runner reads from a real selection.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from curamoir import doctor, green_gate
from curamoir.config import LOCAL_RUNNER, Runner, RunnerKind
from curamoir.gate import mutation_list, mutations, worktrees
from tests.os_sandbox_stand_in import Absent

HOSTED = Runner(RunnerKind.HOSTED)
PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"

CORE = textwrap.dedent('''\
    def classify(spec):
        if spec.get("slos"):
            return "gate"
        return "api"
''')
TESTS = textwrap.dedent('''\
    from pkg.core import classify


    def test_gate():
        assert classify({"slos": [1]}) == "gate"


    def test_weak():
        assert classify({}) in ("api", "gate", None)


    def test_slow():
        import time
        time.sleep(float(__import__("os").environ.get("SLOW", "0")))
''')


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "core.py").write_text(CORE, encoding="utf-8")
    (root / "tests" / "test_core.py").write_text(TESTS, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def block(*entries):
    return "".join(textwrap.dedent(e) for e in entries)


KILLED = f'''\
    - path: src/pkg/core.py
      locate: {{anchor: "def classify("}}
      mutation: invert_condition
      expect_red: "{PYTEST} tests/test_core.py::test_gate"
      behaviour: "a manifest with SLOs is a gate"
'''
SURVIVES = f'''\
    - path: src/pkg/core.py
      locate: {{lines: [4, 4]}}
      mutation: return_constant
      expect_red: "{PYTEST} tests/test_core.py::test_weak"
'''
RUNS_NOTHING = f'''\
    - path: src/pkg/core.py
      locate: {{lines: [4, 4]}}
      mutation: return_constant
      expect_red: "{PYTEST} tests/test_core.py -k matches_nothing"
'''


def run(repo, text, runner=LOCAL_RUNNER, cap=2, timeout=120, notices=None):
    head = _git(repo, "rev-parse", "HEAD")
    return mutations.run_mutations(repo, text, base=head, head=head, runner=runner, cap=cap,
                                   timeout=timeout, notices=notices)


def no_worktrees_left(repo):
    listed = _git(repo, "worktree", "list", "--porcelain")
    return listed.count("worktree ") == 1 and not any(
        (worktrees.worktrees_root(repo)).rglob("mutation-*"))


# --- the acceptance cases --------------------------------------------------------

def test_a_killed_mutation_is_red_a_surviving_one_green_and_an_empty_selection_inconclusive(
        repo):
    results = run(repo, block(KILLED, SURVIVES, RUNS_NOTHING))

    assert [r.result for r in results] == ["red", "green", "inconclusive"]
    assert [r.index for r in results] == [1, 2, 3] and {r.total for r in results} == {3}
    red, green, empty = results
    assert "1 of 1 test(s) failed" in red.reason
    assert "survived" in green.reason and "src/pkg/core.py lines 4-4" in green.reason
    assert green.diff.splitlines()[-2:] == ['-    return "api"', "+    return None"]
    assert "no test ran" in empty.reason
    assert no_worktrees_left(repo)
    # The user's checkout is never touched (R5-R34).
    assert (repo / "src" / "pkg" / "core.py").read_text(encoding="utf-8") == CORE
    assert _git(repo, "status", "--porcelain") == ""


def test_a_green_names_the_behaviour_the_entry_gives():
    named = mutation_list.parse_entries(textwrap.dedent(SURVIVES) +
                                        '  behaviour: "an empty spec is api"\n')[0]
    assert named.behaviour == "an empty spec is api"


def test_a_green_quotes_its_behaviour(repo):
    (green,) = run(repo, block(SURVIVES + '      behaviour: "an empty spec is api"\n'))

    assert green.result == "green" and "'an empty spec is api'" in green.reason


def test_a_selection_that_fails_unmutated_is_inconclusive(repo):
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{sys.executable} -c 'import sys; sys.exit(1)'"
    '''))

    assert result.result == "inconclusive"
    assert "does not pass unmutated (exit 1)" in result.reason


@pytest.mark.parametrize(("code", "expected"), [(1, "red"), (2, "inconclusive"),
                                                (0, "green")])
def test_a_command_that_is_not_pytest_is_read_by_its_exit_code(repo, code, expected):
    # Passes unmutated (core.py returns "api"), then exits `code` with the
    # mutation (return None).
    script = ("import sys, pkg.core as c; r = c.classify({}); "
              f"sys.exit(0 if r == 'api' else {code})")
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{sys.executable} -c \\"{script}\\""
    '''))

    assert result.result == expected, result.reason


def test_an_entry_that_no_longer_fits_the_code_is_stale_and_the_rest_still_run(repo):
    results = run(repo, block(KILLED.replace('anchor: "def classify("', 'anchor: "def gone("'),
                              KILLED))

    assert [r.result for r in results] == ["stale", "red"]
    assert "not one" in results[0].reason


def test_a_path_through_a_symlink_is_stale_and_nothing_outside_is_written(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1 == 1\n", encoding="utf-8")
    (repo / "src" / "link.py").symlink_to(outside)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a link")

    (result,) = run(repo, block(f'''\
        - path: src/link.py
          locate: {{lines: [1, 1]}}
          mutation: swap_operator
          expect_red: "{PYTEST} tests/test_core.py::test_gate"
    '''))

    assert result.result == "stale" and "symlink" in result.reason
    assert outside.read_text(encoding="utf-8") == "x = 1 == 1\n"


def test_a_timed_out_selection_is_inconclusive(repo, monkeypatch):
    monkeypatch.setenv("SLOW", "5")
    monkeypatch.setattr(green_gate, "_INHERITED", (*green_gate._INHERITED, "SLOW"))
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{PYTEST} tests/test_core.py::test_slow"
    '''), timeout=1)

    assert result.result == "inconclusive" and "timed out after 1s" in result.reason


# --- isolation (R5-R44) --------------------------------------------------------

def test_the_local_runner_refuses_without_an_os_sandbox(repo, monkeypatch):
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())

    with pytest.raises(mutations.SandboxMissing, match="no OS sandbox"):
        run(repo, block(KILLED))
    assert not worktrees.worktrees_root(repo).exists()


def test_the_hosted_runner_runs_without_an_os_sandbox(repo, monkeypatch):
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())

    results = run(repo, block(KILLED, SURVIVES), runner=HOSTED)

    assert [r.result for r in results] == ["red", "green"]


def test_the_selection_imports_the_worktrees_code_not_the_checkouts(repo):
    """sandbox_environment puts the worktree first on PYTHONPATH: the
    mutation is in the worktree only, and it is what the selection sees."""
    (result,) = run(repo, block(KILLED))

    assert result.result == "red"
    assert "assert None == 'gate'" in result.output_tail or "'api'" in result.output_tail


# --- the cap (R5-R40) ----------------------------------------------------------------

@pytest.mark.parametrize(("runner", "configured", "explicit", "cores", "cap"), [
    (LOCAL_RUNNER, 2, False, 16, 2),
    (LOCAL_RUNNER, 5, True, 1, 5),
    (HOSTED, 2, False, 4, 4),
    (HOSTED, 2, False, 32, 8),
    (HOSTED, 3, True, 16, 3),
    (HOSTED, 12, True, 16, 8),
])
def test_the_worktree_cap(monkeypatch, runner, configured, explicit, cores, cap):
    monkeypatch.setattr(os, "cpu_count", lambda: cores)

    assert worktrees.max_parallel(runner, configured, explicit) == cap


def test_no_more_than_the_cap_run_at_once(repo, monkeypatch):
    monkeypatch.setenv("SLOW", "1")
    monkeypatch.setattr(green_gate, "_INHERITED", (*green_gate._INHERITED, "SLOW"))
    slow = f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{PYTEST} tests/test_core.py::test_slow"
    '''
    seen, peak = [], 0
    original = worktrees.RunWorktrees.add

    def counting(self):
        nonlocal peak
        path = original(self)
        live = [p for p in self.created if Path(p).exists()]
        seen.append(len(live))
        peak = max(peak, len(live))
        return path

    monkeypatch.setattr(worktrees.RunWorktrees, "add", counting)
    started = time.monotonic()
    results = run(repo, block(slow, slow, slow, slow), cap=2)

    assert len(results) == 4 and peak <= 4
    # Two at a time, each about two seconds (baseline and mutant): about 4 s,
    # not 8 s one by one, and not 2 s all at once.
    elapsed = time.monotonic() - started
    assert 3.5 < elapsed < 7.5, elapsed


# --- the lifecycle (R5-R41, R5-R42) ------------------------------------------------

DRIVER = textwrap.dedent('''\
    import sys
    from curamoir import green_gate
    from curamoir.config import LOCAL_RUNNER
    from curamoir.gate import mutations
    # The selection sleeps for SLOW seconds; let it through the allowlist.
    green_gate._INHERITED = (*green_gate._INHERITED, "SLOW")
    repo, head, text = sys.argv[1], sys.argv[2], open(sys.argv[3]).read()
    mutations.run_mutations(repo, text, base=head, head=head, runner=LOCAL_RUNNER, cap=2,
                            timeout=120)
''')


def _start_a_slow_run(repo, tmp_path):
    slow = f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{PYTEST} tests/test_core.py::test_slow"
    '''
    listing = tmp_path / "block.yaml"
    listing.write_text(block(slow, slow), encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    env = dict(os.environ, SLOW="60")
    env.pop("PYTEST_ADDOPTS", None)
    process = subprocess.Popen([sys.executable, str(driver), str(repo),
                                _git(repo, "rev-parse", "HEAD"), str(listing)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process


def _wait_for_worktrees(repo, count, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(list(worktrees.worktrees_root(repo).rglob("mutation-*"))) >= count:
            return
        time.sleep(0.1)
    raise AssertionError("the run never made its worktrees")


def test_a_terminated_run_kills_its_selections_and_leaves_no_worktree(repo, tmp_path):
    process = _start_a_slow_run(repo, tmp_path)
    _wait_for_worktrees(repo, 2)
    time.sleep(1)       # the selections are running

    process.send_signal(signal.SIGTERM)
    process.wait(timeout=30)

    assert process.returncode != 0
    assert no_worktrees_left(repo)
    assert worktrees.orphans(repo) == []


def test_a_run_killed_outright_is_an_orphan_doctor_and_the_next_run_report(repo, tmp_path):
    process = _start_a_slow_run(repo, tmp_path)
    _wait_for_worktrees(repo, 1)
    # While it lives it is not an orphan: its owner lock is held.
    assert worktrees.orphans(repo) == []

    process.send_signal(signal.SIGKILL)
    process.wait(timeout=30)

    (orphan,) = worktrees.orphans(repo)
    assert orphan[1] == worktrees.STANDALONE
    owner = json.loads((orphan[0] / worktrees.OWNER_FILE).read_text(encoding="utf-8"))
    assert owner["pid"] == process.pid
    (repo / ".curamoir").mkdir()
    found = [f for f in doctor.diagnose(repo, tools={"ast-grep": True, "bd": True, "gh": True,
                                                     "claude": True})
             if f.check == "worktrees"]
    assert [f.level for f in found] == ["warning"]
    assert str(orphan[0]) in found[0].detail
    notices = []
    run(repo, block(KILLED), notices=notices.append)
    assert notices == [worktrees.ORPHAN_SENTENCE.format(path=orphan[0], scope="standalone")]


def test_doctor_reports_no_orphan_without_one(repo):
    (repo / ".curamoir").mkdir()

    found = [f for f in doctor.diagnose(repo, tools={}) if f.check == "worktrees"]

    assert [(f.level, f.detail) for f in found] == [("ok", "no orphan R5 worktrees")]


def test_worktrees_live_under_the_git_common_dir_never_the_working_tree(repo):
    root = worktrees.worktrees_root(repo)

    assert root == (repo / ".git" / "r5-worktrees").resolve()
    with worktrees.RunWorktrees(repo, "cm-x", base="0" * 40,
                                head=_git(repo, "rev-parse", "HEAD")) as trees:
        path = trees.add()
        assert path.parent.parent == root / "cm-x"
        assert path.name == "mutation-1" and (path / "src" / "pkg" / "core.py").is_file()
        other = worktrees.RunWorktrees(repo, "cm-x", base="0" * 40, head="0" * 40)
        assert other.run_id != trees.run_id
    assert not path.exists() and trees.removed == [str(path)]


def test_a_parent_swapped_for_a_symlink_during_the_baseline_is_not_followed(repo, tmp_path):
    """The baseline runs the repository's code, which may write in its
    worktree: a parent directory replaced by a link to elsewhere before the
    mutation is written is refused, and nothing outside is touched."""
    outside = tmp_path / "elsewhere"
    (outside / "pkg").mkdir(parents=True)
    (outside / "pkg" / "core.py").write_text(CORE, encoding="utf-8")
    swap = ("import os, shutil; shutil.move('src', 'src.moved'); "
            f"os.symlink({str(outside)!r}, 'src')")
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{sys.executable} -c \\"{swap}\\""
    '''), runner=HOSTED)

    assert result.result == "inconclusive"
    assert "could not be written" in result.reason
    assert (outside / "pkg" / "core.py").read_text(encoding="utf-8") == CORE



# --- correctness review, round 1 ------------------------------------------------

def test_a_files_line_endings_are_kept(repo):
    (repo / "src" / "pkg" / "core.py").write_bytes(CORE.replace("\n", "\r\n").encode())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "crlf")
    head = _git(repo, "rev-parse", "HEAD")
    with worktrees.RunWorktrees(repo, None, base=head, head=head) as trees:
        path = trees.add()
        entry = mutation_list.parse_entries(textwrap.dedent(SURVIVES))[0]
        groups = mutations._Groups()
        mutations._run_entry(repo, entry, 1, path, True, 60, groups)
        written = (path / "src" / "pkg" / "core.py").read_bytes()
    assert written.count(b"\r\n") == 4 and b"return None\r\n" in written


def test_a_fifo_put_in_place_of_the_file_does_not_hang_the_write(repo, tmp_path):
    swap = "import os; os.remove('src/pkg/core.py'); os.mkfifo('src/pkg/core.py')"
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{sys.executable} -c \\"{swap}\\""
    '''), runner=HOSTED, timeout=30)

    assert result.result == "inconclusive" and "could not be written" in result.reason


def test_a_sandbox_that_cannot_confine_refuses_before_any_worktree(repo, monkeypatch):
    class Broken(Absent):
        def wrap(self, argv, **kwargs):
            return list(argv)

    monkeypatch.setattr(green_gate, "OS_SANDBOX", Broken())

    with pytest.raises(mutations.SandboxMissing):
        run(repo, block(KILLED))
    assert not worktrees.worktrees_root(repo).exists()


def test_a_cleanup_failure_after_the_entries_keeps_their_results(repo, monkeypatch):
    real = worktrees.RunWorktrees.__exit__

    def fail(self, *exc):
        real(self, *exc)
        raise worktrees.CleanupError("worktrees could not be removed: planted")

    monkeypatch.setattr(worktrees.RunWorktrees, "__exit__", fail)

    with pytest.raises(mutations.CleanupFailed) as caught:
        run(repo, block(KILLED))
    assert [r.result for r in caught.value.results] == ["red"]


def test_a_probe_never_creates_a_lock_and_a_starting_run_is_not_an_orphan(repo):
    head = _git(repo, "rev-parse", "HEAD")
    root = worktrees.worktrees_root(repo)
    (root / "standalone" / ".20261125T000000Z-1-abc.starting").mkdir(parents=True)
    bare = root / "standalone" / "20261125T000000Z-2-def"
    bare.mkdir()

    found = worktrees.orphans(repo)

    assert [path for path, _ in found] == [bare]
    assert not (bare / worktrees.OWNER_LOCK).exists()
    with worktrees.RunWorktrees(repo, None, base=head, head=head) as trees:
        assert trees.directory.exists() and trees.directory not in [p for p, _ in
                                                                      worktrees.orphans(repo)]


# --- edge-cases review, round 1 --------------------------------------------------

@pytest.mark.parametrize("launch", [
    "env -u NOTHING {py} -m pytest", "{py} -X dev -m pytest", "{py} -W error -m pytest",
    "timeout -s KILL 60 {py} -m pytest", "{py} -B -m pytest",
])
def test_pytest_is_recognised_however_it_is_launched(repo, launch):
    """No command line is parsed: PYTEST_ADDOPTS reaches pytest behind any
    wrapper, so an all-skipped selection is inconclusive, never green."""
    command = launch.format(py=sys.executable) + " -q -p no:cacheprovider " \
        "tests/test_core.py -k matches_nothing"
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{command}"
    '''), runner=HOSTED)

    assert result.result == "inconclusive" and "no test ran" in result.reason


def test_a_command_that_only_names_pytest_is_read_by_its_exit_code(repo):
    """A `make pytest` target, a tox env called pytest: the word is not the
    program, the command is left as written, and no JUnit file appears."""
    script = ("import sys, pkg.core as c; r = c.classify({}); "
              "sys.exit(0 if r == 'api' else 1)")
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "{sys.executable} -c \\"{script}\\" pytest"
    '''))

    assert result.result == "red" and "exit 1" in result.reason


def test_a_wrapped_pytest_that_runs_no_test_is_inconclusive(repo):
    (result,) = run(repo, block(f'''\
        - path: src/pkg/core.py
          locate: {{lines: [4, 4]}}
          mutation: return_constant
          expect_red: "env A=1 {PYTEST} tests/test_core.py -k matches_nothing"
    '''))

    assert result.result == "inconclusive" and "no test ran" in result.reason


def test_a_byte_order_mark_is_kept_and_the_file_still_parses(repo):
    (repo / "src" / "pkg" / "core.py").write_text("﻿" + CORE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bom")
    head = _git(repo, "rev-parse", "HEAD")
    with worktrees.RunWorktrees(repo, None, base=head, head=head) as trees:
        path = trees.add()
        entry = mutation_list.parse_entries(textwrap.dedent(SURVIVES))[0]
        result = mutations._run_entry(repo, entry, 1, path, True, 60, mutations._Groups())
        written = (path / "src" / "pkg" / "core.py").read_text(encoding="utf-8")
    assert result.result == "green"
    assert written.startswith("﻿") and "return None" in written


def test_a_file_that_is_not_utf8_is_inconclusive_not_drift(repo):
    (repo / "src" / "pkg" / "core.py").write_bytes(b"# -*- coding: latin-1 -*-\nx = '\xe9'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "latin-1")

    (result,) = run(repo, block(SURVIVES.replace("[4, 4]", "[2, 2]")), runner=HOSTED)

    assert result.result == "inconclusive" and "is not UTF-8" in result.reason


def test_the_output_kept_is_the_tail_read_from_the_end(tmp_path):
    with open(tmp_path / "out", "w+b") as handle:
        handle.write(b"x" * 1_000_000 + b"END")
        assert mutations._read(handle).endswith("END")
        assert len(mutations._read(handle)) == 4 * mutations.OUTPUT_TAIL


def test_a_clean_run_does_not_prune_the_users_other_worktrees(repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    _git(repo, "worktree", "add", "--detach", str(elsewhere))
    import shutil
    shutil.rmtree(elsewhere)        # gone for now: unmounted media, say

    run(repo, block(KILLED))

    assert str(elsewhere) in _git(repo, "worktree", "list", "--porcelain")



def test_a_finishing_run_is_never_seen_as_an_orphan(repo, monkeypatch):
    """Every unlink of the run's own files happens out of the probes' sight."""
    head = _git(repo, "rev-parse", "HEAD")
    seen, real = [], os.unlink

    def spying(path, *args, **kwargs):
        real(path, *args, **kwargs)
        seen.append(worktrees.orphans(repo))

    with worktrees.RunWorktrees(repo, None, base=head, head=head) as trees:
        trees.add()
        monkeypatch.setattr(os, "unlink", spying)
    monkeypatch.setattr(os, "unlink", real)
    assert seen and all(found == [] for found in seen)
