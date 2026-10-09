"""The hooks' declared budgets, tested through the hooks.

A reviewer found that `_Deadline` was fully implemented, correct, and called by
nothing — the real SessionEnd hook still took 12.1s against a 1.5s shared
budget, because `sessionend.py` called `write_handoff(repo)` with no deadline
and the test passed one in by hand. The fix was one argument; what made it
invisible for a round was testing the mechanism instead of the wiring.

So these drive `main()` with a deliberately slow `git` on PATH. Setting
`SESSION_END_BUDGET_SECONDS = None` must make them fail.
"""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from curamoir.scaffold import init_repo

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture
def slow_git(tmp_path, monkeypatch):
    """A `git` that answers, but far too slowly for a hook budget."""
    directory = tmp_path / "slowbin"
    directory.mkdir()
    shim = directory / "git"
    shim.write_text("#!/bin/sh\nsleep 4\nexit 0\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ.get('PATH', '')}")
    return directory


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


def run_hook(module, repo, event):
    """Drive a hook the way Claude Code does: JSON on stdin, in its own process."""
    payload = json.dumps({"hook_event_name": event, "cwd": str(repo), "reason": "clear"})
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", module],
        input=payload, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    return time.monotonic() - started, result


def test_the_session_end_hook_returns_inside_its_shared_budget(repo, slow_git):
    """hooks.json allows SessionEnd 2s, and its own comment records that
    SessionEnd hooks SHARE 1.5s. `build_handoff` makes several git calls, at 5s
    each by default — so without the deadline the hook is killed having written
    nothing, on exactly the session most worth a handoff."""
    elapsed, result = run_hook("curamoir.hooks.sessionend", repo, "SessionEnd")

    assert result.returncode == 0, result.stderr
    assert elapsed < 1.5, f"took {elapsed:.2f}s against the 1.5s shared budget"
    assert (repo / ".curamoir" / "handoff" / "latest.md").is_file(), (
        "returned in time but wrote no handoff, which is the same loss"
    )


def test_the_precompact_hook_returns_inside_its_own_budget(repo, slow_git):
    """PreCompact gets 30s in hooks.json — the write that matters most — but it
    still must not spend the lot on a hung git."""
    elapsed, result = run_hook("curamoir.hooks.precompact", repo, "PreCompact")

    assert result.returncode == 0, result.stderr
    assert elapsed < 29, f"took {elapsed:.2f}s against the 30s hooks.json timeout"
    assert (repo / ".curamoir" / "handoff" / "latest.md").is_file()


def test_the_budgets_are_set_and_inside_what_hooks_json_allows():
    """Pinned as numbers too, so that setting either to None — which is how the
    regression was reintroducible — fails here as well as in the timing tests
    above, including on a machine where the timing is too noisy to trust."""
    from curamoir.hooks.precompact import PRE_COMPACT_BUDGET_SECONDS
    from curamoir.hooks.sessionend import SESSION_END_BUDGET_SECONDS

    declared = json.loads(
        (Path(__file__).resolve().parents[2] / "hooks" / "hooks.json").read_text(
            encoding="utf-8"
        )
    )["hooks"]

    def timeout_for(event):
        return declared[event][0]["hooks"][0]["timeout"]

    assert isinstance(SESSION_END_BUDGET_SECONDS, (int, float))
    assert 0 < SESSION_END_BUDGET_SECONDS <= 1.5, "SessionEnd hooks share 1.5s"
    assert timeout_for("SessionEnd") > SESSION_END_BUDGET_SECONDS

    assert isinstance(PRE_COMPACT_BUDGET_SECONDS, (int, float))
    assert 0 < PRE_COMPACT_BUDGET_SECONDS < timeout_for("PreCompact")
