"""E3-R7 (cm-fkd.8): two consecutive sweeps with no intervening changes
produce no second PR, and a second run on the merged result opens nothing
and logs `no_work`.

**Fixture provenance.** The decision-rule tests build work lists by hand
from ReportedViolation (the lint report's own type) and seed ledger lines in
the shape JobRun writes (tests/test_job_run.py pins it): the rules are about
which lines and which open branches there are, and a seeded ledger is what
"the previous sweep" is. The end-to-end tests run on `fixtures/sample-repo`
committed with tests/test_lint.py's regex rules (tests/conftest.py,
committed_sample): its five planted violations, fixed by the same edits
tests/test_fix_counts.py makes (standing in for the fix agent, which no test
runs), with the real `scripts/curamoir-lint` at every base, real git, and
the test gate's command `true`. "Merged" is the gate's own patch applied to
the source repository with `git apply` and committed; "open" is the
injected open-PR list holding sweep 1's branch, which is what a `gh` query
would return while the PR is open.
"""

import subprocess
from datetime import datetime

import pytest

from curamoir.fix_counts import (
    fix_counts,
    found_delta,
    found_set,
    ledger_fields,
    read_snapshot,
    record_fixes,
    snapshot_for,
    snapshot_path,
    write_snapshot_or_warn,
)
from curamoir.fix_scope import FILE_NOT_IN_WORK_LIST, lint_sandbox, verify_fix
from curamoir.git_guard import prepare_sandbox, snapshot_git_state
from curamoir.green_gate import run_test_gate
from curamoir.guards.pr_cap import OpenPrCap
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal
from curamoir.idempotence import (
    DUPLICATE_OPEN_PR,
    NO_WORK,
    PR_FIELD,
    SWEEP_BRANCH_PREFIX,
    UNKNOWN,
    WORK,
    WORK_FIELD,
    parse_sweep_marker,
    pr_field,
    sweep_decision,
    sweep_marker,
    sweep_pr_listing,
    sweep_prs,
)
from curamoir.job_run import JobRun
from curamoir.ledger import Ledger
from curamoir.lint_report import LintReport, ReportedViolation
from curamoir.remedies import apply_file_remedies
from curamoir.sweep import WorkList
from tests.conftest import committed_sample
from tests.test_fix_counts import (
    BILLING,
    BILLING_PRINT,
    BILLING_RAISE,
    HANDLERS,
    HANDLERS_PRINT,
    HANDLERS_RAISE,
    VARIANT,
    edit,
)

BASE = "a" * 40
NEWER = "b" * 40
BRANCH = SWEEP_BRANCH_PREFIX + "2026-10-05"
URL = "https://github.invalid/o/r/pull/7"


def violation(fingerprint, path="src/a.py", rule_id="no-print", principle="logging"):
    return ReportedViolation(
        path=path, line=1, col=1, end_line=1, end_col=2, severity="error",
        rule_id=rule_id, principle_id=principle, principle_rule="", message="",
        fix="", fix_source="rule", why_link="", match="print()", fingerprint=fingerprint)


def work_of(*items):
    report = LintReport(schema_version=1, tools={}, fail_on="error", complete=True,
                        rules_checked=("no-print",), rules_skipped=(), notes=(),
                        violations=tuple(items))
    return WorkList(items=tuple(items), report=report)


def pr_line(number=7, branch=BRANCH, base=BASE, paths=("src/a.py",), outcome="success",
            job="sweep", **extra):
    line = {"kind": "job", "job": job, "outcome": outcome, "reason": None, "message": "",
            "run_id": f"run{number}", "at": "2026-10-05T03:00:00+00:00",
            WORK_FIELD: WORK,
            PR_FIELD: pr_field(number, branch, URL, base, paths)}
    line.update(extra)
    return line


def never_called():
    raise AssertionError("the open-PR list was asked for when it did not matter")


# --- the convention --------------------------------------------------------------------

def test_the_ledger_vocabulary_is_one_set_of_strings():
    assert (WORK_FIELD, PR_FIELD) == ("work", "pr")
    assert (WORK, NO_WORK, DUPLICATE_OPEN_PR) == ("work", "no_work", "duplicate_open_pr")
    assert SWEEP_BRANCH_PREFIX == "curamoir/sweep-"
    assert Reason.IDEMPOTENCE == "idempotence"


def test_the_pr_field_holds_its_shape():
    assert pr_field(3, BRANCH, URL, BASE, ["src/b.py", "src/a.py"]) == {
        "number": 3, "branch": BRANCH, "url": URL, "base": BASE,
        "paths": ["src/a.py", "src/b.py"]}
    for bad in ((0, BRANCH, URL, BASE, ["a"]), (True, BRANCH, URL, BASE, ["a"]),
                (3, "", URL, BASE, ["a"]), (3, BRANCH, URL, "HEAD", ["a"]),
                (3, BRANCH, URL, BASE, []), (3, BRANCH, URL, BASE, [""]),
                (3, BRANCH, 5, BASE, ["a"])):
        with pytest.raises(ValueError):
            pr_field(*bad)


# --- nothing to fix (AC 1) --------------------------------------------------------------

def test_an_empty_work_list_is_no_work_without_asking_for_open_prs():
    decision = sweep_decision(work_of(), BASE, [pr_line()], never_called)
    assert decision.kind == NO_WORK and decision.terminal
    assert decision.ledger_fields() == {"work": "no_work"}


# --- the previous PR still open ---------------------------------------------------------

def test_an_open_sweep_pr_at_the_same_base_is_a_duplicate_whatever_it_left():
    """No intervening changes: the base is the one the open PR was made from.
    Its leftovers (a cap or a red test dropped them) wait for it, so the
    second sweep opens nothing even for a file the first PR does not touch."""
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/left.py"))
    decision = sweep_decision(work, BASE, [pr_line()], lambda: [BRANCH, "feature/x"])
    assert decision.kind == DUPLICATE_OPEN_PR and decision.terminal
    fields = decision.ledger_fields()
    assert fields["work"] == "duplicate_open_pr"
    assert fields["open_sweep_prs"] == [{"number": 7, "branch": BRANCH, "url": URL}]
    assert "#7" in decision.message


def test_an_open_pr_at_an_older_base_covering_every_file_is_a_duplicate():
    """Fingerprints do not depend on line numbers, so an unchanged file's
    violations are the same at the newer base; the decision goes by path,
    which the open PR's fixes are all within."""
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/b.py"))
    decision = sweep_decision(work, NEWER, [pr_line(paths=("src/a.py", "src/b.py"))],
                              lambda: [BRANCH])
    assert decision.kind == DUPLICATE_OPEN_PR
    assert decision.ledger_fields()["held_for_open_prs"] == 2


def test_partial_overlap_at_a_newer_base_proceeds_with_only_the_files_no_open_pr_touches():
    a1, a2, b1 = (violation("f1", "src/a.py"), violation("f2", "src/a.py", rule_id="r2"),
                  violation("f3", "src/b.py"))
    work = work_of(a1, a2, b1)
    decision = sweep_decision(work, NEWER, [pr_line(paths=("src/a.py",))], lambda: [BRANCH])
    assert decision.kind == WORK and not decision.terminal
    assert decision.work_list.items == (b1,)
    assert decision.work_list.report is work.report
    assert decision.held == (a1, a2)
    assert decision.ledger_fields() == {
        "work": "work", "held_for_open_prs": 2,
        "open_sweep_prs": [{"number": 7, "branch": BRANCH, "url": URL}]}


def test_two_open_sweep_prs_both_hold_their_files():
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/b.py"),
                   violation("f3", "src/c.py"))
    other = SWEEP_BRANCH_PREFIX + "2026-10-12"
    entries = [pr_line(7, paths=("src/a.py",)),
               pr_line(8, branch=other, base="c" * 40, paths=("src/b.py",))]
    decision = sweep_decision(work, NEWER, entries, lambda: [other, BRANCH])
    assert decision.kind == WORK
    assert [item.path for item in decision.work_list.items] == ["src/c.py"]
    assert [pr["number"] for pr in decision.ledger_fields()["open_sweep_prs"]] == [7, 8]


# --- the previous PR merged or closed ---------------------------------------------------

def test_a_pr_no_longer_open_frees_its_files_merged_or_closed():
    """`gh pr list` lists open PRs only, so merged and closed look alike here.
    Merged: the base now holds the fixes and the lint no longer reports them,
    so whatever is still listed is new. Closed unmerged: its fixes were never
    applied, and are free to be proposed again."""
    work = work_of(violation("f1", "src/a.py"))
    decision = sweep_decision(work, BASE, [pr_line()], lambda: [])
    assert decision.kind == WORK
    assert decision.work_list.items == work.items
    assert decision.ledger_fields() == {"work": "work", "held_for_open_prs": 0,
                                        "open_sweep_prs": []}


def test_with_no_sweep_pr_ever_and_none_open_the_whole_list_proceeds():
    work = work_of(violation("f1"))
    decision = sweep_decision(work, BASE, [], lambda: ["feature/x", "curamoir/promote-y"])
    assert decision.kind == WORK and decision.work_list.items == work.items


# --- unknown state: fail safe -----------------------------------------------------------

@pytest.mark.parametrize("listing", [
    lambda: (_ for _ in ()).throw(OSError("gh: command not found")),
    lambda: "curamoir/sweep-x",
    lambda: [BRANCH, 5],
])
def test_open_prs_that_cannot_be_listed_refuse_rather_than_risk_a_duplicate(listing):
    decision = sweep_decision(work_of(violation("f1")), BASE, [pr_line()], listing)
    assert decision.kind == UNKNOWN and decision.terminal
    refusal = decision.refusal()
    assert refusal.reason == Reason.IDEMPOTENCE
    assert "duplicate" in refusal.message
    with pytest.raises(ValueError):
        decision.ledger_fields()


def test_an_open_sweep_pr_the_ledger_has_no_record_of_refuses():
    """A ledger that lost the line (or a checkout that never had it) cannot say
    which files the open PR touches, so no file is known to be free."""
    decision = sweep_decision(work_of(violation("f1")), NEWER,
                              [pr_line(branch=SWEEP_BRANCH_PREFIX + "other")],
                              lambda: [BRANCH])
    assert decision.kind == UNKNOWN
    assert BRANCH in decision.refusal().message


def test_a_non_unknown_decision_has_no_refusal():
    with pytest.raises(ValueError):
        sweep_decision(work_of(), BASE, [], never_called).refusal()


# --- reading the ledger -----------------------------------------------------------------

def test_the_latest_valid_pr_record_per_branch_wins_and_malformed_ones_are_skipped():
    older = pr_line(5, paths=("src/old.py",))
    newer = pr_line(9, paths=("src/new.py",))
    broken = dict(pr_line(10), pr={"number": "ten", "branch": BRANCH})
    noise = [42, {"kind": "guard", "job": "sweep", "pr": newer["pr"]},
             pr_line(11, job="review"), {"kind": "job", "job": "sweep"}]
    prs = sweep_prs([older, *noise, newer])
    assert list(prs) == [BRANCH]
    assert prs[BRANCH]["number"] == 9
    # Latest is ledger order: a malformed newest record does not resurrect #9;
    # the branch's ledger record is unknown (None).
    assert sweep_prs([older, *noise, newer, broken]) == {BRANCH: None}


def test_a_pr_recorded_on_a_failed_line_still_counts_as_opened():
    """A runner that opened the PR and then crashed writes a failure line; the
    PR exists all the same, so its files are held."""
    work = work_of(violation("f1", "src/a.py"))
    decision = sweep_decision(work, NEWER, [pr_line(outcome="failure")], lambda: [BRANCH])
    assert decision.kind == DUPLICATE_OPEN_PR


# --- the ledger lines -------------------------------------------------------------------

def test_terminal_decisions_record_a_success_with_work_or_a_refusal(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    delta = {"base": BASE}
    for decision in (sweep_decision(work_of(), BASE, [], never_called),
                     sweep_decision(work_of(violation("f1")), BASE, [pr_line()],
                                    lambda: [BRANCH]),
                     sweep_decision(work_of(violation("f1")), BASE, [pr_line()],
                                    lambda: 1 / 0)):
        with JobRun(ledger, "sweep") as run:
            decision.record(run, delta)
    no_work, duplicate, unknown = ledger.entries()
    assert (no_work["outcome"], no_work["work"], no_work["found_delta"]) == (
        "success", "no_work", delta)
    assert (duplicate["outcome"], duplicate["work"]) == ("success", "duplicate_open_pr")
    assert duplicate["found_delta"] == delta and "fixes" not in duplicate
    assert (unknown["outcome"], unknown["reason"]) == ("refused", "idempotence")
    assert "found_delta" not in unknown and "work" not in unknown
    # They carry no fixes, so the promotion ladder's reader counts nothing.
    now = datetime.fromisoformat(no_work["at"])
    assert fix_counts(ledger.entries(), now=now, window_days=30) == {}


def test_a_proceed_decision_is_not_recorded_by_itself(tmp_path):
    decision = sweep_decision(work_of(violation("f1")), BASE, [], lambda: [])
    with pytest.raises(ValueError), JobRun(Ledger(tmp_path / "l.jsonl"), "sweep") as run:
        decision.record(run, {"base": BASE})


# --- end to end on the fixture: AC 1 and AC 2 -------------------------------------------

AGENT_EDITS = [BILLING_PRINT, BILLING_RAISE, HANDLERS_PRINT, HANDLERS_RAISE]


def git_in(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def sweep(repo, tmp_path, ledger, open_branches, edits, number, branch):
    """One sweep in fix_scope's runner order, from the lint through the line,
    with `edits` standing in for the fix agent and "opening the PR" being the
    patch kept and the `pr` field built. Returns (line, patch or None, the
    paths the stand-in agent was given or None when it never ran)."""
    agent_given = None
    patch = None
    with JobRun(ledger, "sweep") as run, \
            prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)                                                  # step 2
        current = found_set(sandbox.work_list, sandbox.base)
        delta = found_delta(snapshot_for(repo, sandbox.base,
                                         read_snapshot(snapshot_path(repo))), current)
        decision = sweep_decision(sandbox.work_list, sandbox.base,             # step 2b
                                  ledger.entries(), open_branches)
        if decision.terminal:
            decision.record(run, delta)
        else:
            decision.apply_to(sandbox)
            remedies = apply_file_remedies(sandbox)                            # step 3
            given = remedies.for_agent(sandbox.work_list)
            agent_given = given.paths
            state = snapshot_git_state(sandbox)                                # step 4
            for path, old, new in edits:
                if path in given.paths:
                    edit(sandbox.path, path, old, new)
            verdict = verify_fix(sandbox, git_state=state)                     # step 5
            assert verdict.ok, verdict.render()
            gate = run_test_gate(sandbox, verdict)                             # step 7
            records = record_fixes(sandbox, gate, remedies)                    # step 8
            patch = gate.patch(sandbox)                                        # step 9
            pr = pr_field(number, branch, f"https://github.invalid/pull/{number}",
                          sandbox.base, gate.paths_for_pr)
            run.succeeded(f"opened #{number}", **gate.ledger_fields(),
                          **ledger_fields(records, delta), **decision.ledger_fields(),
                          pr=pr)
        if decision.kind != UNKNOWN:                                           # step 2a
            write_snapshot_or_warn(snapshot_path(repo), current, sandbox=sandbox)
    return ledger.entries()[-1], patch, agent_given


def fixture(tmp_path):
    repo = committed_sample(tmp_path)
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        'version: 1\nsweep:\n  test_command: "true"\n', encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", "test command")
    return repo, Ledger(tmp_path / "ledger.jsonl")


def test_two_consecutive_sweeps_with_nothing_changed_open_no_second_pr(tmp_path):
    """AC 2: sweep 1 opens a PR fixing all five; it stays open, nothing is
    pushed, and sweep 2 on the same base opens nothing, runs no fix step,
    and says which PR it is waiting on."""
    repo, ledger = fixture(tmp_path)
    first, patch, given = sweep(repo, tmp_path, ledger, lambda: [], AGENT_EDITS, 1, BRANCH)
    assert first["outcome"] == "success" and first["work"] == "work"
    assert len(first["fixes"]) == 5 and patch
    assert first["pr"]["paths"] == sorted([BILLING, HANDLERS, VARIANT])
    assert given == (BILLING, HANDLERS)

    second, patch, given = sweep(repo, tmp_path, ledger, lambda: [BRANCH], AGENT_EDITS, 2,
                                 SWEEP_BRANCH_PREFIX + "2026-10-12")
    assert (second["outcome"], second["work"]) == ("success", "duplicate_open_pr")
    assert patch is None and given is None
    assert "pr" not in second and "fixes" not in second
    assert second["open_sweep_prs"] == [{"number": 1, "branch": BRANCH,
                                         "url": "https://github.invalid/pull/1"}]
    # The same base, so the same found set: nothing added, nothing removed.
    assert second["found_delta"]["added_count"] == second["found_delta"]["removed_count"] == 0
    # Only the PR that was opened counts toward promotion.
    now = datetime.fromisoformat(second["at"])
    assert sum(fix_counts(ledger.entries(), now=now, window_days=30).values()) == 5


def test_a_second_sweep_on_the_merged_result_opens_nothing_and_logs_no_work(tmp_path):
    """AC 1: sweep 1's patch is applied and committed (the merge), so its PR
    is no longer open; the lint at the new base finds nothing."""
    repo, ledger = fixture(tmp_path)
    _first, patch, _given = sweep(repo, tmp_path, ledger, lambda: [], AGENT_EDITS, 1, BRANCH)
    (tmp_path / "fix.patch").write_bytes(patch)
    git_in(repo, "apply", "--index", str(tmp_path / "fix.patch"))
    git_in(repo, "commit", "-qm", "Merge curamoir/sweep-2026-10-05")

    second, patch, given = sweep(repo, tmp_path, ledger, lambda: [], AGENT_EDITS, 2,
                                 SWEEP_BRANCH_PREFIX + "2026-10-12")
    assert (second["outcome"], second["work"]) == ("success", "no_work")
    assert patch is None and given is None and "pr" not in second
    assert second["found_delta"]["removed_count"] == 5
    assert second["found_delta"]["added_count"] == 0


def test_after_an_unrelated_change_only_files_the_open_pr_does_not_touch_are_swept(tmp_path):
    """Sweep 1 fixed handlers.py only (the agent skipped billing.py) and its
    PR stays open; then the developer pushes an unrelated commit. Sweep 2
    proceeds with billing.py alone, and the scope check, not the prompt,
    keeps it off handlers.py: an edit there is out of the narrowed work list."""
    repo, ledger = fixture(tmp_path)
    first, _patch, _given = sweep(repo, tmp_path, ledger, lambda: [],
                                  [HANDLERS_PRINT, HANDLERS_RAISE], 1, BRANCH)
    assert BILLING not in first["pr"]["paths"] and HANDLERS in first["pr"]["paths"]
    (repo / "NOTES.md").write_text("unrelated\n", encoding="utf-8")
    git_in(repo, "add", "NOTES.md")
    git_in(repo, "commit", "-qm", "unrelated")

    second, patch, given = sweep(repo, tmp_path, ledger, lambda: [BRANCH],
                                 [BILLING_PRINT, BILLING_RAISE], 2,
                                 SWEEP_BRANCH_PREFIX + "2026-10-12")
    assert given == (BILLING,) and patch
    assert second["work"] == "work" and second["pr"]["paths"] == [BILLING]
    assert second["held_for_open_prs"] == 3  # handlers.py's two, handlers_v2.py's one
    assert {fix["path"] for fix in second["fixes"]} == {BILLING}

    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        sweep_decision(sandbox.work_list, sandbox.base, ledger.entries(),
                       lambda: [BRANCH]).apply_to(sandbox)
        state = snapshot_git_state(sandbox)
        edit(sandbox.path, *HANDLERS_PRINT)
        verdict = verify_fix(sandbox, git_state=state)
    assert not verdict.ok
    assert [(b.kind, b.path) for b in verdict.breaches] == [(FILE_NOT_IN_WORK_LIST, HANDLERS)]


def test_apply_to_refuses_a_sandbox_the_decision_was_not_made_for(tmp_path):
    repo, _ledger = fixture(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        elsewhere = sweep_decision(sandbox.work_list, NEWER, [], lambda: [])
        with pytest.raises(ValueError):
            elsewhere.apply_to(sandbox)
        foreign = sweep_decision(work_of(violation("f1")), sandbox.base, [], lambda: [])
        with pytest.raises(ValueError):
            foreign.apply_to(sandbox)
        terminal = sweep_decision(work_of(), sandbox.base, [], never_called)
        with pytest.raises(ValueError):
            terminal.apply_to(sandbox)


# --- review fixes: CI mode, one open-PR contract, the PR marker ------------------------

def gh_record(branch=BRANCH, number=7, base=BASE, paths=("src/a.py",), url=URL,
              cross_repository=False):
    return {"branch": branch, "number": number, "url": url, "base": base,
            "paths": None if paths is None else list(paths),
            "cross_repository": cross_repository}


def test_ci_mode_without_the_ledger_line_falls_back_to_the_listings_base_and_paths():
    """CI mode: a fresh checkout's ledger lacks sweep 1's line until the state
    PR merges; GitHub's listing (the PR body's marker, its files) says it."""
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/b.py"))
    decision = sweep_decision(work, NEWER, [], lambda: [gh_record()])
    assert decision.kind == WORK
    assert [i.path for i in decision.work_list.items] == ["src/b.py"]
    assert [i.path for i in decision.held] == ["src/a.py"]
    assert decision.ledger_fields()["open_sweep_prs"] == [
        {"number": 7, "branch": BRANCH, "url": URL}]


def test_a_listed_base_alone_is_enough_to_see_a_same_base_duplicate():
    work = work_of(violation("f1", "src/a.py"))
    decision = sweep_decision(work, BASE, [], lambda: [gh_record(paths=None)])
    assert decision.kind == DUPLICATE_OPEN_PR
    assert "same base commit" in decision.message
    assert "leftovers wait for the next base" in decision.message
    assert "nothing has changed" not in decision.message


def test_neither_source_knowing_the_files_refuses_and_says_when_it_clears():
    work = work_of(violation("f1", "src/a.py"))
    for listing in ([BRANCH], [gh_record(paths=None)], [gh_record(base=None)],
                    [gh_record(paths=["ok.py", ""])], [gh_record(base="A" * 40)]):
        decision = sweep_decision(work, NEWER, [], lambda listing=listing: listing)
        message = decision.refusal().message
        assert decision.kind == UNKNOWN, listing
        assert BRANCH in message and "neither the ledger nor GitHub" in message
        assert "merged or closed" in message
        assert "next sweep will try again" not in message
        assert "restore the ledger line" not in message


def test_a_transient_listing_failure_keeps_the_try_again_wording():
    def gh_down():
        raise OSError("gh: command not found")
    decision = sweep_decision(work_of(violation("f1")), NEWER, [], gh_down)
    assert "next sweep will try again" in decision.refusal().message


def test_the_ledger_record_is_primary_over_the_listing():
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/b.py"))
    decision = sweep_decision(work, NEWER, [pr_line(7, paths=("src/a.py",))],
                              lambda: [gh_record(paths=("src/b.py",))])
    assert [i.path for i in decision.held] == ["src/a.py"]


def test_a_ledger_record_for_an_older_pr_on_a_reused_branch_is_not_used():
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/b.py"))
    stale = [pr_line(5, paths=("src/a.py",))]
    assert sweep_decision(work, NEWER, stale,
                          lambda: [gh_record(number=9, base=None)]).kind == UNKNOWN
    decision = sweep_decision(work, NEWER, stale,
                              lambda: [gh_record(number=9, paths=("src/b.py",))])
    assert [i.path for i in decision.held] == ["src/b.py"]


def test_a_malformed_newest_ledger_record_falls_back_to_the_listing_else_refuses():
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/b.py"))
    broken = dict(pr_line(9), pr={"number": 9, "branch": BRANCH, "paths": "src/b.py"})
    entries = [pr_line(5, paths=("src/a.py",)), broken]
    assert sweep_decision(work, NEWER, entries, lambda: [BRANCH]).kind == UNKNOWN
    decision = sweep_decision(work, NEWER, entries,
                              lambda: [gh_record(number=9, paths=("src/b.py",))])
    assert [i.path for i in decision.held] == ["src/b.py"]


def test_a_huge_listing_is_clipped_in_the_message():
    decision = sweep_decision(work_of(violation("f1")), NEWER, [], lambda: "x" * 5000)
    assert decision.kind == UNKNOWN and len(decision.message) < 600


def test_the_sweep_marker_round_trips_and_parses_strictly():
    assert sweep_marker(BASE) == f"<!-- curamoir-sweep base={BASE} -->"
    body = f"Fixes things.\r\n\r\n{sweep_marker(BASE)}\r\n"
    assert parse_sweep_marker(body) == BASE
    assert parse_sweep_marker(f"{sweep_marker(BASE)}\n{sweep_marker(BASE)}") == BASE
    for bad in (None, 5, "", "no marker here",
                f"<!-- curamoir-sweep base={'A' * 40} -->",
                f"<!-- curamoir-sweep base={'a' * 39} -->",
                f"<!--  curamoir-sweep base={BASE} -->",
                f"<!-- curamoir-sweep base={BASE} --> trailing",
                f"quoted: <!-- curamoir-sweep base={BASE} -->",
                f"{sweep_marker(BASE)}\n{sweep_marker(NEWER)}",
                f"{sweep_marker(BASE)}\n<!-- curamoir-sweep base=oops -->"):
        assert parse_sweep_marker(bad) is None, bad
    with pytest.raises(ValueError):
        sweep_marker("A" * 40)


def test_a_gh_listing_item_becomes_a_record_and_a_capped_file_list_is_unknown():
    item = {"headRefName": BRANCH, "number": 7, "url": URL,
            "body": "x\n" + sweep_marker(BASE), "files": [{"path": "src/b.py"},
                                                          {"path": "src/a.py"}]}
    assert sweep_pr_listing(item) == {"branch": BRANCH, "number": 7, "url": URL,
                                      "base": BASE, "paths": ["src/a.py", "src/b.py"],
                                      "cross_repository": None}
    assert sweep_pr_listing(dict(item, isCrossRepository=False))["cross_repository"] is False
    assert sweep_pr_listing(dict(item, isCrossRepository=True))["cross_repository"] is True
    capped = dict(item, files=[{"path": f"f{i}.py"} for i in range(100)])
    assert sweep_pr_listing(capped)["paths"] is None
    assert sweep_pr_listing(dict(item, body=None))["base"] is None
    assert sweep_pr_listing(dict(item, files=None))["paths"] is None


SHAPES = {
    "list": lambda: [BRANCH],
    "tuple": lambda: (BRANCH,),
    "set": lambda: {BRANCH},
    "generator": lambda: (b for b in [BRANCH]),
    "dict_keys": lambda: {BRANCH: 1}.keys(),
    "records": lambda: [gh_record()],
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_one_contract_any_iterable_of_names_or_records_for_cap_and_decision(shape):
    work = work_of(violation("f1", "src/a.py"), violation("f2", "src/b.py"))
    decision = sweep_decision(work, NEWER, [pr_line()], SHAPES[shape])
    assert decision.kind == WORK and [i.path for i in decision.held] == ["src/a.py"]
    assert isinstance(OpenPrCap(SHAPES[shape], max_open_prs=1)(None), Refusal)
    assert OpenPrCap(SHAPES[shape], max_open_prs=2)(None) is None


def _raises():
    raise RuntimeError("gh timed out")


@pytest.mark.parametrize("listing", [lambda: None, _raises, lambda: "curamoir/sweep-x",
                                     lambda: [BRANCH, 5], lambda: {"branch": BRANCH}])
def test_one_contract_none_or_garbage_is_could_not_list_for_cap_and_decision(listing):
    assert sweep_decision(work_of(violation("f1")), NEWER, [], listing).kind == UNKNOWN
    assert isinstance(OpenPrCap(listing)(None), Degraded)


def test_a_held_delete_file_remedy_survives_apply_to(tmp_path):
    repo, _ledger = fixture(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        free = sweep_decision(sandbox.work_list, sandbox.base, [], lambda: [])
        assert VARIANT in [i.path for i in free.work_list.items]
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        held = sweep_decision(sandbox.work_list, sandbox.base, [pr_line(base=NEWER,
                              paths=(VARIANT,))], lambda: [BRANCH])
        held.apply_to(sandbox)
        apply_file_remedies(sandbox)
        assert (sandbox.path / VARIANT).is_file()
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)  # control: unheld, the same remedy deletes it
        sweep_decision(sandbox.work_list, sandbox.base, [], lambda: []).apply_to(sandbox)
        apply_file_remedies(sandbox)
        assert not (sandbox.path / VARIANT).exists()


# --- review fix: the trust boundary is the same repository ----------------------------
#
# Fixture provenance: the gh items are the shape `gh pr list --json
# headRefName,number,url,body,files,isCrossRepository` returns, built as the
# reviewer's probe built them (its shadowing case is reproduced verbatim
# below, with isCrossRepository added as gh would report it). A fork's
# headRefName is the fork owner's choice, so it can equal ours.

def gh_item(number, base, paths, cross=None, branch=BRANCH):
    item = {"headRefName": branch, "number": number, "url": f"u{number}",
            "body": "Fixes\n" + sweep_marker(base) + "\n",
            "files": [{"path": path} for path in paths]}
    if cross is not None:
        item["isCrossRepository"] = cross
    return item


SHADOW_WORK = (violation("1", "x.py"), violation("2", "y.py"))


@pytest.mark.parametrize("fork_first", [True, False])
def test_a_fork_pr_shadowing_our_branch_is_dropped_and_ours_is_still_a_duplicate(fork_first):
    """The reviewer's probe: our #7 at BASE covers x.py; a fork's #99 on the
    same head branch name claims base NEWER and zzz.py. Listed newest first
    it used to displace our ledger record and yield WORK on x.py: a second PR."""
    ours = sweep_pr_listing(gh_item(7, BASE, ["x.py"], cross=False))
    fork = sweep_pr_listing(gh_item(99, NEWER, ["zzz.py"], cross=True))
    listing = [fork, ours] if fork_first else [ours, fork]
    entries = [pr_line(7, paths=("x.py",))]
    decision = sweep_decision(work_of(*SHADOW_WORK), BASE, entries, lambda: listing)
    assert decision.kind == DUPLICATE_OPEN_PR
    assert [pr["number"] for pr in decision.open_prs] == [7]
    # The same at a newer base: x.py stays held by our #7, only y.py is work.
    decision = sweep_decision(work_of(*SHADOW_WORK), NEWER, entries, lambda: listing)
    assert decision.kind == WORK and [i.path for i in decision.work_list.items] == ["y.py"]


@pytest.mark.parametrize("fork_first", [True, False])
def test_the_probes_listing_without_provenance_never_yields_a_second_pr(fork_first):
    # The probe verbatim: no isCrossRepository at all (a cm-fkd.4 that forgot
    # the field). The ledger still pairs our #7 by number; #99 is unknown.
    ours = sweep_pr_listing(gh_item(7, BASE, ["x.py"]))
    fork = sweep_pr_listing(gh_item(99, NEWER, ["zzz.py"]))
    listing = [fork, ours] if fork_first else [ours, fork]
    entries = [pr_line(7, paths=("x.py",))]
    assert sweep_decision(work_of(*SHADOW_WORK), BASE, entries,
                          lambda: listing).kind == DUPLICATE_OPEN_PR
    assert sweep_decision(work_of(*SHADOW_WORK), NEWER, entries,
                          lambda: listing).kind == UNKNOWN


def test_a_fork_pr_with_a_current_base_marker_holds_nothing_and_refuses_nothing():
    work = work_of(*SHADOW_WORK)
    for item in (gh_item(99, BASE, ["x.py"], cross=True),
                 dict(gh_item(99, BASE, ["x.py"], cross=True), body="no marker")):
        decision = sweep_decision(work, BASE, [], lambda item=item: [sweep_pr_listing(item)])
        assert decision.kind == WORK, item
        assert decision.open_prs == () and decision.held == ()


def test_unknown_provenance_is_never_used_as_the_fallback():
    work = work_of(*SHADOW_WORK)
    for cross in (None, "false", 0, 1):
        same_base = dict(gh_record(paths=("x.py",)), cross_repository=cross)
        older = dict(gh_record(base=NEWER, paths=("x.py",)), cross_repository=cross)
        for record in (same_base, older):
            decision = sweep_decision(work, BASE, [], lambda record=record: [record])
            assert decision.kind == UNKNOWN, (cross, record["base"])
            assert "neither the ledger nor GitHub" in decision.refusal().message
    unknown = dict(gh_record(paths=("x.py",)), cross_repository=None)
    del unknown["cross_repository"]
    assert sweep_decision(work, NEWER, [], lambda: [unknown]).kind == UNKNOWN
    # ...but the ledger still knows its own PR, provenance or not (plain names too).
    entries = [pr_line(7, paths=("x.py",))]
    for listing in ([unknown], [BRANCH]):
        decision = sweep_decision(work, NEWER, entries, lambda listing=listing: listing)
        assert decision.kind == WORK and [i.path for i in decision.held] == ["x.py"]


def test_two_same_repo_prs_on_one_branch_pair_with_the_ledger_by_number_else_refuse():
    work = work_of(*SHADOW_WORK)
    first = gh_record(number=8, base=NEWER, paths=("y.py",))
    second = gh_record(number=7, base=NEWER, paths=("zzz.py",))
    decision = sweep_decision(work, BASE, [], lambda: [first, second])
    assert decision.kind == UNKNOWN
    assert sweep_decision(work, BASE, [], lambda: [second, first]).kind == UNKNOWN
    # The ledger knows #7: it is paired by number, whatever the order, and
    # #8 (same repository, base and paths listed) is known by its listing.
    entries = [pr_line(7, base=NEWER, paths=("x.py",))]
    for listing in ([first, second], [second, first]):
        decision = sweep_decision(work, BASE, entries, lambda listing=listing: listing)
        assert decision.kind == DUPLICATE_OPEN_PR, listing
        assert {pr["number"]: pr["paths"] for pr in decision.open_prs} == {
            7: ["x.py"], 8: ["y.py"]}


def test_the_cap_does_not_count_cross_repository_prs():
    fork = gh_record(branch="curamoir/sweep-x", cross_repository=True)
    ours = gh_record(branch="curamoir/sweep-y")
    assert OpenPrCap(lambda: [fork, fork, fork], max_open_prs=1)(None) is None
    assert isinstance(OpenPrCap(lambda: [fork, ours], max_open_prs=1)(None), Refusal)
    # A plain name (the old contract) and unknown provenance still count.
    unknown = dict(ours, cross_repository=None)
    assert isinstance(OpenPrCap(lambda: ["curamoir/sweep-z"], max_open_prs=1)(None), Refusal)
    assert isinstance(OpenPrCap(lambda: [unknown], max_open_prs=1)(None), Refusal)
