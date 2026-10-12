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
from curamoir.guards.reasons import Reason
from curamoir.idempotence import (
    DUPLICATE_OPEN_PR,
    NO_WORK,
    PR_FIELD,
    SWEEP_BRANCH_PREFIX,
    UNKNOWN,
    WORK,
    WORK_FIELD,
    pr_field,
    sweep_decision,
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
    prs = sweep_prs([older, *noise, newer, broken])
    assert list(prs) == [BRANCH]
    assert prs[BRANCH]["number"] == 9


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
