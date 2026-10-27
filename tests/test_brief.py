"""E9-R1..R5: the away-brief.

"The artefact that makes the plugin legible to its audience." It is the thing a
developer reads after three weeks away, and §22 ships it in M1 — deliberately
before sweeps, reviews, grades or spend data exist. So every section must render
honestly when its source epic has not landed, rather than erroring or implying a
zero.

Section 6 is the whole point: exactly ONE suggested first task.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from curamoir.brief import build_brief, due_for_brief, render_brief

SECTION_TITLES = [
    "Since you were here",
    "What changed",
    "What was found",
    "Grades",
    "Blocked on you",
    "Start here",
]


def prepared(tmp_path, handoff_age_days=None, beads=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    (repo / ".curamoir" / "handoff").mkdir(parents=True)
    (repo / ".curamoir" / "briefs").mkdir(parents=True)
    (repo / ".curamoir" / "state").mkdir(parents=True)
    if handoff_age_days is not None:
        handoff = repo / ".curamoir" / "handoff" / "latest.md"
        handoff.write_text("# old handoff\n", encoding="utf-8")
        import os
        when = (datetime.now(timezone.utc) - timedelta(days=handoff_age_days)).timestamp()
        os.utime(handoff, (when, when))
    if beads is not None:
        (repo / ".beads").mkdir(exist_ok=True)
        (repo / ".beads" / "issues.jsonl").write_text(
            "\n".join(json.dumps(b) for b in beads), encoding="utf-8")
    return repo


# --- E9-R1: the trigger ----------------------------------------------------

def test_a_brief_is_due_after_the_absence_threshold(tmp_path):
    repo = prepared(tmp_path, handoff_age_days=9)

    assert due_for_brief(repo, threshold_days=5) is True


def test_a_brief_is_not_due_during_an_active_stretch(tmp_path):
    repo = prepared(tmp_path, handoff_age_days=1)

    assert due_for_brief(repo, threshold_days=5) is False


def test_a_brief_is_due_when_there_is_no_handoff_at_all(tmp_path):
    """No handoff means either a new install or a very long absence. Both are
    exactly when a brief helps."""
    repo = prepared(tmp_path)

    assert due_for_brief(repo, threshold_days=5) is True


def test_a_weekly_cadence_makes_the_brief_due_regardless_of_absence(tmp_path):
    """cadence.brief accepts `weekly` as well as `on_return` (§6.3), so the
    unconditional mode must actually do something."""
    repo = prepared(tmp_path, handoff_age_days=1)

    assert due_for_brief(repo, threshold_days=5, cadence="weekly") is True


# --- E9-R2: the six sections ----------------------------------------------

def test_all_six_sections_appear_in_order(tmp_path):
    rendered = render_brief(build_brief(prepared(tmp_path, handoff_age_days=14)))

    positions = [rendered.find(title) for title in SECTION_TITLES]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions), "sections out of order"


def test_absence_duration_is_reported(tmp_path):
    brief = build_brief(prepared(tmp_path, handoff_age_days=14))

    assert brief["absence_days"] == 14


def test_sections_with_no_data_yet_say_so_without_implying_zero_activity(tmp_path):
    """M1 ships this before sweeps and reviews exist. "0 findings" would be a
    lie; "not available yet" is the truth."""
    rendered = render_brief(build_brief(prepared(tmp_path, handoff_age_days=7)))

    assert "not available yet" in rendered.lower()


def test_section_six_suggests_exactly_one_task(tmp_path):
    repo = prepared(tmp_path, handoff_age_days=7, beads=[
        {"id": "b-1", "title": "Big risky refactor", "status": "open", "priority": 1, "estimate": 480},
        {"id": "b-2", "title": "Fix the pagination off-by-one", "status": "open", "priority": 1, "estimate": 45},
        {"id": "b-3", "title": "Tidy the docs", "status": "open", "priority": 3, "estimate": 30},
    ])

    brief = build_brief(repo)

    assert brief["start_here"]["id"] == "b-2", brief["start_here"]
    assert brief["start_here"]["reason"]


def test_section_six_rejects_anything_over_ninety_minutes(tmp_path):
    repo = prepared(tmp_path, handoff_age_days=7, beads=[
        {"id": "b-1", "title": "Too big", "status": "open", "priority": 0, "estimate": 240},
    ])

    brief = build_brief(repo)

    assert brief["start_here"] is None


def test_section_six_prefers_priority_then_smallness(tmp_path):
    repo = prepared(tmp_path, handoff_age_days=7, beads=[
        {"id": "b-1", "title": "P2 and tiny", "status": "open", "priority": 2, "estimate": 15},
        {"id": "b-2", "title": "P0 and small", "status": "open", "priority": 0, "estimate": 60},
        {"id": "b-3", "title": "P0 and smaller", "status": "open", "priority": 0, "estimate": 30},
    ])

    brief = build_brief(repo)

    assert brief["start_here"]["id"] == "b-3"


def test_in_progress_beads_are_not_offered_as_a_first_task(tmp_path):
    """"a ready bead" — something already claimed is not a fresh start."""
    repo = prepared(tmp_path, handoff_age_days=7, beads=[
        {"id": "b-1", "title": "Already going", "status": "in_progress", "priority": 0, "estimate": 30},
    ])

    assert build_brief(repo)["start_here"] is None


def test_with_no_ready_beads_section_six_suggests_a_review_instead(tmp_path):
    """E9-R2's own acceptance criterion: "With no ready beads, section 6 says so
    and suggests running /curamoir:review on the lowest-graded module instead"."""
    repo = prepared(tmp_path, handoff_age_days=7, beads=[])
    (repo / ".curamoir" / "state" / "grades.json").write_text(json.dumps(
        {"modules": {"src/a": {"grade": "B"}, "src/b": {"grade": "D"}}}), encoding="utf-8")

    rendered = render_brief(build_brief(repo))

    assert "/curamoir:review" in rendered
    assert "src/b" in rendered, "did not name the lowest-graded module"


# --- E9-R4: the word cap --------------------------------------------------

def test_the_brief_stays_under_six_hundred_words(tmp_path):
    repo = prepared(tmp_path, handoff_age_days=30, beads=[
        {"id": f"b-{i}", "title": f"A reasonably wordy bead title number {i}",
         "status": "open", "priority": 1, "estimate": 60}
        for i in range(60)
    ])
    (repo / ".curamoir" / "state" / "grades.json").write_text(json.dumps(
        {"modules": {f"src/m{i}": {"grade": "C", "score": 70} for i in range(40)}}),
        encoding="utf-8")

    rendered = render_brief(build_brief(repo))

    assert len(rendered.split()) < 600, f"{len(rendered.split())} words"


def test_going_over_the_cap_links_out_rather_than_truncating_mid_section(tmp_path):
    """Beads cannot inflate the brief — section 6 shows exactly one task, which is
    a structural bound. The grades table is the part that grows with the repo, so
    that is what has to be capped."""
    repo = prepared(tmp_path, handoff_age_days=30)
    (repo / ".curamoir" / "state" / "grades.json").write_text(json.dumps(
        {"modules": {
            f"src/package_with_a_long_name_{i}/submodule_{i}": {"grade": "C", "score": 70}
            for i in range(200)
        }}), encoding="utf-8")

    rendered = render_brief(build_brief(repo))

    assert len(rendered.split()) < 600, f"{len(rendered.split())} words"
    assert "/curamoir:status" in rendered, "no pointer to the full state"
    assert not rendered.rstrip().endswith("|"), "truncated mid-table"


# --- E9-R5: readable without the plugin ----------------------------------

def test_the_brief_is_plain_markdown_with_no_plugin_only_links(tmp_path):
    rendered = render_brief(build_brief(prepared(tmp_path, handoff_age_days=7)))

    assert "](/curamoir:" not in rendered, "a slash command was rendered as a link"
    assert "claude://" not in rendered
    assert "<" not in rendered or "](" not in rendered.split("<")[0]


# --- E9-R3: writing and the optional issue --------------------------------

def test_the_brief_is_written_to_the_documented_path(tmp_path):
    from curamoir.brief import write_brief

    repo = prepared(tmp_path, handoff_age_days=7)

    path = write_brief(repo)

    assert path == repo / ".curamoir" / "briefs" / "latest.md"
    assert "cúramóir brief" in path.read_text(encoding="utf-8")


def test_the_brief_is_redacted_on_the_way_out(tmp_path):
    """It may become a PUBLIC GitHub issue, so this is the artefact where a leak
    costs most."""
    from curamoir.brief import write_brief

    repo = prepared(tmp_path, handoff_age_days=7, beads=[
        {"id": "b-1", "title": "Rotate ghp_0123456789abcdefghijklmnopqrstuvwxyz",
         "status": "open", "priority": 0, "estimate": 30},
    ])

    text = write_brief(repo).read_text(encoding="utf-8")

    assert "ghp_0123456789" not in text
    assert "REDACTED" in text


def test_no_issue_is_posted_unless_it_is_asked_for(tmp_path):
    """Posting is optional (config), and posting by default would publish to a
    repo the developer has not consented to publish to."""
    from curamoir.brief import write_brief

    posted = []
    write_brief(prepared(tmp_path, handoff_age_days=7), post_issue=lambda t, b: posted.append(t))

    assert posted == []


def test_an_issue_is_posted_when_enabled_with_the_dated_title(tmp_path):
    from curamoir.brief import write_brief

    posted = []
    write_brief(
        prepared(tmp_path, handoff_age_days=7),
        post_issue=lambda title, body: posted.append((title, body)),
        post=True,
    )

    assert len(posted) == 1
    title, body = posted[0]
    assert title.startswith("cúramóir brief — ")
    assert "Start here" in body


def test_a_failed_issue_post_does_not_lose_the_written_brief(tmp_path):
    """The file is the deliverable; the issue is a convenience. Losing the former
    because the latter failed would be the wrong way round."""
    from curamoir.brief import write_brief

    def exploding(_title, _body):
        raise RuntimeError("gh unreachable")

    path = write_brief(prepared(tmp_path, handoff_age_days=7), post_issue=exploding, post=True)

    assert path.is_file()
    assert "cúramóir brief" in path.read_text(encoding="utf-8")


def test_a_repo_with_no_handoff_does_not_claim_an_absurd_absence(tmp_path):
    """The sentinel for "no handoff" was 10000 and it was rendered verbatim, so
    the brief said "Away 10000 day(s)" — a falsehood on the face of the artefact
    most likely to be read by a stranger (E9-R5)."""
    rendered = render_brief(build_brief(prepared(tmp_path)))

    assert "10000" not in rendered
    assert "unknown" in rendered.lower() or "no previous" in rendered.lower()


def test_the_brief_threshold_and_cadence_come_from_config(tmp_path):
    """E9-R1's threshold and `cadence.brief` were only arguments to a Python
    function; nothing read the config, so the documented `weekly` mode was
    unreachable in practice."""
    from curamoir.brief import due_for_brief_from_config

    repo = prepared(tmp_path, handoff_age_days=1)
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        "version: 1\ncadence:\n  brief: weekly\n", encoding="utf-8")

    assert due_for_brief_from_config(repo) is True


# --- boundary behaviours whose only other record is a code comment --------

def test_an_unrecognised_grade_does_not_masquerade_as_the_lowest(tmp_path):
    """brief.py's comment records the bug in full: ranking an unknown grade 9
    with reverse=True reported a typo'd grade as the lowest-graded module, ahead
    of a genuine F, so the brief sent the developer to the wrong place. The
    comment was the only thing stopping it coming back."""
    repo = prepared(tmp_path, handoff_age_days=7, beads=[])
    (repo / ".curamoir" / "state" / "grades.json").write_text(json.dumps(
        {"modules": {"src/real": {"grade": "F"}, "src/typo": {"grade": "Bee"}}}),
        encoding="utf-8")

    brief = build_brief(repo)

    assert brief["lowest_graded"] == "src/real"
    assert "src/real" in render_brief(brief)


def test_all_unknown_grades_name_no_module_rather_than_a_random_one(tmp_path):
    repo = prepared(tmp_path, handoff_age_days=7, beads=[])
    (repo / ".curamoir" / "state" / "grades.json").write_text(json.dumps(
        {"modules": {"src/a": {"grade": "?"}, "src/b": {}}}), encoding="utf-8")

    assert build_brief(repo)["lowest_graded"] is None


def test_the_absence_threshold_boundary_is_inclusive(tmp_path):
    """E9-R1's default is 5 days. No test used absence == threshold, so the
    suite could not tell `>=` from `>` — "5 days away" from "6 days away"."""
    at_threshold = prepared(tmp_path / "at", handoff_age_days=5)
    under = prepared(tmp_path / "under", handoff_age_days=4)

    assert due_for_brief(at_threshold, threshold_days=5) is True
    assert due_for_brief(under, threshold_days=5) is False


# --- E9-R2 §2: what changed (cm-fkd.9) ----------------------------------------

SWEEP_BASE = "c" * 40


def sweep_line(number, opened_days_ago, fixes=(("P1", "a.py"),), outcome="success"):
    """A sweep job line as the runner writes it once its PR is open."""
    at = datetime.now(timezone.utc) - timedelta(days=opened_days_ago)
    line = {"kind": "job", "job": "sweep", "outcome": outcome, "at": at.isoformat(),
            "pr": {"number": number, "branch": f"curamoir/sweep-x-{number}",
                   "url": f"https://github.com/example/repo/pull/{number}",
                   "base": SWEEP_BASE, "paths": ["a.py"]}}
    if fixes:
        line["fixes"] = [{"principle_id": principle, "path": path} for principle, path in fixes]
    return line


def with_ledger(repo, *lines):
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return repo


def states(*items):
    """A pr_states callable answering `items` of (number, state, merged days ago)."""
    def answer():
        return {number: {"branch": f"curamoir/sweep-x-{number}", "state": state,
                         "merged_at": None if merged is None
                         else datetime.now(timezone.utc) - timedelta(days=merged)}
                for number, state, merged in items}
    return answer


def changed(repo, pr_states=None):
    rendered = render_brief(build_brief(repo, pr_states=pr_states))
    return rendered.split("## What changed", 1)[1].split("##", 1)[0].strip()


def test_an_open_sweep_pr_gets_one_line_with_its_fixes_and_link(tmp_path):
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10), sweep_line(
        12, 3, fixes=[("P1", "a.py"), ("P1", "b.py"), ("P2", "c.py")]))

    section = changed(repo, states((12, "OPEN", None)))

    assert section == ("- #12, open: 3 fixes of 2 principles, opened "
                       f"{(datetime.now(timezone.utc) - timedelta(days=3)).date()}. "
                       "https://github.com/example/repo/pull/12")


def test_a_merged_sweep_pr_says_merged(tmp_path):
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10), sweep_line(12, 3))

    assert "#12, merged: 1 fix of 1 principle" in changed(repo, states((12, "MERGED", 1)))


def test_a_sweep_pr_closed_without_merging_changed_nothing(tmp_path):
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10), sweep_line(12, 3))

    assert changed(repo, states((12, "CLOSED", None))) == "No sweep PRs since you were here."


def test_with_no_sweep_prs_the_section_says_so(tmp_path):
    """Not "not available yet" forever: the sweep exists now."""
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10),
                       {"kind": "job", "job": "sweep", "outcome": "success",
                        "at": datetime.now(timezone.utc).isoformat(), "work": "no_work"})

    assert changed(repo, states()) == "No sweep PRs since you were here."


def test_with_no_ledger_at_all_the_section_says_so(tmp_path):
    assert changed(prepared(tmp_path, handoff_age_days=10)) == (
        "No sweep PRs since you were here.")


def test_gh_is_not_asked_when_the_ledger_has_no_sweep_pr(tmp_path):
    asked = []
    repo = prepared(tmp_path, handoff_age_days=10)

    changed(repo, lambda: asked.append(1) or {})

    assert asked == []


def test_gh_is_asked_once_however_many_prs(tmp_path):
    asked = []
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10),
                       sweep_line(12, 3), sweep_line(13, 2), sweep_line(14, 1))

    changed(repo, lambda: asked.append(1) or {})

    assert asked == [1]


@pytest.mark.parametrize("failure", [RuntimeError("gh exploded"), OSError("no gh")])
def test_when_gh_fails_the_prs_are_still_listed_with_their_state_unknown(tmp_path, failure):
    """The brief is the deliverable; a gh failure never costs it."""
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10), sweep_line(12, 3))

    def broken():
        raise failure

    assert "#12, state unknown: 1 fix of 1 principle" in changed(repo, broken)


def test_without_a_state_lookup_the_state_is_unknown(tmp_path):
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10), sweep_line(12, 3))

    assert "#12, state unknown" in changed(repo)


def test_a_pr_gh_does_not_list_or_lists_on_another_branch_has_an_unknown_state(tmp_path):
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10),
                       sweep_line(12, 3), sweep_line(13, 2))

    def answer():
        return {13: {"branch": "someone-else", "state": "MERGED", "merged_at": None}}

    section = changed(repo, answer)

    assert "#12, state unknown" in section and "#13, state unknown" in section


def test_only_prs_from_this_absence_are_listed_unless_still_open_or_merged_during_it(tmp_path):
    repo = with_ledger(
        prepared(tmp_path, handoff_age_days=10),
        sweep_line(1, 30),   # before, merged before: old news
        sweep_line(2, 30),   # before, still open: still waiting
        sweep_line(3, 30),   # before, merged during the absence
        sweep_line(4, 30),   # before, state unknown: cannot say it changed
        sweep_line(5, 3),    # during, state unknown
    )

    section = changed(repo, states((1, "MERGED", 20), (2, "OPEN", None), (3, "MERGED", 4)))

    listed = [line.split(",", 1)[0] for line in section.splitlines()]
    assert listed == ["- #2", "- #3", "- #5"]


def test_with_no_handoff_every_sweep_pr_is_listed(tmp_path):
    repo = with_ledger(prepared(tmp_path), sweep_line(1, 300), sweep_line(2, 3))

    section = changed(repo, states((1, "MERGED", 200), (2, "OPEN", None)))

    assert [line.split(",", 1)[0] for line in section.splitlines()] == ["- #1", "- #2"]


def test_a_pr_recorded_on_several_lines_is_one_line_with_the_fixes_counted_once(tmp_path):
    """A failure line written after the PR opened carries the same pr and no
    fixes; the PR is still one PR."""
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10),
                       sweep_line(12, 3, fixes=[("P1", "a.py"), ("P2", "b.py")]),
                       sweep_line(12, 3, fixes=(), outcome="failure"))

    section = changed(repo, states((12, "OPEN", None)))

    assert section.count("#12") == 1 and "2 fixes of 2 principles" in section


def test_a_pr_line_without_fixes_says_so_rather_than_zero(tmp_path):
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10),
                       sweep_line(12, 3, fixes=(), outcome="failure"))

    section = changed(repo, states((12, "OPEN", None)))

    assert section.startswith("- #12, open: fixes not recorded, opened ")


def test_malformed_pr_records_are_skipped_not_fatal(tmp_path):
    repo = with_ledger(prepared(tmp_path, handoff_age_days=10),
                       {"kind": "job", "job": "sweep", "at": "x", "pr": {"number": "12"}},
                       {"kind": "job", "job": "sweep", "at": "x", "pr": "nonsense"},
                       sweep_line(13, 2))

    assert [line.split(",", 1)[0] for line in changed(repo, states()).splitlines()] == ["- #13"]
