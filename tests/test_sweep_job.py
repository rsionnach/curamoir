"""cm-fkd.13 (cm-fkd.4's ST4): the sweep body's first half, fix_scope's
runner steps 1 to 2c, and the line's cost (curamoir.sweep_job).

Acceptance criteria, and the tests that hold them:

- "Empty work list with tier 1 off -> success line, work: no_work,
  found_delta, no token fields, snapshot written":
  test_an_empty_work_list_with_tier1_off_is_no_work_with_a_snapshot.
- "Open PR at the same base -> duplicate_open_pr":
  test_an_open_sweep_pr_from_the_same_base_is_a_duplicate.
- "Listing raises -> refusal reason idempotence, no snapshot":
  test_a_listing_that_raises_is_an_idempotence_refusal_with_no_snapshot.
- "Scan that changes the sandbox -> refusal carrying the tier1 field and
  its cost": test_a_scan_that_changes_the_sandbox_is_refused_with_its_field_and_cost.
- "Scan that times out -> input_tokens == output_tokens == 'unrecorded' +
  model, counted by budget.month_to_date as unrecorded":
  test_a_scan_that_times_out_is_unrecorded_spend_never_zero.
- "No origin/<default> -> refusal before any sandbox":
  test_no_origin_default_is_refused_before_any_sandbox.
- "line_cost_fields unit tests cover every combination": the tests under
  "the line's cost", test_line_cost_fields_* .

**The world.** Every test runs real git: a checkout of the committed
sample (tests/conftest.committed_sample, real rules, the real
scripts/curamoir-lint) whose `origin` names a GitHub repository
(`https://github.com/acme/widgets.git`) redirected by the checkout's own
`url.<path>.insteadOf` and `pushInsteadOf` to a local bare repository, as
tests/test_publish.py's World does. Nothing is pushed here; `git fetch`
in a test goes to the bare repository. gh is tests/fake_gh.FakeGh (the
autouse `no_real_gh`), whose `repo view` names `main` as the default and
whose `pr list` answers from what the test feeds it. The model calls are
stand-in `claude` scripts (tests/test_tier1.fake_claude). HOME and
XDG_CONFIG_HOME are isolated for every test (`isolated_home`), so the
developer's global git config cannot rewrite origin to the real GitHub.
No test reaches GitHub, the real gh or a model.
"""

import contextlib
import json
import os
import pathlib
import signal
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from curamoir import github, sweep_job, tier1
from curamoir.budget import UNRECORDED, PriceTable, month_to_date
from curamoir.fix_counts import read_snapshot, snapshot_path
from curamoir.fix_step import NOT_STARTED, FixStepRun
from curamoir.git_guard import GitGuardError
from curamoir.guards.reasons import Reason
from curamoir.idempotence import DUPLICATE_OPEN_PR, NO_WORK, TIER1_FAILED, WORK
from curamoir.job_run import FAILURE, REFUSED, SUCCESS, JobRun
from curamoir.ledger import repo_ledger
from curamoir.sweep_job import (
    SweepTools,
    line_cost_fields,
    resolve_sweep_base,
    run_sweep,
)
from curamoir.termination import Terminated
from curamoir.tier1 import (
    ATTEMPTS_EXHAUSTED,
    FAILED,
    NO_CANDIDATES,
    OFF,
    READY,
    SANDBOX_CHANGED,
    SCANNED,
    Tier1Plan,
    Tier1Scan,
)
from tests import claude_payloads
from tests.conftest import committed_sample
from tests.gh_pr_payloads import SAME_REPOSITORY_PR, as_sweep_pr
from tests.test_lint import NO_BARE_PRINT, NO_VARIANT_MODULE
from tests.test_tier1 import BILLING, EDITS, fake_claude, findings_reply

GH_REPO = "github.com/acme/widgets"
GITHUB_URL = "https://github.com/acme/widgets.git"
MODEL = "claude-sonnet-5"
PRICES = PriceTable({MODEL: {"input": 2.0, "output": 10.0}})
TIER1_OFF = "version: 1\nsweep:\n  tier1_scan: false\n"
TIER1_ON = "version: 1\nsweep:\n  test_command: \"true\"\n"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """HOME and XDG_CONFIG_HOME for this test alone (tests/test_publish.py's
    fixture): no global git config of the developer's applies."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[curamoir-test]\n\tisolated-home = yes\n",
                                     encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    # Moving HOME hides every per-user site-packages (~/Library/Python/X.Y
    # on macOS), and where that is the only PyYAML on PATH, scripts/curamoir-lint
    # finds no interpreter that can read the rules and the sweep refuses at lint
    # (cm-854). The interpreter running the tests has PyYAML (a project
    # dependency), so its directory goes first on PATH for the lint to find.
    monkeypatch.setenv("PATH", os.pathsep.join(
        [str(pathlib.Path(sys.executable).parent), *filter(None, [os.environ.get("PATH")])]))
    return home


@pytest.fixture(autouse=True)
def fix_step_cannot_start(tmp_path, monkeypatch):
    """Steps 1 to 2c are this file's: past them, the fix step's `claude`
    cannot start (the real run_fix_step, given an executable that does not
    exist), so the sweep records a failure starting NOT_STARTED with no
    call and no cost of its own, and every line here is the scan's alone.
    tests/test_sweep_publish.py runs steps 3 to 10."""
    real = sweep_job.run_fix_step
    missing = str(tmp_path / "no-fix-claude")
    monkeypatch.setattr(sweep_job, "run_fix_step",
                        lambda sandbox, prompt, **options: real(
                            sandbox, prompt, **{**options, "claude": missing}))


def past_step_2c(line):
    """The line of a sweep that got past step 2c: the fix step's failure to
    start (fix_step_cannot_start)."""
    return line["outcome"] == FAILURE and line["message"].startswith(NOT_STARTED)


def checkout(tmp_path, *rules, config=TIER1_OFF, clean=False):
    """The committed sample with `rules` and `config` committed, its origin
    a bare local repository behind a GitHub URL (module docstring), main
    pushed and origin/HEAD set. `clean` replaces the two planted prints
    with logger calls, so the no-bare-print lint finds nothing."""
    repo = committed_sample(tmp_path, *rules)
    (repo / ".curamoir" / "curamoir.yaml").write_text(config, encoding="utf-8")
    if clean:
        for path, old, new in (EDITS[0], EDITS[2]):
            target = repo / path
            target.write_text(target.read_text(encoding="utf-8").replace(old, new),
                              encoding="utf-8")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)],
                   check=True)
    for command in (["add", "-A"], ["commit", "--quiet", "-m", "config"],
                    ["remote", "add", "origin", GITHUB_URL],
                    ["config", f"url.{origin}.insteadOf", GITHUB_URL],
                    ["config", f"url.{origin}.pushInsteadOf", GITHUB_URL],
                    ["push", "--quiet", "origin", "main"],
                    ["remote", "set-head", "origin", "main"]):
        git(repo, *command)
    return repo


def base_of(repo):
    return git(repo, "rev-parse", "refs/remotes/origin/main").strip()


def sweep(repo, **tools):
    """run_sweep on a JobRun writing to `repo`'s own ledger; its line."""
    with JobRun(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(**tools))
    return job_lines(repo)[-1]


def job_lines(repo):
    return [entry for entry in repo_ledger(repo).entries() if entry.get("kind") == "job"]


def gh_calls(gh, command):
    return [argv for argv in gh.calls() if argv[:2] == command.split()]


def no_cost_fields(line):
    return not {"input_tokens", "output_tokens", "model", "cost_usd"} & set(line)


# --- the acceptance criteria -----------------------------------------------------------

def test_an_empty_work_list_with_tier1_off_is_no_work_with_a_snapshot(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, clean=True)
    assert not snapshot_path(repo).exists()

    line = sweep(repo)

    assert line["outcome"] == SUCCESS and line["work"] == NO_WORK
    assert line["found_delta"]["base"] == base_of(repo)
    assert line["found_delta"]["baseline"] is True
    assert line["tier1"]["status"] == OFF and line["tier1"]["scanned"] is False
    assert no_cost_fields(line)
    written = read_snapshot(snapshot_path(repo))
    assert written is not None and written["base"] == base_of(repo)
    assert written["digest"] == line["found_delta"]["digest"]
    # No work and no scan: the open PRs are never listed.
    assert gh_calls(no_real_gh, "pr list") == []


def test_an_open_sweep_pr_from_the_same_base_is_a_duplicate(tmp_path, no_real_gh):
    """Also the cm-fkd.10 note's happy path: the real gh-backed listing
    (FakeGh) reaches sweep_decision, which reads the PR's marker, rather
    than raising inside it and refusing as UNKNOWN."""
    repo = checkout(tmp_path, NO_BARE_PRINT)
    branch = f"curamoir/sweep-2026-10-20-{base_of(repo)[:8]}"
    no_real_gh.feed(prs=[as_sweep_pr(SAME_REPOSITORY_PR, branch, base_of(repo), number=7)])

    line = sweep(repo)

    assert line["outcome"] == SUCCESS and line["work"] == DUPLICATE_OPEN_PR
    assert [pr["number"] for pr in line["open_sweep_prs"]] == [7]
    assert line["found_delta"]["base"] == base_of(repo)
    assert line["tier1"]["status"] == OFF
    assert no_cost_fields(line)
    assert read_snapshot(snapshot_path(repo))["base"] == base_of(repo)
    # The listing named origin's repository, as publish will.
    [listing] = gh_calls(no_real_gh, "pr list")
    assert listing[listing.index("--repo") + 1] == GH_REPO


def test_a_listing_that_raises_is_an_idempotence_refusal_with_no_snapshot(tmp_path,
                                                                        no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    no_real_gh.feed(fail_on={"pr list": "HTTP 503: Service Unavailable"})

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.IDEMPOTENCE.value
    assert "HTTP 503" in line["message"]
    assert "found_delta" not in line and "tier1" not in line and "work" not in line
    assert not snapshot_path(repo).exists()
    assert len(gh_calls(no_real_gh, "pr list")) == 1


def test_a_scan_that_changes_the_sandbox_is_refused_with_its_field_and_cost(tmp_path,
                                                                          no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply(), writes={BILLING: "x = 1\n"})

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SANDBOX.value
    assert "reads only" in line["message"]
    assert line["tier1"]["status"] == SANDBOX_CHANGED
    assert line["tier1"]["cost_usd"] == 0.02
    usage = claude_payloads.USAGE
    assert (line["input_tokens"], line["output_tokens"], line["model"], line["cost_usd"]) == (
        usage["input_tokens"], usage["output_tokens"], MODEL, 0.02)
    assert "found_delta" not in line
    assert not snapshot_path(repo).exists()


def test_a_scan_that_times_out_is_unrecorded_spend_never_zero(tmp_path, no_real_gh):
    """The scan is FAILED (timed out, no payload); the lint has work, so the
    sweep goes on to the fix step (which cannot start here), whose failure
    line carries the scan's cost as UNRECORDED. The same line, read by the
    budget, is counted as unrecorded, not priced at $0."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    claude, _ = fake_claude(tmp_path, "scan", None)
    pid_file = tmp_path / "scan.pid"
    pathlib.Path(claude).write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8")
    try:
        line = sweep(repo, claude=claude, scan_timeout=1)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    assert past_step_2c(line)
    assert line["tier1"]["status"] == FAILED
    assert line["tier1"]["reason"] == "the tier-1 scan timed out"
    assert line["input_tokens"] == line["output_tokens"] == UNRECORDED
    assert line["model"] == MODEL
    assert "cost_usd" not in line
    spend = month_to_date(repo_ledger(repo), PRICES, datetime.now(timezone.utc))
    assert spend.unrecorded == 1 and spend.usd == 0.0 and spend.unaccounted == 0


def test_no_origin_default_is_refused_before_any_sandbox(tmp_path, no_real_gh, monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    git(repo, "remote", "set-head", "origin", "-d")
    parent = tmp_path / "sandboxes"
    parent.mkdir()
    made = []
    monkeypatch.setattr(sweep_job, "prepare_sandbox",
                        lambda *args, **kwargs: made.append(args))

    line = sweep(repo, sandbox_parent=str(parent))

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "refs/remotes/origin/main" in line["message"]
    assert made == [] and list(parent.iterdir()) == []
    assert "found_delta" not in line and "tier1" not in line and no_cost_fields(line)
    assert not snapshot_path(repo).exists()
    assert gh_calls(no_real_gh, "pr list") == []


# --- the base --------------------------------------------------------------------------

def test_the_base_is_origins_default_never_unpushed_work(tmp_path, no_real_gh):
    """A local commit not on origin is not swept: the base is
    refs/remotes/origin/main, and the planted print the local commit adds
    is not in the work list."""
    repo = checkout(tmp_path, NO_BARE_PRINT, clean=True)
    pushed = base_of(repo)
    (repo / "unpushed.py").write_text('print("local only")\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "unpushed")
    assert git(repo, "rev-parse", "HEAD").strip() != pushed

    line = sweep(repo)

    assert line["found_delta"]["base"] == pushed
    assert line["work"] == NO_WORK


def test_a_fetched_origin_ahead_of_every_local_branch_is_the_base(tmp_path, no_real_gh):
    """origin/main has a commit no local branch has (fetched, not merged):
    the sandbox is made at it, the clone fetching it by its SHA."""
    repo = checkout(tmp_path, NO_BARE_PRINT, clean=True)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(tmp_path / "origin.git"), str(other)],
                   check=True)
    (other / "new.py").write_text('print("from elsewhere")\n', encoding="utf-8")
    for command in (["add", "-A"], ["-c", "user.name=o", "-c", "user.email=o@x.invalid",
                                    "commit", "--quiet", "-m", "elsewhere"],
                    ["push", "--quiet", "origin", "main"]):
        git(other, *command)
    git(repo, "fetch", "--quiet", "origin")
    ahead = base_of(repo)
    assert git(repo, "rev-parse", "main").strip() != ahead

    line = sweep(repo)

    assert line["found_delta"]["base"] == ahead
    assert line["work"] == WORK and past_step_2c(line)


def test_the_base_branch_is_githubs_default_as_publish_reads_it(tmp_path, no_real_gh):
    """origin's default is `develop` (GitHub says so; origin/HEAD agrees):
    the sweep builds on origin/develop, the branch publish's pr_base will
    open the PR into, not on a local `main`."""
    repo = checkout(tmp_path, NO_BARE_PRINT, clean=True)
    (repo / "develop.py").write_text('print("develop")\n', encoding="utf-8")
    git(repo, "checkout", "--quiet", "-b", "develop")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "develop")
    git(repo, "push", "--quiet", "origin", "develop")
    git(tmp_path / "origin.git", "symbolic-ref", "HEAD", "refs/heads/develop")
    git(repo, "remote", "set-head", "origin", "develop")
    no_real_gh.feed(default_branch="develop")

    resolved = resolve_sweep_base(repo)
    line = sweep(repo)

    develop = git(repo, "rev-parse", "refs/remotes/origin/develop").strip()
    assert (resolved.gh_repo, resolved.default_branch, resolved.ref, resolved.sha) == (
        GH_REPO, "develop", "refs/remotes/origin/develop", develop)
    assert line["found_delta"]["base"] == develop != base_of(repo)
    views = [argv for argv in no_real_gh.calls() if argv[:2] == ["repo", "view"]]
    assert views[-1] == ["repo", "view", GH_REPO, "--json", "defaultBranchRef"]


def test_a_stale_origin_head_is_refused_before_any_sandbox(tmp_path, no_real_gh):
    """GitHub's default is `trunk`, the checkout's origin/HEAD still names
    `main`: publish would refuse the PR, so the sweep refuses to start."""
    repo = checkout(tmp_path, NO_BARE_PRINT)
    git(repo, "push", "--quiet", "origin", "main:refs/heads/trunk")
    git(repo, "fetch", "--quiet", "origin")
    no_real_gh.feed(default_branch="trunk")

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "git remote set-head origin -a" in line["message"]


def test_an_origin_that_is_not_on_github_is_refused(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    git(repo, "remote", "set-url", "origin", str(tmp_path / "origin.git"))

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert no_real_gh.calls() == []


def test_gh_failing_to_name_the_default_is_refused(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    no_real_gh.feed(fail_on={"repo view": "HTTP 502: Bad Gateway"})

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "HTTP 502" in line["message"]


# --- the rest of steps 1 to 2c ---------------------------------------------------------

def test_lint_work_with_no_open_prs_goes_on_to_the_fix_step(tmp_path, no_real_gh):
    """Past step 2b with tier 1 off: the fix step's line (here its failure
    to start), with the decision's fields, found_delta and the tier1 field,
    and the snapshot (step 2a). The sandbox is gone afterwards."""
    repo = checkout(tmp_path, NO_BARE_PRINT)
    parent = tmp_path / "sandboxes"
    parent.mkdir()

    line = sweep(repo, sandbox_parent=str(parent))

    assert past_step_2c(line)
    assert line["work"] == WORK and line["open_sweep_prs"] == []
    assert line["held_for_open_prs"] == 0
    assert line["found_delta"]["added_count"] == 2
    assert line["tier1"]["status"] == OFF
    assert no_cost_fields(line)
    assert read_snapshot(snapshot_path(repo))["base"] == base_of(repo)
    assert list(parent.iterdir()) == []


def test_a_second_sweep_reads_the_first_ones_snapshot(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    first = sweep(repo)
    second = sweep(repo)

    assert first["found_delta"]["baseline"] is True
    assert second["found_delta"]["baseline"] is False
    assert second["found_delta"]["previous_digest"] == first["found_delta"]["digest"]
    assert second["found_delta"]["added_count"] == 0


def test_a_scan_that_ran_notes_its_cost_and_field_on_the_line(tmp_path, no_real_gh):
    """A scan that found nothing, with lint work left: the line (the fix
    step's, which cannot start here) carries the scan's usage as its
    top-level counts."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    claude, log = fake_claude(tmp_path, "scan", findings_reply())

    line = sweep(repo, claude=claude)

    assert past_step_2c(line)
    assert line["tier1"]["status"] == SCANNED and line["tier1"]["scanned"] is True
    assert (line["input_tokens"], line["output_tokens"], line["model"], line["cost_usd"]) == (
        1200, 340, MODEL, 0.02)
    argv = json.loads(log.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == MODEL


def test_a_crash_after_the_scan_still_records_its_spend_and_attempt(tmp_path, no_real_gh,
                                                                    monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply())

    def boom(*_args):
        raise RuntimeError("after the scan")

    monkeypatch.setattr(sweep_job, "with_findings", boom)

    with pytest.raises(RuntimeError), JobRun(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(claude=claude))
    line = job_lines(repo)[-1]

    assert line["outcome"] == FAILURE and "after the scan" in line["message"]
    assert line["input_tokens"] == 1200 and line["model"] == MODEL
    assert line["tier1"]["status"] == SCANNED


def test_a_failed_scan_with_no_lint_work_is_a_failure_with_its_field(tmp_path, no_real_gh):
    """Step 2c's terminal decision: TIER1_FAILED, written by Decision.record
    as a failure with found_delta and the tier1 field; the snapshot follows."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=TIER1_ON, clean=True)
    claude, _ = fake_claude(tmp_path, "scan", "I could not finish.")

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and line["work"] == WORK
    # TIER1_FAILED is a decision kind, never written: its line says
    # `work: "work"` and is a failure (Decision.ledger_fields).
    assert TIER1_FAILED not in line.values()
    assert line["tier1"]["status"] == FAILED and "found_delta" in line
    assert line["input_tokens"] == 1200
    assert read_snapshot(snapshot_path(repo)) is not None


def test_a_clean_base_scanned_clean_is_no_work_with_its_cost(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=TIER1_ON, clean=True)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply())

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS and line["work"] == NO_WORK
    assert line["tier1"]["status"] == SCANNED
    assert (line["input_tokens"], line["output_tokens"], line["model"]) == (1200, 340, MODEL)


def test_an_untrusted_lint_is_a_lint_refusal(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    lint = tmp_path / "bin" / "broken-lint"
    lint.parent.mkdir(parents=True, exist_ok=True)
    lint.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(2)\n", encoding="utf-8")
    lint.chmod(0o755)

    line = sweep(repo, lint_options={"lint": lint})

    assert line["outcome"] == REFUSED and line["reason"] == Reason.LINT.value
    assert "found_delta" not in line and not snapshot_path(repo).exists()


def test_a_scope_check_error_before_the_plan_is_a_sandbox_refusal(tmp_path, no_real_gh,
                                                                  monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=TIER1_ON)

    def unreadable(_sandbox, _entries):
        from curamoir.fix_scope import ScopeCheckError
        raise ScopeCheckError("the principles at the base commit cannot be read")

    monkeypatch.setattr(sweep_job, "plan_scan", unreadable)

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SANDBOX.value
    assert "tier1" not in line and "found_delta" not in line


def test_an_unloadable_config_is_refused_like_the_gateway_refuses_it(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    (repo / ".curamoir" / "curamoir.yaml").write_text("sweep: [\n", encoding="utf-8")

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.GUARD_ERROR.value
    assert no_real_gh.calls() == []


def git_times_out(*_args, **_kwargs):
    raise GitGuardError("git cat-file did not finish within 60 seconds")


@pytest.mark.parametrize("broken, outcome, status, reason", [
    # Reading the base to check the reply: a FAILED scan, and the lint's
    # work goes on to the fix step.
    ("check_findings", FAILURE, FAILED, None),
    # Checking the sandbox is still its base: refused as changed.
    ("verify_git_state", REFUSED, SANDBOX_CHANGED, Reason.SANDBOX.value),
])
def test_a_git_failure_after_the_scan_call_keeps_its_spend_and_counts_as_an_attempt(
        tmp_path, no_real_gh, monkeypatch, broken, outcome, status, reason):
    """A git failure after the scan's model call ran still leaves the call's
    token counts and the tier1 field on the line: the budget prices the
    call (not $0), and the attempt counts, so after MAX_SCAN_ATTEMPTS
    such lines at one base the scan is not called again. Without both,
    every sweep would pay for the scan anew with nothing recorded."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    claude, log = fake_claude(tmp_path, "scan", findings_reply())
    monkeypatch.setattr(tier1, broken, git_times_out)

    first = sweep(repo, claude=claude)

    assert log.exists(), "the model call ran"
    assert first["outcome"] == outcome and first.get("reason") == reason
    assert "cat-file did not finish" in (first["tier1"]["reason"] or "")
    assert first["tier1"]["status"] == status
    assert (first["input_tokens"], first["output_tokens"], first["model"]) == (1200, 340, MODEL)
    spend = month_to_date(repo_ledger(repo), PRICES, datetime.now(timezone.utc))
    assert spend.usd == pytest.approx((1200 * 2.0 + 340 * 10.0) / 1_000_000)
    assert spend.unrecorded == 0 and spend.unaccounted == 0

    log.unlink()
    second = sweep(repo, claude=claude)
    assert log.exists() and second["tier1"]["status"] == status
    log.unlink()
    third = sweep(repo, claude=claude)
    assert not log.exists(), "two attempts at this base: the scan is not called again"
    assert third["tier1"]["status"] == ATTEMPTS_EXHAUSTED
    assert no_cost_fields(third)


def test_a_scan_whose_claude_cannot_start_spent_nothing(tmp_path, no_real_gh):
    """With no `claude` executable no call is made: the scan is FAILED (an
    attempt, never no_work), and the line carries no token counts, not
    UNRECORDED for a call that never happened."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)

    line = sweep(repo, claude=str(tmp_path / "no-such-claude"))

    assert past_step_2c(line)
    assert line["tier1"]["status"] == FAILED
    assert line["tier1"]["reason"].startswith("the tier-1 scan could not start")
    assert no_cost_fields(line)


def test_the_body_is_registered_as_the_sweep_job():
    from curamoir import runner

    assert runner.JOBS["sweep"].body is run_sweep
    assert runner.JOBS["sweep"].opens_prs is True


# --- the line's cost -------------------------------------------------------------------

#: claude_payloads.success's default `total_cost_usd`, what scan_of and the
#: fix steps built without a `cost` report.
DEFAULT_COST = 0.02


def scan_of(status, usage=claude_payloads.USAGE, cost=DEFAULT_COST):
    return Tier1Scan(status, "a" * 40, usage=None if usage is None else dict(usage),
                     cost_usd=cost)


def fix_run_of(payload):
    stdout = "Segmentation fault" if payload is None else json.dumps(payload)
    return FixStepRun(("claude",), 0 if payload else None, stdout, "",
                      timed_out=payload is None)


FIX_USAGE = {"input_tokens": 5000, "output_tokens": 800}

#: Every kind of scan: none, one that made no call, and called ones with
#: usage, without it, and with counts that are not numbers.
SCANS = {
    "no scan": None,
    "skipped": scan_of(OFF, usage=None, cost=None),
    "no candidates": scan_of(NO_CANDIDATES, usage=None, cost=None),
    "scanned": scan_of(SCANNED),
    "failed with usage": scan_of(FAILED),
    "sandbox changed": scan_of(SANDBOX_CHANGED),
    "timed out": scan_of(FAILED, usage=None, cost=None),
    "never started": Tier1Scan(FAILED, "a" * 40, started=False),
    "usage without output": scan_of(SCANNED, usage={"input_tokens": 10}),
    "string count": scan_of(SCANNED, usage={"input_tokens": "10", "output_tokens": 1}),
    "bool count": scan_of(SCANNED, usage={"input_tokens": True, "output_tokens": 1}),
    "huge count": scan_of(SCANNED, usage={"input_tokens": 10**400, "output_tokens": 1}),
    "huge cost": scan_of(SCANNED, cost=10**400),
    "inf count": scan_of(SCANNED, usage={"input_tokens": float("inf"), "output_tokens": 1}),
    "inf cost": scan_of(SCANNED, cost=float("inf")),
}

#: Every kind of fix step: not invoked, finished, crashed or timed out (no
#: payload), payload without usage, and counts that are not usable.
FIXES = {
    "no fix step": None,
    "fixed": fix_run_of(claude_payloads.success("done", cost=0.5, usage=FIX_USAGE)),
    "timed out": fix_run_of(None),
    "no usage": fix_run_of({**claude_payloads.success("done", cost=0.5), "usage": None}),
    "negative count": fix_run_of(claude_payloads.success(
        "done", usage={"input_tokens": -1, "output_tokens": 2})),
    "nan count": fix_run_of(claude_payloads.success(
        "done", usage={"input_tokens": float("nan"), "output_tokens": 2})),
    "no cost": fix_run_of({**claude_payloads.success("done", usage=FIX_USAGE),
                           "total_cost_usd": None}),
    "huge count": fix_run_of(claude_payloads.success(
        "done", usage={"input_tokens": 10**400, "output_tokens": 2})),
    "nan cost": fix_run_of({**claude_payloads.success("done", usage=FIX_USAGE),
                            "total_cost_usd": float("nan")}),
    "inf count": fix_run_of(claude_payloads.success(
        "done", usage={"input_tokens": float("inf"), "output_tokens": 2})),
    "inf cost": fix_run_of({**claude_payloads.success("done", usage=FIX_USAGE),
                            "total_cost_usd": float("inf")}),
}

_SCAN_TOKENS = {"scanned": (1200, 340), "failed with usage": (1200, 340),
                "sandbox changed": (1200, 340), "huge cost": (1200, 340),
                "inf cost": (1200, 340)}
_FIX_TOKENS = {"fixed": (5000, 800), "no cost": (5000, 800), "nan cost": (5000, 800),
               "inf cost": (5000, 800)}
_NO_CALL_SCANS = {"no scan", "skipped", "no candidates", "never started"}


def expected(scan_name, fix_name):
    """What fix_scope's "The line's cost" says, case by case."""
    scan_ran = scan_name not in _NO_CALL_SCANS
    fix_ran = fix_name != "no fix step"
    if not (scan_ran or fix_ran):
        return {}
    known = []
    if scan_ran:
        known.append(_SCAN_TOKENS.get(scan_name))
    if fix_ran:
        known.append(_FIX_TOKENS.get(fix_name))
    if None in known:
        fields = {"input_tokens": UNRECORDED, "output_tokens": UNRECORDED}
    else:
        fields = {"input_tokens": sum(k[0] for k in known),
                  "output_tokens": sum(k[1] for k in known)}
    fields["model"] = MODEL
    costs = []
    if scan_ran:
        costs.append(None if scan_name in ("timed out", "huge cost", "inf cost")
                     else DEFAULT_COST)
    if fix_ran:
        costs.append({"fixed": 0.5, "no usage": 0.5, "negative count": DEFAULT_COST,
                      "nan count": DEFAULT_COST, "huge count": DEFAULT_COST,
                      "inf count": DEFAULT_COST}.get(fix_name))
    if None not in costs:
        fields["cost_usd"] = sum(costs)
    return fields


@pytest.mark.parametrize("fix_name", list(FIXES))
@pytest.mark.parametrize("scan_name", list(SCANS))
def test_line_cost_fields_every_combination(scan_name, fix_name):
    got = line_cost_fields(MODEL, scan=SCANS[scan_name], fix_run=FIXES[fix_name])
    want = expected(scan_name, fix_name)
    assert got.keys() == want.keys()
    for key, value in want.items():
        if key == "cost_usd":
            assert got[key] == pytest.approx(value)
        else:
            assert got[key] == value


def test_line_cost_fields_sums_both_calls_and_their_cost():
    got = line_cost_fields(MODEL, scan=SCANS["scanned"], fix_run=FIXES["fixed"])

    assert got == {"input_tokens": 6200, "output_tokens": 1140, "model": MODEL,
                   "cost_usd": pytest.approx(0.52)}


def test_line_cost_fields_never_writes_zeros_for_no_call():
    assert line_cost_fields(MODEL) == {}
    assert line_cost_fields(MODEL, scan=SCANS["no candidates"]) == {}


def test_line_cost_fields_hedges_the_whole_line_when_one_call_is_unrecorded():
    """The other call's known tokens leave the total with it: one count
    per line cannot say "this much, and more"."""
    got = line_cost_fields(MODEL, scan=SCANS["timed out"], fix_run=FIXES["fixed"])

    assert got == {"input_tokens": UNRECORDED, "output_tokens": UNRECORDED, "model": MODEL}


def test_the_budget_counts_an_unrecorded_line_and_prices_a_summed_one(tmp_path):
    ledger = repo_ledger(tmp_path)
    ledger.record("sweep", None, "", **line_cost_fields(MODEL, scan=SCANS["timed out"]))
    ledger.record("sweep", None, "", **line_cost_fields(MODEL, scan=SCANS["scanned"],
                                                        fix_run=FIXES["fixed"]))
    ledger.record("sweep", None, "", **line_cost_fields(MODEL))

    spend = month_to_date(ledger, PRICES, datetime.now(timezone.utc))

    assert spend.unrecorded == 1 and spend.unaccounted == 0
    assert spend.usd == pytest.approx((6200 * 2.0 + 1140 * 10.0) / 1_000_000)


def test_line_cost_fields_takes_a_plan_as_no_call_and_refuses_anything_else():
    """The runner's tier1_state may still be the plan, which made no call.
    Anything that is not a scan, a plan or None is refused, not guessed
    at."""
    assert line_cost_fields(MODEL, scan=Tier1Plan(READY, "a" * 40)) == {}
    assert line_cost_fields(MODEL, scan=Tier1Plan(OFF, "a" * 40),
                            fix_run=FIXES["fixed"])["input_tokens"] == 5000
    with pytest.raises(TypeError):
        line_cost_fields(MODEL, scan={"status": SCANNED})
    with pytest.raises(TypeError):
        line_cost_fields(MODEL, fix_run={"usage": FIX_USAGE})


def test_line_cost_fields_adds_nothing_for_a_fix_step_that_never_started():
    never = FixStepRun(("claude",), None, "", f"{NOT_STARTED}ENOENT")
    assert not never.started and fix_run_of(None).started
    assert line_cost_fields(MODEL, fix_run=never) == {}
    assert line_cost_fields(MODEL, scan=SCANS["scanned"], fix_run=never)["input_tokens"] == 1200



# --- edge cases ------------------------------------------------------------------------

def attempts_accumulate(log, run_one):
    """Three sweeps (`run_one` runs one and returns its line): the first two
    call the scan, the third finds the attempts used up and does not. The
    lines of the first two."""
    lines = []
    for _ in range(2):
        lines.append(run_one())
        assert log.exists(), "the model call ran"
        log.unlink()
    third = run_one()
    assert not log.exists() and third["tier1"]["status"] == ATTEMPTS_EXHAUSTED
    assert no_cost_fields(third)
    return lines


def test_a_huge_token_count_is_unrecorded_never_a_crash(tmp_path, no_real_gh):
    """A count too large for a float (10**400) cannot be priced: it is
    UNRECORDED, the line keeps its tier1 field, and the attempt counts."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    usage = {**claude_payloads.USAGE, "input_tokens": 10**400}
    claude, log = fake_claude(tmp_path, "scan", None, payload=claude_payloads.success(
        findings_reply(), usage=usage, cost=10**400))

    [first, _second] = attempts_accumulate(log, lambda: sweep(repo, claude=claude))

    assert past_step_2c(first)
    assert first["input_tokens"] == first["output_tokens"] == UNRECORDED
    assert first["model"] == MODEL and "cost_usd" not in first
    assert first["tier1"]["status"] == SCANNED and first["tier1"]["cost_usd"] is None
    spend = month_to_date(repo_ledger(repo), PRICES, datetime.now(timezone.utc))
    assert spend.unrecorded == 2 and spend.unaccounted == 0


@pytest.mark.parametrize("stop", [Terminated(signal.SIGTERM), KeyboardInterrupt(),
                                  ValueError("a reply the checks choked on")],
                         ids=["Terminated", "KeyboardInterrupt", "ValueError"])
def test_a_run_that_ends_after_the_scan_call_records_unrecorded_spend_and_the_attempt(
        tmp_path, no_real_gh, monkeypatch, stop):
    """Whatever ends the run inside run_tier1_scan after the model call (a
    signal, ^C, an exception), the line carries what was noted before the
    call: UNRECORDED counts with the model, and a FAILED tier1 field of
    this base. The budget counts it as unrecorded, and the attempts add up
    until the scan is no longer called."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    claude, log = fake_claude(tmp_path, "scan", findings_reply())

    def ends_the_run(*_args, **_kwargs):
        raise stop

    monkeypatch.setattr(tier1, "check_findings", ends_the_run)

    def run_one():
        # The third sweep must not call the scan, so must not raise; caught
        # here all the same, so a regression fails on the asserts rather
        # than letting a KeyboardInterrupt stop the test session.
        with contextlib.suppress(type(stop)), JobRun(repo_ledger(repo), "sweep") as run:
            run_sweep(repo, run, SweepTools(claude=claude))
        return job_lines(repo)[-1]

    lines = attempts_accumulate(log, run_one)

    for line in lines:
        assert line["outcome"] == FAILURE
        assert line["input_tokens"] == line["output_tokens"] == UNRECORDED
        assert line["model"] == MODEL and "cost_usd" not in line
        assert line["tier1"]["status"] == FAILED and line["tier1"]["base"] == base_of(repo)
    spend = month_to_date(repo_ledger(repo), PRICES, datetime.now(timezone.utc))
    assert spend.unrecorded == 2 and spend.unaccounted == 0


def test_a_scan_with_no_candidates_drops_the_noted_cost(tmp_path, no_real_gh):
    """No file to scan (max_files_per_pr: 0): no call, so the counts noted
    before it are removed again."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE,
                    config=TIER1_ON + "  max_files_per_pr: 0\n")
    claude, log = fake_claude(tmp_path, "scan", findings_reply())

    line = sweep(repo, claude=claude)

    assert not log.exists()
    assert line["tier1"]["status"] == NO_CANDIDATES and no_cost_fields(line)


@pytest.mark.parametrize("name", ["main~1", "main^", "main@{1}"])
def test_a_default_branch_name_with_revision_syntax_is_refused(tmp_path, no_real_gh, name):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    git(repo, "remote", "set-head", "origin", "-d")
    no_real_gh.feed(default_branch=name)

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "not a valid branch name" in line["message"]


def test_gh_hanging_on_the_default_branch_is_refused(tmp_path, no_real_gh, monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    monkeypatch.setattr(github, "GH_TIMEOUT", 1)
    no_real_gh.feed(hang=True)

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "did not finish" in line["message"]


def test_a_listing_at_its_limit_is_an_idempotence_refusal(tmp_path, no_real_gh, monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    monkeypatch.setattr(github, "PR_LIMIT", 2)
    no_real_gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in (1, 2)])

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.IDEMPOTENCE.value
    assert "cut short" in line["message"]
    assert not snapshot_path(repo).exists()


def test_a_tracking_ref_that_is_not_a_commit_is_refused(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    git(repo, "update-ref", "refs/remotes/origin/main", tree)

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "names no commit" in line["message"]


def test_a_sandbox_that_cannot_be_removed_keeps_the_recorded_line(tmp_path, no_real_gh,
                                                                  monkeypatch):
    from curamoir import git_guard

    repo = checkout(tmp_path, NO_BARE_PRINT, clean=True)

    def cannot_remove(_sandbox):
        raise GitGuardError("the sandbox could not be removed")

    monkeypatch.setattr(git_guard.Sandbox, "cleanup", cannot_remove)

    with pytest.warns(RuntimeWarning, match="not removed"):
        line = sweep(repo)

    assert line["outcome"] == SUCCESS and line["work"] == NO_WORK
    assert read_snapshot(snapshot_path(repo)) is not None


def test_an_interrupt_still_removes_the_sandbox(tmp_path, no_real_gh, monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    parent = tmp_path / "sandboxes"
    parent.mkdir()

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(sweep_job, "lint_sandbox", interrupted)

    with pytest.raises(KeyboardInterrupt), JobRun(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(sandbox_parent=str(parent)))

    assert list(parent.iterdir()) == []
    assert job_lines(repo)[-1]["outcome"] == FAILURE
    assert not snapshot_path(repo).exists()


def test_a_corrupt_snapshot_is_read_as_a_baseline_and_replaced(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    snapshot_path(repo).parent.mkdir(parents=True, exist_ok=True)
    snapshot_path(repo).write_text("{not json", encoding="utf-8")

    line = sweep(repo)

    assert line["found_delta"]["baseline"] is True
    assert read_snapshot(snapshot_path(repo))["base"] == base_of(repo)


def test_an_origin_with_two_urls_is_refused(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    git(repo, "remote", "set-url", "--add", "origin", "https://github.com/acme/other.git")

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "more than one push URL" in line["message"]


def test_a_dangling_origin_head_is_refused(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SWEEP_BASE.value
    assert "set-head" in line["message"]


@pytest.mark.parametrize("broken, error, outcome", [
    ("scan_candidates", GitGuardError("git ls-files did not finish within 60 seconds"),
     REFUSED),
    ("scan_prompt", ValueError("a prompt that cannot be built"), FAILURE),
])
def test_a_scan_that_fails_before_its_call_records_no_spend_and_no_attempt(
        tmp_path, no_real_gh, monkeypatch, broken, error, outcome):
    """The cost is noted immediately before the `claude` process starts, not
    before run_tier1_scan: a failure while choosing the candidates or
    building the prompt made no call, so the line carries no counts and no
    tier1 attempt, and tier 1 is not used up at this base."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE, config=TIER1_ON)
    claude, log = fake_claude(tmp_path, "scan", findings_reply())

    def fails(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(tier1, broken, fails)

    with contextlib.suppress(type(error)), JobRun(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(claude=claude))
    line = job_lines(repo)[-1]

    assert not log.exists(), "no model call"
    assert line["outcome"] == outcome
    if outcome == REFUSED:
        assert line["reason"] == Reason.SANDBOX.value
    assert no_cost_fields(line) and "tier1" not in line
    assert tier1.scan_attempts(repo_ledger(repo).entries(), base_of(repo)) == 0



def test_a_refusal_after_the_plan_carries_its_tier1_field(tmp_path, no_real_gh, monkeypatch):
    """A refusal from step 2b (iii) on carries the tier1 field, here the
    skipped plan's (tier 1 off), so the line says tier 1 did not run."""
    from curamoir import idempotence

    repo = checkout(tmp_path, NO_BARE_PRINT)

    def sandbox_gone(_decision, _sandbox):
        raise GitGuardError("the sandbox's git files changed")

    monkeypatch.setattr(idempotence.Decision, "apply_to", sandbox_gone)

    line = sweep(repo)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SANDBOX.value
    assert line["tier1"]["status"] == OFF


BOTS = TIER1_OFF + "guardrails:\n  branch_prefix: bots/\n"


@pytest.mark.parametrize("prefix, duplicate", [("bots/", True), ("curamoir/", False)])
def test_the_duplicate_check_follows_the_configured_branch_prefix(tmp_path, no_real_gh,
                                                                   prefix, duplicate):
    """With `guardrails.branch_prefix: bots/`, a sweep PR is one on
    `bots/sweep-...`: one from this base is a duplicate, and a PR on the
    default `curamoir/` prefix is not cúramóir's at all."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=BOTS)
    branch = f"{prefix}sweep-2026-10-20-{base_of(repo)[:8]}"
    no_real_gh.feed(prs=[as_sweep_pr(SAME_REPOSITORY_PR, branch, base_of(repo), number=7)])

    line = sweep(repo)

    if duplicate:
        assert line["outcome"] == SUCCESS and line["work"] == DUPLICATE_OPEN_PR
    else:
        assert past_step_2c(line)
        assert line["work"] == WORK and line["open_sweep_prs"] == []


# --- cache tokens on the line (cm-2ui) -------------------------------------------------

def test_line_cost_fields_sums_both_calls_cache_tokens():
    scan = scan_of(SCANNED, usage={"input_tokens": 10, "output_tokens": 1,
                                   "cache_creation_input_tokens": 100,
                                   "cache_read_input_tokens": 1000})
    fix = fix_run_of(claude_payloads.success("done", usage={
        "input_tokens": 20, "output_tokens": 2, "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 2000}))

    got = line_cost_fields(MODEL, scan=scan, fix_run=fix)

    assert (got["input_tokens"], got["output_tokens"]) == (30, 3)
    assert got["cache_creation_input_tokens"] == 300
    assert got["cache_read_input_tokens"] == 3000


def test_line_cost_fields_leaves_out_cache_counts_that_are_zero_or_absent():
    """A line from a call that used no cache reads as lines always have."""
    fix = fix_run_of(claude_payloads.success("done", usage={
        "input_tokens": 20, "output_tokens": 2}))

    got = line_cost_fields(MODEL, fix_run=fix)

    assert "cache_creation_input_tokens" not in got and "cache_read_input_tokens" not in got


@pytest.mark.parametrize("value", [-1, "many", float("nan"), True])
def test_a_cache_count_that_is_not_usable_makes_the_line_unrecorded(value):
    fix = fix_run_of(claude_payloads.success("done", usage={
        "input_tokens": 20, "output_tokens": 2, "cache_read_input_tokens": value}))

    got = line_cost_fields(MODEL, fix_run=fix)

    assert got["input_tokens"] == UNRECORDED and got["output_tokens"] == UNRECORDED
    assert "cache_read_input_tokens" not in got
