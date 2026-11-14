"""Every test runs under a per-test time limit (cm-3aj).

The full suite hung now and then, asleep in poll with no child processes,
in a different test each time (test_sweep_job, test_github_rest,
test_idempotence), and each of those passed when run alone. Without a
limit a hang ran until the caller's own wall-clock limit killed the run, and
nothing said which test it was. pytest-timeout turns the hang into a failure
with every thread's stack.

The method is `thread`, not `signal`: `curamoir.lint._time_limit` bounds a
rule with SIGALRM through `setitimer`, and on its way out it sets the timer
to 0, which would cancel a `signal`-method timeout for the rest of the test.
The `thread` method uses no signal. On a timeout it prints every thread's
stack and ends the whole run. The stack names the hanging test's file,
function and line, but not a parametrized test's id; under the usual `-q`
run that id is not shown anywhere, and under `-v` it is the last progress
line before the stack.

The thread method's timer is Python code, so it cannot fire while a test
holds the GIL, as a regex that backtracks for ever does. For that,
`faulthandler_timeout` (pytest's own, a C watchdog) is set a little above the
limit: it prints every thread's stack while the GIL is held and, with
`faulthandler_exit_on_timeout`, ends the run as the thread method does.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest_timeout

ROOT = Path(__file__).resolve().parents[1]

#: Mirror `timeout` and `faulthandler_timeout` in pyproject.toml, whose
#: comment gives the reasoning.
LIMIT = 120.0
WATCHDOG = 150.0


def test_every_test_runs_under_the_thread_method_time_limit(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("timeout")
    # What is in force, not just what is configured: pytest-timeout's own
    # resolver, which puts --timeout, PYTEST_TIMEOUT and --timeout-method
    # ahead of the ini values.
    settings = pytest_timeout.get_env_settings(pytestconfig)
    assert (settings.timeout, settings.method) == (LIMIT, "thread")
    assert float(pytestconfig.getini("faulthandler_timeout")) == WATCHDOG
    assert pytestconfig.getini("faulthandler_exit_on_timeout") is True


# The probe below, in seconds, smallest first: the shortened limit; the
# bound the run must finish inside, which only a working limit meets; the
# guard that stops this test if the limit does nothing; and the sleep, longer
# than all three, so that only the limit can end it.
SHORT_LIMIT, STOPPED_WITHIN, GUARD, HANG = 2, 30, 50, 60

SLEEP = f"time.sleep({HANG})"
HANGING_TEST = ["import time", "", "", "def test_that_hangs():", "    " + SLEEP]


def test_a_hanging_test_is_named_and_its_stack_printed(tmp_path):
    """The repository's own pytest configuration, with only the limit
    shortened, run on a test that sleeps far longer than that limit. Without
    `-v`, so the test is named by the stack dump alone."""
    (tmp_path / "test_hangs.py").write_text("\n".join(HANGING_TEST) + "\n")
    sleep_line = HANGING_TEST.index("    " + SLEEP) + 1
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"),
         "--rootdir", str(tmp_path), "-p", "no:cacheprovider", "-q",
         "-o", f"timeout={SHORT_LIMIT}", str(tmp_path / "test_hangs.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=GUARD)
    elapsed = time.monotonic() - started
    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert elapsed < STOPPED_WITHIN, f"the limit did not stop the hang ({elapsed:.0f} s)"
    assert "Timeout" in output
    assert f'test_hangs.py", line {sleep_line}, in test_that_hangs' in output
    assert SLEEP in output


#: A regex that backtracks for 2**40 steps inside C, holding the GIL.
GIL_HANG = ["import re", "", "", "def test_that_holds_the_gil():",
            '    re.match(r"(a+)+$", "a" * 40 + "b")']


def test_a_hang_that_holds_the_gil_is_named_and_ends_the_run(tmp_path):
    """The thread method cannot stop this test; the faulthandler watchdog,
    shortened to the same short limit, names it and ends the run."""
    (tmp_path / "test_gil.py").write_text("\n".join(GIL_HANG) + "\n")
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"),
         "--rootdir", str(tmp_path), "-p", "no:cacheprovider", "-q",
         "-o", f"timeout={SHORT_LIMIT}", "-o", f"faulthandler_timeout={SHORT_LIMIT}",
         str(tmp_path / "test_gil.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=GUARD)
    elapsed = time.monotonic() - started
    assert done.returncode != 0
    assert elapsed < STOPPED_WITHIN, f"the watchdog did not end the run ({elapsed:.0f} s)"
    assert (f'test_gil.py", line {len(GIL_HANG)} in test_that_holds_the_gil'
            in done.stdout + done.stderr)
