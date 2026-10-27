"""cm-fkd.15 (cm-fkd.4's ST6): the sweep's acceptance, through the real
runner.

Every sweep here is `runner.run_job(repo, "sweep")`, as `curamoir run
sweep` calls it: the real guards (kill switch, lock slot, budget, open-PR
cap), the real ledger, the real lint on the real rules, the real test gate
running the sample's own test suite, and the real publish pushing to
origin. Only the outside world stands in:

- origin is a local bare repository behind a GitHub URL, reached through
  the checkout's own `url.<path>.insteadOf` and `pushInsteadOf`
  (tests/test_sweep_job.checkout, as tests/test_publish.py's World);
- gh is tests/fake_gh.FakeGh, first on PATH and `github.GH`
  (tests/conftest.py's `no_real_gh`), whose `pr create` records the body
  it was sent and adds the PR to what `pr list` answers;
- `claude` is a stand-in script that makes the fix's edits, reached
  through `sweep_job.CLAUDE`, the seam the registered body reads (the
  autouse `no_real_claude` puts a refusing one there and first on PATH,
  so a test that forgets gets a refusal, never a model);
- HOME and XDG_CONFIG_HOME are isolated (`isolated_home`), so no global
  git config can rewrite origin to the real GitHub.

**The fixture** is fixtures/sample-repo with tests/test_lint.py's three
regex rules, one per principle: its 5 planted violations (PLANTED.json's
`violations`) across 3 principles, in 3 files: two in billing.py and two
in handlers.py (a print, structured-logging; a raise with no cause,
errors-carry-context) and one in handlers_v2.py (its existence,
no-file-variant-proliferation, whose `remedy: delete_file` the runner
applies itself). The stand-in fixes the four edits (tests/test_tier1.EDITS).

Acceptance criteria, and the tests that hold them:

- cm-fkd.4 AC1 (one green PR fixing all 5, a body listing 5 fixes and 3
  principle ids): test_one_sweep_opens_one_green_pr_fixing_all_five.
- cm-fkd.4 AC2 (a PR at `max_files_per_pr`, the rest for the next run):
  test_a_capped_sweep_leaves_the_rest_for_the_run_after_the_merge, which
  also holds that a second run at the same base is duplicate_open_pr, and
  that the run after the second merge is no_work.
- cm-v5p (a fix step that times out or crashes is an UNRECORDED line that
  budget.month_to_date counts):
  test_a_fix_step_that_does_not_finish_is_unrecorded_spend_through_the_runner.
- The epic's "broken test -> reduced PR, recorded":
  test_a_fix_that_breaks_a_test_is_left_out_of_a_reduced_pr.
"""

import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from curamoir import publish, runner, sweep_job
from curamoir.budget import UNRECORDED, month_to_date
from curamoir.fix_counts import AGENT, REMEDY
from curamoir.green_gate import GREEN, REDUCED
from curamoir.idempotence import DUPLICATE_OPEN_PR, NO_WORK, WORK, parse_sweep_marker
from curamoir.job_run import FAILURE, SUCCESS
from curamoir.ledger import parse_ledger_timestamp, repo_ledger
from tests.test_lint import ALL_REGEX_RULES
from tests.test_sweep_job import (  # noqa: F401  (isolated_home is an autouse fixture)
    GH_REPO,
    MODEL,
    PRICES,
    base_of,
    checkout,
    git,
    isolated_home,
    job_lines,
)
from tests.test_tier1 import BILLING, ECC, EDITS, HANDLERS, VARIANT, fake_claude

#: The sample's own test suite, run by this interpreter (which has pytest),
#: as PLANTED.json's `test_command` is with `python3`.
TEST_COMMAND = f"PYTHONPATH=src {shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
#: The three principles the planted violations break.
PRINCIPLES = {"structured-logging", ECC, "no-file-variant-proliferation"}
#: Every file the planted violations live in.
FILES = sorted([BILLING, HANDLERS, VARIANT])
#: The run's one clock (run_job's `now`): the branch, title and line agree.
#: A day the wall clock will never show again, so a sweep that dated its
#: branch by the wall clock instead would be caught.
NOW = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
DAY = "2025-12-31"
#: An edit beside billing's fixes that breaks the sample's test of line_total.
BREAKS_LINE_TOTAL = (BILLING, "return unit_price * quantity * (1 + TAX_RATE)",
                     "return unit_price * quantity * (2 + TAX_RATE)")


def config(extra=""):
    """The committed `.curamoir/curamoir.yaml`: the sample's tests as the
    gate, plus `extra` lines under `sweep:`."""
    return f"version: 1\nsweep:\n  test_command: {json.dumps(TEST_COMMAND)}\n{extra}"


def sample(tmp_path, extra=""):
    """The committed sample with all three rules, its origin a bare
    repository behind GitHub's URL, main pushed (tests/test_sweep_job)."""
    return checkout(tmp_path, *ALL_REGEX_RULES, config=config(extra))


def stand_in(tmp_path, monkeypatch, edits=EDITS, name="fix"):
    """A stand-in `claude` making `edits`, where the registered body finds
    it (sweep_job.CLAUDE); (path, log)."""
    claude, log = fake_claude(tmp_path, name, "done", edits=edits)
    monkeypatch.setattr(sweep_job, "CLAUDE", claude)
    return claude, log


def run_sweep(repo):
    """`curamoir run sweep`, in-process: run_job on the fixed clock; (RunReport,
    the job line it wrote)."""
    ran = runner.run_job(repo, "sweep", now=lambda: NOW)
    return ran, job_lines(repo)[-1]


def creates(gh):
    return [record for record in gh.call_records() if record["argv"][:2] == ["pr", "create"]]


def section(body, heading):
    """The lines of `body` under `heading`, up to the next heading."""
    lines = body.split("\n")
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("#")), len(lines))
    return lines[start:end]


def items(body, heading):
    """The list items under `heading`."""
    return [line for line in section(body, heading) if line.startswith("- ")]


def fix_lines(body):
    """The `### Fixes` list items: one per fix."""
    return items(body, "### Fixes")


def principle_ids(lines):
    """The principle id each fix line opens with (sweep_pr: `- `id` ...`)."""
    return {re.match(r"- `([^`]+)`", line).group(1) for line in lines}


def changed_on_origin(tmp_path, branch, base):
    """What `branch` on origin changes from `base`: {path: status letter}."""
    out = git(tmp_path / "origin.git", "diff", "--name-status", base, branch)
    return {path: status for status, path in (line.split("\t") for line in out.splitlines())}


def merge_on_origin(tmp_path, repo, gh, branch):
    """GitHub's merge of the PR from `branch`, done to the bare origin: a
    merge commit of main and the branch becomes main, the branch is
    deleted (as a repository that deletes head branches does), the PR is
    closed (no longer in the fake gh's open listing), and the checkout's
    remote-tracking ref moves as a `git fetch` moves it. Returns the new
    base."""
    origin = tmp_path / "origin.git"
    tree = git(origin, "rev-parse", f"{branch}^{{tree}}").strip()
    merge = subprocess.run(
        ["git", "-c", "user.name=GitHub", "-c", "user.email=noreply@example.invalid",
         "commit-tree", tree, "-p", "main", "-p", branch, "-m", f"Merge {branch}"],
        cwd=origin, check=True, capture_output=True, text=True).stdout.strip()
    git(origin, "update-ref", "refs/heads/main", merge)
    git(origin, "update-ref", "-d", f"refs/heads/{branch}")
    gh.feed(prs=[pr for pr in gh.prs() if pr["headRefName"] != branch])
    git(repo, "fetch", "--quiet", "--prune", "origin")
    assert base_of(repo) == merge
    return merge


@pytest.fixture(autouse=True)
def quick_lookups(monkeypatch):
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)


# --- cm-fkd.4 AC1 ----------------------------------------------------------------------

def test_one_sweep_opens_one_green_pr_fixing_all_five(tmp_path, no_real_gh, monkeypatch):
    repo = sample(tmp_path)
    base = base_of(repo)
    _claude, log = stand_in(tmp_path, monkeypatch)

    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert log.exists(), "the stand-in made the edits"
    assert line["outcome"] == SUCCESS and line["work"] == WORK, line["message"]
    # Exactly one PR.
    [create] = creates(no_real_gh)
    argv = create["argv"]
    branch = f"curamoir/sweep-{DAY}-{base[:8]}"
    assert argv[argv.index("--head") + 1] == branch
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--repo") + 1] == GH_REPO
    assert argv[argv.index("--title") + 1] == f"curamoir sweep {DAY}: 5 fixes of 3 principles"
    # Its body: 5 fixes, with 3 principle ids, nothing left out.
    body = create["body_file_content"]
    assert parse_sweep_marker(body) == base
    fixes = fix_lines(body)
    assert len(fixes) == 5
    assert principle_ids(fixes) == PRINCIPLES
    assert section(body, "### Files left for the next run")[0] == "None."
    # Its diff: exactly the 3 files, the variant deleted by the remedy.
    assert changed_on_origin(tmp_path, branch, base) == {BILLING: "M", HANDLERS: "M",
                                                         VARIANT: "D"}
    # Green, on the sample's own tests.
    assert line["test_gate"]["strategy"] == GREEN
    # The line agrees with the PR.
    assert line["pr"]["paths"] == FILES and line["pr"]["base"] == base
    assert len(line["fixes"]) == 5
    assert {fix["principle_id"] for fix in line["fixes"]} == PRINCIPLES
    assert {(fix["path"], fix["fixed_by"]) for fix in line["fixes"]} == {
        (BILLING, AGENT), (HANDLERS, AGENT), (VARIANT, REMEDY)}
    assert line["fix_summary"] == {"structured-logging": 2, ECC: 2,
                                   "no-file-variant-proliferation": 1}
    # Each fix is one planted violation, none twice and none missed.
    planted = json.loads((repo / "PLANTED.json").read_text(encoding="utf-8"))["violations"]
    assert sorted((fix["path"], fix["principle_id"]) for fix in line["fixes"]) == sorted(
        (violation["path"], violation["principle_id"]) for violation in planted)
    assert line["started"] == NOW.isoformat()
    assert ran.sentence.startswith("cúramóir ran 'sweep': success.")


# --- cm-fkd.4 AC2 ----------------------------------------------------------------------

def test_a_capped_sweep_leaves_the_rest_for_the_run_after_the_merge(tmp_path, no_real_gh,
                                                                   monkeypatch):
    repo = sample(tmp_path, extra="  max_files_per_pr: 2\n")
    first_base = base_of(repo)
    stand_in(tmp_path, monkeypatch)

    # Run 1: one PR at the cap, the third file left for the next run.
    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    [create] = creates(no_real_gh)
    first_branch = line["pr"]["branch"]
    assert line["pr"]["paths"] == [BILLING, HANDLERS]
    assert sorted(changed_on_origin(tmp_path, first_branch, first_base)) == [BILLING, HANDLERS]
    body = create["body_file_content"]
    fixes = fix_lines(body)
    assert len(fixes) == 4
    assert all(BILLING in fix or HANDLERS in fix for fix in fixes)
    assert principle_ids(fixes) == {"structured-logging", ECC}
    assert {fix["path"] for fix in line["fixes"]} == {BILLING, HANDLERS}
    assert items(body, "### Files left for the next run") == [
        f"- `{VARIANT}` (past `max_files_per_pr`)"]

    # Run 1b, at the same base with the PR still open: the duplicate check,
    # and no second PR.
    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert line["outcome"] == SUCCESS and line["work"] == DUPLICATE_OPEN_PR
    assert [pr["number"] for pr in line["open_sweep_prs"]] == [1]
    assert len(creates(no_real_gh)) == 1

    # The PR is merged: a new base, and the PR closed.
    second_base = merge_on_origin(tmp_path, repo, no_real_gh, first_branch)

    # Run 2, at the new base: the rest.
    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert len(creates(no_real_gh)) == 2
    assert line["work"] == WORK and line["pr"]["base"] == second_base
    assert line["pr"]["paths"] == [VARIANT]
    assert changed_on_origin(tmp_path, line["pr"]["branch"], second_base) == {VARIANT: "D"}
    body = creates(no_real_gh)[-1]["body_file_content"]
    assert principle_ids(fix_lines(body)) == {"no-file-variant-proliferation"}
    assert len(fix_lines(body)) == 1
    assert section(body, "### Files left for the next run")[0] == "None."

    # Run 3, once that PR is merged too: nothing is left.
    merge_on_origin(tmp_path, repo, no_real_gh, line["pr"]["branch"])
    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert line["outcome"] == SUCCESS and line["work"] == NO_WORK
    assert len(creates(no_real_gh)) == 2


# --- cm-v5p: unrecorded spend through the runner ---------------------------------------

@pytest.mark.parametrize("ends", ["timed_out", "crashed"])
def test_a_fix_step_that_does_not_finish_is_unrecorded_spend_through_the_runner(
        tmp_path, no_real_gh, monkeypatch, ends):
    """The fix step's `claude` starts, then hangs past FIX_TIMEOUT or dies
    with a traceback and no payload: the line is a failure whose counts are
    UNRECORDED with `model` (never zeros), and the budget counts it as
    unrecorded spend. Nothing is pushed."""
    repo = sample(tmp_path)
    claude, _ = stand_in(tmp_path, monkeypatch)
    pid_file = tmp_path / "fix.pid"
    # The hang is bounded, so a regression in run_in_group's kill cannot leave
    # it running for long; the crash is left the default timeout, so a slow
    # interpreter start cannot turn it into a timeout.
    behaviour = ("time.sleep(30)\n" if ends == "timed_out"
                 else "raise RuntimeError('the stand-in crashed')\n")
    with open(claude, "w", encoding="utf-8") as script:
        script.write(f"#!{sys.executable}\nimport os, time\n"
                     f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n" + behaviour)
    if ends == "timed_out":
        monkeypatch.setattr(sweep_job, "FIX_TIMEOUT", 3)
    ran, line = run_sweep(repo)

    assert pid_file.exists(), "the stand-in started"
    assert ran.exit_code == runner.EXIT_FAILURE, ran.sentence
    assert line["outcome"] == FAILURE
    assert ("timed out" if ends == "timed_out" else "exited 1") in line["message"]
    assert line["input_tokens"] == line["output_tokens"] == UNRECORDED
    assert line["model"] == MODEL and "cost_usd" not in line
    assert creates(no_real_gh) == []
    # Counted in the month of the line's own `at` (the wall clock,
    # job_guard.run_guarded), so a run at a month's end cannot miss it.
    spend = month_to_date(repo_ledger(repo), PRICES, parse_ledger_timestamp(line["at"]))
    assert spend.unrecorded == 1 and spend.usd == 0.0


# --- the epic's: a fix that breaks a test ----------------------------------------------

def test_a_fix_that_breaks_a_test_is_left_out_of_a_reduced_pr(tmp_path, no_real_gh,
                                                              monkeypatch):
    """The stand-in's billing.py fix also breaks line_total, which the
    sample's test catches: the gate drops billing.py, the PR carries the
    other two files, and the line and the body say what was dropped."""
    repo = sample(tmp_path)
    base = base_of(repo)
    stand_in(tmp_path, monkeypatch, edits=[*EDITS, BREAKS_LINE_TOTAL])

    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert line["test_gate"]["strategy"] == REDUCED
    assert [drop["path"] for drop in line["test_gate"]["dropped"]] == [BILLING]
    assert line["pr"]["paths"] == [HANDLERS, VARIANT]
    assert BILLING not in changed_on_origin(tmp_path, line["pr"]["branch"], base)
    assert {fix["path"] for fix in line["fixes"]} == {HANDLERS, VARIANT}
    body = creates(no_real_gh)[0]["body_file_content"]
    assert not any(BILLING in fix for fix in fix_lines(body))
    assert items(body, "### Files left for the next run") == [
        f"- `{BILLING}` (dropped by the test gate)"]
