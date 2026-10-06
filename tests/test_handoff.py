"""E8-R1/R2/R5: the handoff, which must exist even when everything else fails.

The deterministic path is the PRIMARY path, not a fallback. A handoff is worth
having precisely on the sessions that went badly, so it is built from git and
bead state — both local, both cheap, both always available — and the model call
only ever condenses what is already there.

Everything here runs with no model and no network.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone

from curamoir.handoff import build_handoff, render_handoff, write_handoff


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "initial")
    (repo / ".curamoir" / "handoff" / "history").mkdir(parents=True)
    return repo


def test_a_handoff_names_the_branch_and_timestamp(tmp_path):
    repo = make_repo(tmp_path)

    handoff = build_handoff(repo)

    assert handoff["branch"] == "main"
    assert handoff["at"]


def test_uncommitted_work_is_summarised_not_diffed(tmp_path):
    """E8-R5: summaries, not raw diffs. A diff in a committed artefact is both
    noise and a leak risk."""
    repo = make_repo(tmp_path)
    (repo / "app.py").write_text("print('changed')\nSECRET = 1\n", encoding="utf-8")
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")

    handoff = build_handoff(repo)
    rendered = render_handoff(handoff)

    assert any("app.py" in change for change in handoff["uncommitted"])
    assert any("new.py" in change for change in handoff["uncommitted"])
    assert "print('changed')" not in rendered, "raw file content leaked into the handoff"
    assert "+++" not in rendered and "@@" not in rendered, "a diff leaked in"


def test_a_clean_tree_says_so_rather_than_listing_nothing(tmp_path):
    repo = make_repo(tmp_path)

    rendered = render_handoff(build_handoff(repo))

    assert "clean" in rendered.lower()


def test_in_progress_beads_are_named(tmp_path):
    repo = make_repo(tmp_path)
    beads = repo / ".beads"
    beads.mkdir()
    (beads / "issues.jsonl").write_text(
        json.dumps({"id": "x-1", "title": "Refactor the loader", "status": "in_progress"}) + "\n"
        + json.dumps({"id": "x-2", "title": "Something else", "status": "open"}) + "\n",
        encoding="utf-8")

    handoff = build_handoff(repo)

    assert any(bead["id"] == "x-1" for bead in handoff["in_progress"])
    assert all(bead["id"] != "x-2" for bead in handoff["in_progress"])


def test_the_last_decision_comes_from_the_journal_without_a_model_call(tmp_path):
    """Stop receives last_assistant_message, and the docs say to prefer it over
    reading the transcript. So the common case needs no model at all."""
    repo = make_repo(tmp_path)
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    journal.write_text(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": "Chose Dolt over SQLite."}) + "\n"
        + json.dumps({"at": "2026-10-06T11:00:00+00:00", "note": "Settled the guard fail-closed rule."}) + "\n",
        encoding="utf-8")

    handoff = build_handoff(repo)

    assert "fail-closed" in handoff["last_decision"]


def test_a_handoff_with_no_journal_still_renders(tmp_path):
    """E8-R2: the deterministic handoff must stand on its own."""
    repo = make_repo(tmp_path)

    rendered = render_handoff(build_handoff(repo))

    assert "main" in rendered
    assert len(rendered) > 0


def test_a_handoff_stays_under_four_hundred_words(tmp_path):
    """E8-R1's acceptance criterion, and it must hold when state is large."""
    repo = make_repo(tmp_path)
    for index in range(60):
        (repo / f"file_{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
    beads = repo / ".beads"
    beads.mkdir()
    beads.joinpath("issues.jsonl").write_text(
        "\n".join(
            json.dumps({"id": f"x-{i}", "title": f"A fairly long bead title number {i}",
                        "status": "in_progress"})
            for i in range(30)
        ), encoding="utf-8")
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    journal.write_text("\n".join(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": "A decision. " * 40})
        for _ in range(20)
    ), encoding="utf-8")

    rendered = render_handoff(build_handoff(repo))

    assert len(rendered.split()) < 400, f"{len(rendered.split())} words"


def test_secrets_are_redacted_from_the_handoff(tmp_path):
    """E8-R5 via E0-R7's single write path."""
    repo = make_repo(tmp_path)
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    journal.write_text(json.dumps({
        "at": "2026-10-06T10:00:00+00:00",
        "note": "Set token ghp_0123456789abcdefghijklmnopqrstuvwxyz in the config.",
    }) + "\n", encoding="utf-8")

    write_handoff(repo)

    written = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "ghp_" not in written
    assert "REDACTED" in written


def test_writing_rolls_the_previous_handoff_into_history(tmp_path):
    repo = make_repo(tmp_path)
    latest = repo / ".curamoir" / "handoff" / "latest.md"
    latest.write_text("# an older handoff\n", encoding="utf-8")

    write_handoff(repo)

    history = list((repo / ".curamoir" / "handoff" / "history").glob("*.md"))
    assert len(history) == 1
    assert "an older handoff" in history[0].read_text(encoding="utf-8")


def test_history_is_pruned_to_the_configured_limit(tmp_path):
    repo = make_repo(tmp_path)
    history = repo / ".curamoir" / "handoff" / "history"
    base = datetime.now(timezone.utc)
    for index in range(12):
        stamp = (base - timedelta(days=index)).strftime("%Y-%m-%dT%H-%M-%S")
        history.joinpath(f"{stamp}.md").write_text(f"# old {index}\n", encoding="utf-8")

    write_handoff(repo, keep_history=5)

    remaining = sorted(path.name for path in history.glob("*.md"))
    assert len(remaining) == 5, remaining


def test_writing_never_raises_even_outside_a_git_repository(tmp_path):
    """E8-R2: the write must degrade, never blow up. A hook that raises adds
    noise to every turn and, because hooks fail open, achieves nothing."""
    not_a_repo = tmp_path / "plain"
    (not_a_repo / ".curamoir" / "handoff").mkdir(parents=True)

    path = write_handoff(not_a_repo)

    assert path.is_file()
    assert "unavailable" in path.read_text(encoding="utf-8").lower()


# --- E8-R2: graceful degradation ------------------------------------------

def test_a_failing_condenser_still_produces_a_handoff_with_git_and_bead_state(tmp_path):
    """E8-R2's acceptance criterion, exactly: "Simulated model failure still
    produces a handoff with git and bead state"."""
    repo = make_repo(tmp_path)
    beads = repo / ".beads"
    beads.mkdir()
    beads.joinpath("issues.jsonl").write_text(
        json.dumps({"id": "x-9", "title": "Half-done refactor", "status": "in_progress"}) + "\n",
        encoding="utf-8")
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    journal.write_text(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": "Picked the lock design."}) + "\n",
        encoding="utf-8")

    def exploding_condenser(_notes):
        raise RuntimeError("model call failed")

    handoff = build_handoff(repo, condense=exploding_condenser)
    rendered = render_handoff(handoff)

    assert "main" in rendered
    assert "x-9" in rendered
    assert "Picked the lock design." in rendered


def test_a_slow_condenser_does_not_hold_up_the_handoff(tmp_path):
    """Non-blocking is the other half of E8-R2. A hook that waits on a hung model
    call teaches the developer to kill the session, and then there is never a
    handoff at all."""
    import time as clock

    repo = make_repo(tmp_path)
    (repo / ".curamoir" / "handoff" / "journal.jsonl").write_text(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": "A decision."}) + "\n",
        encoding="utf-8")

    def slow_condenser(_notes):
        clock.sleep(10)
        return "never arrives"

    started = clock.monotonic()
    handoff = build_handoff(repo, condense=slow_condenser, condense_timeout=0.2)
    elapsed = clock.monotonic() - started

    assert elapsed < 3.0, f"waited {elapsed:.2f}s on a hung condenser"
    assert "A decision." in handoff["last_decision"]


def test_a_working_condenser_is_used_when_it_succeeds(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".curamoir" / "handoff" / "journal.jsonl").write_text(
        "\n".join(
            json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": f"Step {i}."})
            for i in range(20)
        ), encoding="utf-8")

    handoff = build_handoff(repo, condense=lambda notes: f"Condensed {len(notes)} notes.")

    assert handoff["last_decision"] == "Condensed 20 notes."


def test_no_condenser_is_attempted_for_a_short_journal(tmp_path):
    """A single note needs no condensing, and a model call to restate one
    sentence is pure waste."""
    repo = make_repo(tmp_path)
    (repo / ".curamoir" / "handoff" / "journal.jsonl").write_text(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": "The one decision."}) + "\n",
        encoding="utf-8")
    calls = []

    build_handoff(repo, condense=lambda notes: calls.append(notes) or "condensed")

    assert calls == [], "condenser called for a journal that did not need it"
