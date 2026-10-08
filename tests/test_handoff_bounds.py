"""The handoff must stay useful on the sessions it exists for.

A handoff earns its keep on the sessions that ended badly, so its degraded
paths are its main paths. Each case here produced a FILE — nothing raised,
nothing was logged — that had quietly stopped carrying the thing it is for.
"""

import os
import stat
import time

from curamoir.handoff import build_handoff, write_handoff
from curamoir.scaffold import init_repo


def initialised(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


def git_repo(tmp_path):
    repo = initialised(tmp_path)
    import subprocess
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "commit", "-q", "--allow-empty", "-m", "first"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    return repo


def test_a_long_note_does_not_discard_the_rest_of_the_handoff(tmp_path):
    """`last_decision` is capped at 60 words; the developer's own note was
    capped at nothing, and `_cap_words` DROPS any line that does not fit. Past
    about 385 words everything went — branch, changes, the explicit next step,
    and the note itself — leaving a title and a truncation notice."""
    repo = git_repo(tmp_path)

    write_handoff(repo, note="word " * 600, next_step="Run the sweep on src/app.")

    rendered = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "**branch:**" in rendered, "the structural fields were dropped"
    assert "Intended next step" in rendered, "the most actionable field was dropped"


def test_a_detached_head_is_not_reported_as_a_branch_named_head(tmp_path):
    """Mid-rebase or mid-bisect is exactly the interrupted session a handoff is
    for, and `HEAD` reads as a branch name."""
    import subprocess

    repo = git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--detach", sha], cwd=repo,
                   check=True, capture_output=True)

    branch = build_handoff(repo)["branch"]

    assert branch != "HEAD"
    assert "detached" in branch.lower()


def test_write_handoff_returns_inside_the_session_end_budget(tmp_path, monkeypatch):
    """hooks.json gives SessionEnd 2s and notes that SessionEnd hooks SHARE a
    1.5s budget, while `build_handoff` made several git calls each allowed 5s.
    A cold network mount or a hung credential helper is slow, not down."""
    directory = tmp_path / "slowbin"
    directory.mkdir()
    shim = directory / "git"
    shim.write_text("#!/bin/sh\nsleep 4\nexit 0\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    repo = initialised(tmp_path)

    started = time.monotonic()
    write_handoff(repo, deadline_seconds=1.5)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"took {elapsed:.1f}s against a 1.5s declared budget"


def test_a_content_free_handoff_does_not_evict_the_last_informative_one(
    tmp_path, monkeypatch
):
    """With git unavailable every write renders the same empty shape, and
    archiving those pruned the last real handoff out of history within ten
    writes — a handful of sessions.

    The first version of this test passed for two wrong reasons, and the fix it
    guarded could be stubbed out entirely with the suite still green:

    1. its fixture never actually removed git from PATH, despite the docstring
       saying so, so every handoff was informative;
    2. it seeded the marker through `--note`, which `record_turn` journals — so
       `_last_decision` replayed the phrase into every LATER handoff, and the
       assertion found it whether or not the original survived.

    Both are fixed here: git is genuinely gone, and the informative handoff is
    seeded by writing the file, so the phrase exists in exactly one place.
    """
    repo = initialised(tmp_path)
    marker = "THE HANDOFF THAT MATTERS"
    latest = repo / ".curamoir" / "handoff" / "latest.md"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(f"# cúramóir handoff\n\n> {marker}\n", encoding="utf-8")

    monkeypatch.setenv("PATH", "")
    for _ in range(12):
        write_handoff(repo, keep_history=10)

    history = repo / ".curamoir" / "handoff" / "history"
    archived = "\n".join(p.read_text(encoding="utf-8") for p in history.iterdir())

    assert marker in archived, (
        "the only informative handoff was pruned away by content-free ones"
    )


def test_the_content_free_gate_is_not_satisfied_by_every_handoff(tmp_path, monkeypatch):
    """The gate's first version tested the rendered markdown for `## Intended
    next step`, which `_next_step` always emits — so it returned True for the
    exact handoff it existed to reject. Asserted on the predicate directly, so
    that the gate cannot quietly become a tautology again."""
    from curamoir.handoff import build_handoff, is_informative

    repo = initialised(tmp_path)
    monkeypatch.setenv("PATH", "")

    assert not is_informative(build_handoff(repo)), (
        "a handoff with no note, no git state and no claimed bead is not informative"
    )


def test_a_handoff_with_any_real_content_is_informative(tmp_path):
    """The gate must not reject a handoff worth keeping."""
    from curamoir.handoff import build_handoff, is_informative

    repo = git_repo(tmp_path)

    assert is_informative(build_handoff(repo))
    assert is_informative(dict(build_handoff(repo), note="a deliberate note"))


def test_a_long_next_step_renders_its_body_not_a_bare_heading(tmp_path):
    """`_cap_words`' skip keeps the short structural fields — and could leave
    `## Intended next step` with nothing under it, which promises the most
    actionable field in the file and delivers nothing."""
    repo = git_repo(tmp_path)

    write_handoff(repo, note="short note", next_step="word " * 600)

    rendered = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    if "## Intended next step" in rendered:
        after = rendered.split("## Intended next step", 1)[1].lstrip("\n")
        body = [line for line in after.splitlines() if line.strip()]
        assert body and not body[0].startswith("##"), "the heading has no body"


def test_a_handoff_written_in_the_future_is_not_reported_as_negative_age(tmp_path):
    """Clock skew, a restored backup, a synced mount. `brief` clamps; `status`
    printed "-3 day(s) old"."""
    from curamoir.status import collect_status

    repo = initialised(tmp_path)
    write_handoff(repo)
    latest = repo / ".curamoir" / "handoff" / "latest.md"
    future = time.time() + 3 * 86_400
    os.utime(latest, (future, future))

    age = collect_status(repo)["handoff"]["age_days"]

    assert age is None or age >= 0, f"reported an age of {age}"
