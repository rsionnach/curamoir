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

import contextlib
import json
import re
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from curamoir import green_gate, publish, runner, sweep_job
from curamoir.budget import UNRECORDED, month_to_date
from curamoir.fix_counts import AGENT, REMEDY
from curamoir.fix_step import FIX_TOOLS
from curamoir.green_gate import GREEN, REDUCED
from curamoir.idempotence import DUPLICATE_OPEN_PR, NO_WORK, WORK, parse_sweep_marker
from curamoir.job_run import FAILURE, SUCCESS
from curamoir.ledger import parse_ledger_timestamp, repo_ledger
from curamoir.termination import Terminated
from tests.os_sandbox_stand_in import Absent, Unconfined
from tests.test_fix_step import NO_TESTS_SENTENCE
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
from tests.test_tier1 import BILLING, ECC, EDITS, HANDLERS, VARIANT, fake_claude, findings_reply

#: The sample's own test suite, run by this interpreter (which has pytest),
#: as PLANTED.json's `test_command` is with `python3`. `src` is put on the
#: path by pytest's own `pythonpath` option, not a leading PYTHONPATH=
#: assignment, so the hosted fix step's Bash rule can name the command
#: (fix_step.bash_rules refuses a leading assignment, cm-2qp.12).
TEST_COMMAND = (f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
                "-o pythonpath=src")
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


def any_creates(gh):
    """The PR creates gh was asked for on either path: `gh pr create`, or
    on the REST path of a cloud session (cm-2qp.9) `gh api -X POST
    .../pulls`. Each record gets the PR's body as `pr_body` (the body
    file's content, or the JSON body's `body`), so a test reads it the
    same way on either."""
    found = []
    for record in gh.call_records():
        if record["argv"][:2] == ["pr", "create"]:
            found.append({**record, "pr_body": record["body_file_content"]})
        elif record["argv"][:1] == ["api"] and "POST" in record["argv"]:
            found.append({**record, "pr_body": json.loads(record["input_content"])["body"]})
    return found


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
    [create] = any_creates(no_real_gh)
    argv = create["argv"]
    branch = f"curamoir/sweep-{DAY}-{base[:8]}"
    assert argv[argv.index("--head") + 1] == branch
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--repo") + 1] == GH_REPO
    assert argv[argv.index("--title") + 1] == f"curamoir sweep {DAY}: 5 fixes of 3 principles"
    # Its body: 5 fixes, with 3 principle ids, nothing left out.
    body = create["pr_body"]
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
    [create] = any_creates(no_real_gh)
    first_branch = line["pr"]["branch"]
    assert line["pr"]["paths"] == [BILLING, HANDLERS]
    assert sorted(changed_on_origin(tmp_path, first_branch, first_base)) == [BILLING, HANDLERS]
    body = create["pr_body"]
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
    assert len(any_creates(no_real_gh)) == 1

    # The PR is merged: a new base, and the PR closed.
    second_base = merge_on_origin(tmp_path, repo, no_real_gh, first_branch)

    # Run 2, at the new base: the rest.
    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert len(any_creates(no_real_gh)) == 2
    assert line["work"] == WORK and line["pr"]["base"] == second_base
    assert line["pr"]["paths"] == [VARIANT]
    assert changed_on_origin(tmp_path, line["pr"]["branch"], second_base) == {VARIANT: "D"}
    body = any_creates(no_real_gh)[-1]["pr_body"]
    assert principle_ids(fix_lines(body)) == {"no-file-variant-proliferation"}
    assert len(fix_lines(body)) == 1
    assert section(body, "### Files left for the next run")[0] == "None."

    # Run 3, once that PR is merged too: nothing is left.
    merge_on_origin(tmp_path, repo, no_real_gh, line["pr"]["branch"])
    ran, line = run_sweep(repo)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert line["outcome"] == SUCCESS and line["work"] == NO_WORK
    assert len(any_creates(no_real_gh)) == 2


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
    assert any_creates(no_real_gh) == []
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
    body = any_creates(no_real_gh)[0]["pr_body"]
    assert not any(BILLING in fix for fix in fix_lines(body))
    assert items(body, "### Files left for the next run") == [
        f"- `{BILLING}` (dropped by the test gate)"]


# --- E14.1 (cm-2qp.1): the runner kind, through the real runner ----------------------------

#: The green sweep's job line keys, and its PR body (the base written
#: <BASE>), as commit 3f50a3f (and this branch before E14.1) writes them in
#: test_one_sweep_opens_one_green_pr_fixing_all_five. Captured from that
#: code.
PRE_E14_SWEEP_KEYS = frozenset({
    "at", "cost_usd", "ended", "fix_summary", "fixes", "found_delta", "held_for_open_prs",
    "input_tokens", "job", "kind", "message", "model", "open_sweep_prs", "outcome",
    "output_tokens", "pr", "reason", "run_id", "started", "sync", "sync_reason",
    "test_gate", "tier1", "work"})
PRE_E14_SWEEP_BODY = (
    "<!-- curamoir-sweep base=<BASE> -->\n## curamoir sweep\n\n5 fixes of 3 principles at "
    "base `<BASE>`, in 3 files. Each fix is listed with its principle id; the sweep's "
    "ledger line records every one.\n\n### Fixes\n"
    "- `errors-carry-context` `no-unchained-raise` `src/sample/core/billing.py` line 37\n"
    "- `errors-carry-context` `no-unchained-raise` `src/sample/web/handlers.py` line 31\n"
    "- `no-file-variant-proliferation` `no-variant-module` `src/sample/web/handlers_v2.py` "
    "line 1 (deleted by the runner's remedy, not edited)\n"
    "- `structured-logging` `no-bare-print` `src/sample/core/billing.py` line 15\n"
    "- `structured-logging` `no-bare-print` `src/sample/web/handlers.py` line 22\n\n"
    "### Test gate\ntests green with all 3 fixed file(s) (1 run)\nNothing was dropped.\n\n"
    "### Files left for the next run\nNone.\n\n### Remedies skipped\nNone.\n\n"
    "### Promotion candidates\nNone yet: promotion candidates come from the promotion "
    "ladder (E4-R1, E4-R2), which has not landed.\n\n### Grade\nnot graded yet (Epic 7)\n")


def test_with_no_runner_variable_the_sweep_is_the_pre_e14_sweep_plus_runner_local(
        tmp_path, no_real_gh, monkeypatch):
    """§8 acceptance 3, for the sweep: no runner variable is the local
    runner, and the line and the PR are what they were before E14.1 but for
    the line's `runner: local`."""
    repo = sample(tmp_path)
    base = base_of(repo)
    stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ={})
    line = job_lines(repo)[-1]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert set(line) == PRE_E14_SWEEP_KEYS | {"runner"}
    assert line["runner"] == "local"
    [create] = any_creates(no_real_gh)
    assert create["pr_body"].replace(base, "<BASE>") == PRE_E14_SWEEP_BODY


def test_a_session_puts_its_transcript_link_on_the_line_and_in_the_pr_body(
        tmp_path, no_real_gh, monkeypatch):
    """E14.1: CLAUDE_CODE_REMOTE_SESSION_ID's `cse_` id is the
    claude.ai/code/session_ link, on the line and in the PR a reviewer
    reads (§8 acceptance 2)."""
    repo = sample(tmp_path)
    base = base_of(repo)
    stand_in(tmp_path, monkeypatch)
    link = "https://claude.ai/code/session_01AbCdEf"

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ={
        "CURAMOIR_RUNNER": "hosted", "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01AbCdEf"})
    line = job_lines(repo)[-1]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert (line["runner"], line["transcript"]) == ("hosted", link)
    [create] = any_creates(no_real_gh)
    body = create["pr_body"]
    assert f"Transcript of the run that opened this PR: {link}" in body.splitlines()
    assert parse_sweep_marker(body) == base


def test_an_invalid_session_id_is_unavailable_on_the_line_and_left_out_of_the_pr_body(
        tmp_path, no_real_gh, monkeypatch):
    repo = sample(tmp_path)
    stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ={
        "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_x](https://evil.example)"})
    line = job_lines(repo)[-1]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert line["transcript"].startswith("unavailable (")
    assert "evil" not in line["transcript"]
    body = any_creates(no_real_gh)[0]["pr_body"]
    assert "Transcript" not in body and "evil" not in body


# --- E14.2 (cm-2qp.2): the test gate by runner kind, through the real runner ---------------

#: The `test_gate` field's keys as 3f50a3f's GateResult.ledger_fields writes
#: them: neither runner kind adds or drops one (the job line's `runner`
#: says which boundary the gate ran under).
PRE_E14_TEST_GATE_KEYS = frozenset({
    "strategy", "cause", "why", "command", "command_source", "kept", "dropped", "runs",
    "canary"})


@pytest.mark.parametrize("environ, hosted", [
    ({}, False),
    ({"CURAMOIR_RUNNER": "hosted", "CLAUDE_CODE_REMOTE": "true"}, True),
], ids=["no-runner-variable", "hosted"])
def test_the_gate_is_wrapped_on_the_local_runner_and_bare_on_the_hosted_one(
        tmp_path, no_real_gh, monkeypatch, environ, hosted):
    """§5 R3 end to end: `run.runner` reaches the gate. A hosted sweep's
    gate never asks the OS sandbox (Absent would refuse it); a sweep with no
    runner variable self-tests once and wraps every run, as at 3f50a3f.
    Both open the green PR, with the pre-E14 test_gate fields."""
    stand_in_sandbox = Absent() if hosted else Unconfined()
    monkeypatch.setattr(green_gate, "OS_SANDBOX", stand_in_sandbox)
    repo = sample(tmp_path)
    stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ=environ)
    line = job_lines(repo)[-1]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert line["runner"] == ("hosted" if hosted else "local")
    assert line["test_gate"]["strategy"] == GREEN
    assert set(line["test_gate"]) == PRE_E14_TEST_GATE_KEYS
    assert len(any_creates(no_real_gh)) == 1
    runs = len(line["test_gate"]["runs"])
    if hosted:
        assert stand_in_sandbox.self_tests == [] and stand_in_sandbox.wrapped == []
    else:
        assert len(stand_in_sandbox.self_tests) == 1
        assert len(stand_in_sandbox.wrapped) == runs + 1  # + the canary


# --- E14.3 (cm-2qp.3): the fix step by runner kind, through the real runner ---------------

HOSTED_ENVIRON = {"CURAMOIR_RUNNER": "hosted", "CLAUDE_CODE_REMOTE": "true"}


@pytest.mark.parametrize("environ, hosted", [
    ({}, False),
    (HOSTED_ENVIRON, True),
], ids=["no-runner-variable", "hosted"])
def test_the_fix_agent_has_bash_on_the_hosted_runner_and_not_on_the_local_one(
        tmp_path, no_real_gh, monkeypatch, environ, hosted):
    """§5 R4 end to end: `run.runner` reaches the fix step. A hosted sweep's
    fix agent is given Bash, approved by one allow rule for the base's test
    command with arguments after it (cm-2qp.12; never the bare `Bash`), with
    the denylist --settings and --restricted; a sweep with no runner
    variable runs 3f50a3f's command line (FIX_TOOLS, --restricted, no
    Bash). The prompt follows: only the hosted agent is told it may run
    commands. Both open the green PR."""
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent() if hosted else Unconfined())
    repo = sample(tmp_path)
    _, log = stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ=environ)
    argv = json.loads(log.read_text(encoding="utf-8"))["argv"]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert len(any_creates(no_real_gh)) == 1
    tools = argv[argv.index("--tools") + 1].split(",")
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "--settings" in argv and "--restricted" in argv
    prompt = argv[1]
    if hosted:
        assert tools == [*FIX_TOOLS, "Bash"] and allowed[-1] == f"Bash({TEST_COMMAND} *)"
        assert "Bash" not in allowed
        assert argv[argv.index("--disallowedTools") + 1].split(",")[-1] == "Bash(git *)"
        # The job line says what the agent's Bash was (cm-2qp.12).
        assert job_lines(repo)[-1][sweep_job.FIX_BASH_FIELD] == TEST_COMMAND
        # The base's own test command, as the gate resolves it.
        assert f"you may run the repository's test command, `{TEST_COMMAND}`" in prompt
        assert "cannot run commands" not in prompt
    else:
        assert tools == list(FIX_TOOLS) and "Bash" not in allowed
        assert "Bash(git *)" not in argv[argv.index("--disallowedTools") + 1]
        assert sweep_job.FIX_BASH_FIELD not in job_lines(repo)[-1]  # the local line as was
        assert "You cannot run commands, and you do not need to" in prompt


@pytest.mark.parametrize("environ, hosted", [
    ({}, False),
    (HOSTED_ENVIRON, True),
], ids=["no-runner-variable", "hosted"])
def test_the_sweep_hands_its_runner_to_the_scan_and_the_fix_step_alike(
        tmp_path, no_real_gh, monkeypatch, environ, hosted):
    """Both of the sweep's model calls take `run.runner` on purpose
    (tier1.run_tier1_scan passes it on to run_fix_step), so neither builds
    its command line from a default: on hosted both keep --restricted
    (user decision 2026-10-29), and only the fix step has Bash; with no
    runner variable both are as at 3f50a3f. errors-carry-context is left
    without a rule here, so the tier-1 scan runs
    (tests/test_tier1.tier1_repo)."""
    from curamoir import tier1
    from tests.test_lint import NO_BARE_PRINT, NO_VARIANT_MODULE

    calls = []
    # The scan's stand-in reads only and finds nothing; the fix's makes the
    # edits (one `claude` serves both calls, so the scan's is swapped in).
    scan_claude, _ = fake_claude(tmp_path, "scan", findings_reply())

    def recording(name, real, **override):
        def run(*args, **kwargs):
            result = real(*args, **{**kwargs, **override})
            calls.append((name, kwargs["runner"].hosted, result.argv))
            return result
        return run

    monkeypatch.setattr(tier1, "run_fix_step",
                        recording("scan", tier1.run_fix_step, claude=scan_claude))
    monkeypatch.setattr(sweep_job, "run_fix_step", recording("fix", sweep_job.run_fix_step))
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent() if hosted else Unconfined())
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=config())
    stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ=environ)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert [(name, was_hosted) for name, was_hosted, _ in calls] == [
        ("scan", hosted), ("fix", hosted)]
    for name, _, argv in calls:
        tools = argv[argv.index("--tools") + 1].split(",")
        assert "--restricted" in argv
        assert ("Bash" in tools) is (hosted and name == "fix")


#: What a test run the hosted agent makes could leave in the clone, were
#: HOSTED_TEST_ENVIRONMENT not honoured (a tool that ignores it, a cache of
#: another tool): bytecode and a pytest cache.
TEST_RUN_DEBRIS = {"src/sample/core/__pycache__/billing.cpython-312.pyc": "bytecode",
                   ".pytest_cache/v/cache/lastfailed": "{}\n"}


def test_test_run_debris_in_the_clone_is_dropped_and_the_pr_still_opens(
        tmp_path, no_real_gh, monkeypatch):
    """cm-2qp.3 edge case: on hosted the agent's own test run can leave
    files in the clone. They are new files no violation names, so the
    review drops them (cm-9ab's drop and re-gate) and the green PR opens
    with the fixes alone; the dropped files are on the line's
    `scope_dropped`."""
    from tests.test_fix_step import fake_claude as file_writing_claude

    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())
    repo = sample(tmp_path)
    claude, _ = file_writing_claude(tmp_path, edits=EDITS, writes=TEST_RUN_DEBRIS)
    monkeypatch.setattr(sweep_job, "CLAUDE", str(claude))

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ=HOSTED_ENVIRON)
    line = job_lines(repo)[-1]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert len(any_creates(no_real_gh)) == 1
    assert {(d["kind"], d["path"]) for d in line["scope_dropped"]} == {
        ("file_added", path) for path in TEST_RUN_DEBRIS}
    assert "scope_after_drop" not in line
    assert not set(TEST_RUN_DEBRIS) & set(line["test_gate"]["kept"])
    assert set(line["test_gate"]["kept"]) == set(FILES)  # the deletion of handlers_v2 too


def _no_command(sandbox):
    """resolve_test_command as for a base with no test command at all."""
    from curamoir.green_gate import NO_TEST_COMMAND, GateCommand

    return GateCommand(None, None, 60, 3, NO_TEST_COMMAND, "no test command at the base")


def _git_fails(sandbox):
    from curamoir.git_guard import GitGuardError

    raise GitGuardError("git ls-tree did not finish within 60 seconds")


def _pipeline_command(sandbox):
    """resolve_test_command as for a base whose configured command no allow
    rule can name (a pipeline: fix_step.bash_rules)."""
    from curamoir.green_gate import GateCommand

    return GateCommand(f"{TEST_COMMAND} | cat", "config", 60, 3)


@pytest.mark.parametrize("resolve, fix_bash", [
    (_no_command, sweep_job.FIX_BASH_NO_COMMAND),
    (_git_fails, sweep_job.FIX_BASH_NO_COMMAND),
    (_pipeline_command, sweep_job.FIX_BASH_NOT_NAMEABLE),
], ids=["no-command", "git-fails", "no-rule-for-it"])
def test_a_hosted_fix_agent_with_no_resolvable_test_command_gets_no_bash(
        tmp_path, no_real_gh, monkeypatch, resolve, fix_bash):
    """sweep_job asks green_gate.resolve_test_command for the command the
    hosted prompt offers and its Bash rule names (sweep_job._hosted_test_command);
    none at the base, a git failure while resolving it, or a command no
    rule can name is no command: the agent is given no Bash at all (FIX_TOOLS,
    no Bash rule, cm-2qp.12) and told to run no tests. The gate resolves its
    own, unaffected (its module's function is not the one replaced here),
    and the PR opens."""
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())
    monkeypatch.setattr(sweep_job, "resolve_test_command", resolve)
    repo = sample(tmp_path)
    _, log = stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ=HOSTED_ENVIRON)
    argv = json.loads(log.read_text(encoding="utf-8"))["argv"]
    prompt = argv[1]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert NO_TESTS_SENTENCE in prompt and "you may run the repository's test command" not in (
        prompt)
    assert argv[argv.index("--tools") + 1].split(",") == list(FIX_TOOLS)
    assert not any(rule.startswith("Bash")
                   for rule in argv[argv.index("--allowedTools") + 1].split(","))
    # The job line gives a fixed reason, never the command no rule can name.
    line = job_lines(repo)[-1]
    assert line[sweep_job.FIX_BASH_FIELD] == fix_bash


@pytest.mark.parametrize("stop", [Terminated(signal.SIGTERM), KeyboardInterrupt(),
                                  ValueError("a reply the checks choked on")],
                         ids=["Terminated", "KeyboardInterrupt", "ValueError"])
def test_a_hosted_run_that_ends_during_the_fix_call_still_records_fix_bash(
        tmp_path, no_real_gh, monkeypatch, stop):
    """fix_bash is noted before the fix call, so whatever ends the run once
    the call has begun (a signal, ^C, an exception) leaves it on the job
    line: every line of a run whose fix agent was called says what its Bash
    was (cm-2qp.12)."""
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())
    repo = sample(tmp_path)
    stand_in(tmp_path, monkeypatch)

    def ends_during_the_call(*_args, before_call, **_kwargs):
        before_call()
        raise stop

    monkeypatch.setattr(sweep_job, "run_fix_step", ends_during_the_call)
    # Caught here as well, so a regression fails on the asserts rather than
    # letting a KeyboardInterrupt stop the test session.
    with contextlib.suppress(type(stop)):
        runner.run_job(repo, "sweep", now=lambda: NOW, environ=HOSTED_ENVIRON)

    line = job_lines(repo)[-1]
    assert line["outcome"] == FAILURE
    assert line[sweep_job.FIX_BASH_FIELD] == TEST_COMMAND


@pytest.mark.parametrize("command, value", [
    ("pytest  -q &&npm test", "pytest -q && npm test"),
    (None, "none (no test command)"),
    ("", "none (no test command)"),
    ("pytest -q\n`echo` ghp_0123456789abcdefghijklmnopqrstuvwxyz",
     "none (not nameable as a rule)"),
    ("pytest -q --token ghp_0123456789abcdefghijklmnopqrstuvwxyz",
     "none (not nameable as a rule)"),
    ("pytest tests/test_token.py::test_refresh_works", "none (not nameable as a rule)"),
], ids=["normalised", "none", "empty", "unnameable", "redacted-secret", "redacted-name"])
def test_the_fix_bash_field_names_the_command_or_a_fixed_reason(command, value):
    """FIX_BASH_FIELD's value: the command as the rules name it, or fixed
    text, never a command no rule can name (it may hold a secret)."""
    assert sweep_job._fix_bash(command) == value


#: The run tokens a GitHub Actions job hands the sweep, which the hosted
#: fix step must not pass on (cm-2qp.12).
RUN_TOKENS = {"GITHUB_TOKEN": "ghs_run", "GH_TOKEN": "ghs_run",
              "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc", "ACTIONS_RUNTIME_TOKEN": "runtime",
              "ACTIONS_RESULTS_URL": "https://results.example"}


@pytest.mark.parametrize("environ, hosted", [
    ({}, False),
    (HOSTED_ENVIRON, True),
], ids=["no-runner-variable", "hosted"])
def test_the_hosted_sweeps_fix_agent_is_given_no_run_token(
        tmp_path, no_real_gh, monkeypatch, environ, hosted):
    """cm-2qp.12 end to end: with the run's tokens in the sweep's own
    environment, the hosted fix step's process has none of them and keeps
    ANTHROPIC_API_KEY; the local one inherits them, as at 3f50a3f. Publish
    runs in the sweep's process, so the PR still opens."""
    from tests.test_fix_step import fake_claude as env_logging_claude

    for name, value in {**RUN_TOKENS, "ANTHROPIC_API_KEY": "sk-ant"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent() if hosted else Unconfined())
    repo = sample(tmp_path)
    claude, log = env_logging_claude(tmp_path, edits=EDITS)
    monkeypatch.setattr(sweep_job, "CLAUDE", str(claude))

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ=environ)
    seen = set(json.loads(log.read_text(encoding="utf-8"))["env"])

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert len(any_creates(no_real_gh)) == 1
    assert "ANTHROPIC_API_KEY" in seen
    assert (not seen & set(RUN_TOKENS)) if hosted else set(RUN_TOKENS) <= seen


def test_a_local_sweep_never_resolves_a_test_command_for_the_prompt(
        tmp_path, no_real_gh, monkeypatch):
    """On the local runner the prompt names no command, so sweep_job never
    asks for one: resolve_test_command replaced by one that fails the test
    is never called there (the gate's own call is green_gate's)."""
    def must_not_be_called(_sandbox):
        raise AssertionError("the local sweep resolved a test command for the prompt")

    monkeypatch.setattr(green_gate, "OS_SANDBOX", Unconfined())
    monkeypatch.setattr(sweep_job, "resolve_test_command", must_not_be_called)
    repo = sample(tmp_path)
    _, log = stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ={})
    prompt = json.loads(log.read_text(encoding="utf-8"))["argv"][1]

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert "You cannot run commands, and you do not need to" in prompt
