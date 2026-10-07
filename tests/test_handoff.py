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
#
# DEC-8 (b) deleted the condenser seam, so there is no model call left to fail.
# The degradation requirement is now satisfied by CONSTRUCTION rather than by a
# fallback branch: the handoff is built from git and bead state, which is the
# only path. The non-blocking and never-raises halves stay tested below.





# --- E8-R4: the manual write ----------------------------------------------

def test_a_manual_note_is_recorded_and_appears_in_the_handoff(tmp_path):
    """E8-R4. A deliberate handoff beats an extracted one, because the developer
    states the next step rather than the model inferring it — so the note must be
    prominent, not buried."""
    repo = make_repo(tmp_path)

    write_handoff(repo, note="Stopped mid-refactor; the loader tests are red on purpose.")

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "Stopped mid-refactor" in text
    assert text.index("Stopped mid-refactor") < text.index("branch"), (
        "a deliberate note must lead, not trail the automatic state"
    )


def test_a_manual_note_is_also_journalled_so_it_survives_the_next_write(tmp_path):
    repo = make_repo(tmp_path)

    write_handoff(repo, note="Chose the thread-based timeout.")
    write_handoff(repo)

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "thread-based timeout" in text


def test_writing_without_a_note_still_works(tmp_path):
    repo = make_repo(tmp_path)

    path = write_handoff(repo)

    assert path.is_file()


# --- E8-R5: no secrets, no raw diffs --------------------------------------

def test_a_secret_in_a_manual_note_is_redacted(tmp_path):
    repo = make_repo(tmp_path)

    write_handoff(repo, note="Used key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA for the test.")

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "sk-ant-" not in text
    assert "REDACTED" in text


def test_a_secret_in_a_changed_filename_is_redacted(tmp_path):
    """Paths reach the handoff from git status, and a filename can carry a token."""
    repo = make_repo(tmp_path)
    (repo / "ghp_0123456789abcdefghijklmnopqrstuvwxyz.txt").write_text("x", encoding="utf-8")

    write_handoff(repo)

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "ghp_0123456789" not in text


def test_no_file_contents_reach_the_handoff_however_large_the_change(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "app.py").write_text("DISTINCTIVE_MARKER = 'leaked'\n" * 50, encoding="utf-8")

    write_handoff(repo)

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "DISTINCTIVE_MARKER" not in text
    assert "leaked" not in text


def test_the_branch_is_reported_on_a_repo_with_no_commits_yet(tmp_path):
    """A fresh repo before its first commit has a branch, but `rev-parse HEAD`
    fails on the unborn ref. Reporting "unavailable" there is misleading: the
    branch is knowable, and this is a normal state for a repo someone has just
    run /curamoir:init in."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    (repo / ".curamoir" / "handoff").mkdir(parents=True)

    handoff = build_handoff(repo)

    assert handoff["branch"] == "main"


# --- E8-R1: the intended next step (spec-compliance CRITICAL) --------------

def test_the_handoff_always_states_an_intended_next_step(tmp_path):
    """E8-R1 lists the intended next step among the six required contents.

    It was previously unreachable: `_next_step` read a journal key no writer
    ever wrote, so the section never rendered in any trigger path. A required
    field that no code path can produce is worse than a missing one, because the
    reader has no way to tell.
    """
    repo = make_repo(tmp_path)

    rendered = render_handoff(build_handoff(repo))

    assert "Intended next step" in rendered


def test_an_explicit_next_step_is_used_when_given(tmp_path):
    repo = make_repo(tmp_path)

    write_handoff(repo, next_step="Finish the lock acquisition in the runner.")

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "Finish the lock acquisition in the runner." in text


def test_an_explicit_next_step_survives_into_the_following_handoff(tmp_path):
    """Like the note, it is journalled, so the next automatic write does not
    silently drop what the developer took the trouble to state."""
    repo = make_repo(tmp_path)

    write_handoff(repo, next_step="Wire the condenser behind config.")
    write_handoff(repo)

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "Wire the condenser behind config." in text


def test_without_an_explicit_next_step_an_in_progress_bead_is_suggested(tmp_path):
    """The deterministic fallback. A claimed bead is the best available guess at
    what the developer was doing, and it beats an empty section."""
    repo = make_repo(tmp_path)
    beads = repo / ".beads"
    beads.mkdir()
    beads.joinpath("issues.jsonl").write_text(
        json.dumps({"id": "x-7", "title": "Teach doctor about every hook",
                    "status": "in_progress"}) + "\n", encoding="utf-8")

    handoff = build_handoff(repo)

    assert "x-7" in handoff["next_step"]


def test_with_nothing_in_progress_the_next_step_says_so_rather_than_inventing_one(tmp_path):
    repo = make_repo(tmp_path)

    handoff = build_handoff(repo)

    assert handoff["next_step"]
    assert "nothing" in handoff["next_step"].lower() or "no " in handoff["next_step"].lower()


def test_a_very_long_last_assistant_message_is_bounded(tmp_path):
    """E8-R1 says the last decision is "one to three sentences". It was
    previously the whole of last_assistant_message, bounded only by the 400-word
    cap on the WHOLE FILE — so a long final turn would push the later sections
    out of the handoff entirely."""
    repo = make_repo(tmp_path)
    sentences = " ".join(f"Sentence number {i} about the decision." for i in range(40))
    (repo / ".curamoir" / "handoff" / "journal.jsonl").write_text(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": sentences}) + "\n",
        encoding="utf-8")

    handoff = build_handoff(repo)

    assert handoff["last_decision"].count(".") <= 4, handoff["last_decision"]
    rendered = render_handoff(handoff)
    assert "Intended next step" in rendered, "later sections pushed out by a long decision"


def test_an_unpunctuated_last_message_is_still_bounded(tmp_path):
    """`_first_sentences` split on sentence-ending punctuation, so prose with no
    terminal punctuation — bullet lists, code blocks, a trailing colon, which is
    what `last_assistant_message` usually is — counted as ONE sentence and passed
    through at full length. The whole-file cap then dropped the sections after it.
    """
    repo = make_repo(tmp_path)
    unpunctuated = " ".join(f"word{i}" for i in range(600))
    (repo / ".curamoir" / "handoff" / "journal.jsonl").write_text(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": unpunctuated}) + "\n",
        encoding="utf-8")

    handoff = build_handoff(repo)

    assert len(handoff["last_decision"].split()) < 80, len(handoff["last_decision"].split())


def test_the_next_step_survives_an_over_long_last_decision(tmp_path):
    """The intended next step is the most actionable field in the handoff, so it
    must not be the one truncation discards."""
    repo = make_repo(tmp_path)
    (repo / ".curamoir" / "handoff" / "journal.jsonl").write_text(
        json.dumps({"at": "2026-10-06T10:00:00+00:00",
                    "note": " ".join(f"word{i}" for i in range(2000))}) + "\n",
        encoding="utf-8")

    rendered = render_handoff(build_handoff(repo))

    assert "Intended next step" in rendered
