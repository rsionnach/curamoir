"""Every test runs under a per-test time limit (cm-3aj).

The full suite hung now and then, asleep in poll with no child processes,
in a different test each time (test_sweep_job, test_github_rest,
test_idempotence), and each of those passed when run alone. Without a
limit a hang ran until the caller's own wall-clock limit killed the run, and
nothing said which test it was. pytest-timeout turns the hang into a named
failure with every thread's stack.

The method is `thread`, not `signal`: curamoir.lint bounds a rule with
SIGALRM through `setitimer`, and on its way out it sets the timer to 0, which
would cancel a `signal`-method timeout for the rest of the test. The `thread`
method uses no signal. On a timeout it prints every thread's stack and ends
the whole run. The stack names the hanging test's file, function and line,
but not a parametrized test's id: that comes from `-v`, whose last progress
line is then the hanging test, so run the full suite with `-v`.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The per-test limit in pyproject.toml, in seconds. The slowest test took
#: 12 s in a full run on 2026-11-14 (test_hook_budgets' PreCompact hook), so
#: this is ten times that; it is a backstop against a hang, not a budget.
LIMIT = 120.0


def test_every_test_runs_under_the_thread_method_time_limit(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("timeout")
    assert float(pytestconfig.getini("timeout")) == LIMIT
    assert pytestconfig.getini("timeout_method") == "thread"


def test_a_hanging_test_is_named_and_its_stack_printed(tmp_path):
    """The repository's own pytest configuration, with only the limit
    shortened, run on a test that sleeps far longer than that limit. Without
    `-v`, so the test is named by the stack dump alone."""
    (tmp_path / "test_hangs.py").write_text(
        "import time\n\n\ndef test_that_hangs():\n    time.sleep(60)\n")
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"),
         "--rootdir", str(tmp_path), "-p", "no:cacheprovider", "-q", "-o", "timeout=2",
         str(tmp_path / "test_hangs.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=50)
    elapsed = time.monotonic() - started
    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert elapsed < 30, f"the limit did not stop the hang ({elapsed:.0f} s)"
    assert "Timeout" in output
    assert 'test_hangs.py", line 5, in test_that_hangs' in output  # the frame
    assert "time.sleep(60)" in output
