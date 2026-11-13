"""E14.1 (cm-2qp.1): the runner kind, declared by CURAMOIR_RUNNER and never
inferred (docs/direction-platform-first.md §3, §5 R2, §8 acceptance 3 and 4).

`config.resolve_runner` is the one place the variable is read, from an
environment the caller passes in; `job_guard.run_guarded` resolves it once
per run (from `environ`, os.environ when not given), notes `runner` (and the
transcript link) on the job line, and refuses a declaration it cannot honour
with reason `runner`, after the kill switch and the lock, as an invalid
config is refused. Every environment here is a plain dict handed in, so no
test depends on the variables of the shell that runs it.

The local pin (§8 acceptance 3) is
test_with_no_runner_variable_the_brief_line_is_the_pre_e14_line_plus_runner_local
here and, for the sweep, tests/test_sweep_acceptance.py's
test_with_no_runner_variable_the_sweep_is_the_pre_e14_sweep_plus_runner_local.
"""

import pytest

from curamoir import job_guard
from curamoir.config import (
    LOCAL_RUNNER,
    Runner,
    RunnerDeclarationError,
    RunnerKind,
    resolve_runner,
    session_transcript,
)
from curamoir.guards import KillSwitch, Reason
from curamoir.job_guard import run_guarded
from curamoir.job_run import JobRun
from curamoir.runner import EXIT_REFUSED, EXIT_SUCCESS, run_job
from curamoir.scaffold import init_repo
from tests.test_job_guard import listing, october
from tests.test_runner import guard_lines, hold_a_live_lock, job_lines

#: A GitHub-hosted Actions runner, as GitHub sets it up.
GITHUB = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}
ROUTINE = {"CLAUDE_CODE_REMOTE": "true"}
SESSION = "cse_01AbCdEf-gh_23"
SESSION_URL = "https://claude.ai/code/session_01AbCdEf-gh_23"


@pytest.fixture
def repo(harness):
    """The harness repo, initialised as `/curamoir:init` leaves it (as
    tests/test_runner.py's `repo`)."""
    init_repo(harness.repo)
    return harness


class Spy:
    """A body that records the Runner its run was given."""

    def __init__(self):
        self.runners = []

    def __call__(self, run):
        self.runners.append(run.runner)
        run.succeeded("ran")
        return "ran"


def guarded(harness, environ, body=None):
    """run_guarded for the brief over the harness repo, in `environ`; the
    GuardedRun, the body, and the run's job line."""
    body = body or Spy()
    result = run_guarded(harness.repo, "brief", None, body, now=october,
                         opens_prs=False, environ=environ)
    return result, body, job_lines(harness)[-1]


# --- resolve_runner: the rules -----------------------------------------------------------

@pytest.mark.parametrize("environ", [{}, {"CURAMOIR_RUNNER": ""}, {"CURAMOIR_RUNNER": "local"}])
def test_no_variable_an_empty_one_or_local_resolves_local(environ):
    assert resolve_runner(environ).kind is RunnerKind.LOCAL


@pytest.mark.parametrize("marker", [GITHUB, ROUTINE, {**GITHUB, **ROUTINE}])
def test_hosted_with_a_hosted_marker_resolves_hosted(marker):
    assert resolve_runner({"CURAMOIR_RUNNER": "hosted", **marker}).kind is RunnerKind.HOSTED


@pytest.mark.parametrize("marker", [GITHUB, ROUTINE])
def test_a_marker_without_the_variable_is_still_local(marker):
    """Declared, never inferred: a GitHub runner or a cloud session that did
    not say `hosted` runs every local guard."""
    assert resolve_runner(marker).kind is RunnerKind.LOCAL
    assert resolve_runner({"CURAMOIR_RUNNER": "local", **marker}).kind is RunnerKind.LOCAL


@pytest.mark.parametrize("markers", [
    {},
    {"GITHUB_ACTIONS": "True"},
    {"GITHUB_ACTIONS": "1"},
    {"GITHUB_ACTIONS": "true "},
    {"CLAUDE_CODE_REMOTE": "yes"},
    {"GITHUB_ACTIONS": "", "CLAUDE_CODE_REMOTE": "false"},
])
def test_hosted_without_a_marker_that_is_exactly_true_is_refused_not_downgraded(markers):
    with pytest.raises(RunnerDeclarationError) as refused:
        resolve_runner({"CURAMOIR_RUNNER": "hosted", **markers})

    message = str(refused.value)
    assert "CURAMOIR_RUNNER=hosted" in message
    assert "GITHUB_ACTIONS=true" in message and "CLAUDE_CODE_REMOTE=true" in message


@pytest.mark.parametrize("value", ["Hosted", "HOSTED", "ci", "github", " hosted", "local ",
                                   "routine"])
def test_any_other_value_is_refused_naming_the_accepted_values(value):
    with pytest.raises(RunnerDeclarationError) as refused:
        resolve_runner({"CURAMOIR_RUNNER": value, **GITHUB, **ROUTINE})

    message = str(refused.value)
    assert "'hosted'" in message and "'local'" in message
    assert f"{len(value)} characters" in message


#: Secrets a developer might paste into the variable by mistake: a 39-character
#: Google API key and a three-part JWT. Clipped, either slips past redaction's
#: patterns, so the refusal never echoes the value at all.
GOOGLE_KEY = "AIza" + "SyD3x9kQ2mN7pL4vR8tW1zY6bC5hJ0gFe2u"
JWT = ("eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "."
       + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")


def raw_runs(value, length=8):
    """Every run of `length` characters of `value`."""
    return {value[i:i + length] for i in range(len(value) - length + 1)}


@pytest.mark.parametrize("secret", [GOOGLE_KEY, JWT])
def test_an_unknown_value_is_never_echoed_on_the_job_or_guard_line(harness, secret):
    assert len(GOOGLE_KEY) == 39

    result, body, line = guarded(harness, {"CURAMOIR_RUNNER": secret})

    assert (result.reason, body.runners) == (Reason.RUNNER, [])
    [guard] = guard_lines(harness)
    for text in (line["message"], guard["message"]):
        assert not any(run in text for run in raw_runs(secret)), text
        assert f"{len(secret)} characters" in text


def test_a_value_that_is_not_text_is_refused_not_raised():
    with pytest.raises(RunnerDeclarationError) as refused:
        resolve_runner({"CURAMOIR_RUNNER": b"hosted", **ROUTINE})

    assert "not text" in str(refused.value)


def test_markers_and_a_session_id_that_are_not_text_do_not_raise():
    runner = resolve_runner({"CURAMOIR_RUNNER": "local", "GITHUB_ACTIONS": b"true",
                             "CLAUDE_CODE_REMOTE_SESSION_ID": b"cse_abc"})

    assert runner.kind is RunnerKind.LOCAL
    assert runner.transcript_unavailable == "the session id is not text"
    with pytest.raises(RunnerDeclarationError):
        resolve_runner({"CURAMOIR_RUNNER": "hosted", "GITHUB_ACTIONS": b"true",
                        "RUNNER_ENVIRONMENT": "github-hosted"})


# --- GitHub runners: only GitHub-hosted ones are hosted ------------------------------------

def test_a_github_hosted_runner_resolves_hosted():
    assert resolve_runner({"CURAMOIR_RUNNER": "hosted", **GITHUB}).kind is RunnerKind.HOSTED


@pytest.mark.parametrize("environment", [{"RUNNER_ENVIRONMENT": "self-hosted"}, {},
                                         {"RUNNER_ENVIRONMENT": "GitHub-Hosted"},
                                         {"RUNNER_ENVIRONMENT": ""}])
def test_a_github_runner_that_is_not_github_hosted_is_refused(environment):
    """Self-hosted runners and nektos/act set GITHUB_ACTIONS=true too, and
    are not disposable VMs (§3): they run as local."""
    with pytest.raises(RunnerDeclarationError) as refused:
        resolve_runner({"CURAMOIR_RUNNER": "hosted", "GITHUB_ACTIONS": "true",
                        **environment})

    message = str(refused.value)
    assert "RUNNER_ENVIRONMENT" in message and "self-hosted" in message
    assert "unset CURAMOIR_RUNNER" in message


@pytest.mark.parametrize("environment", [{"RUNNER_ENVIRONMENT": "self-hosted"}, {}])
def test_a_claude_code_session_is_hosted_whatever_runner_environment_says(environment):
    environ = {"CURAMOIR_RUNNER": "hosted", "GITHUB_ACTIONS": "true", **ROUTINE, **environment}

    assert resolve_runner(environ).kind is RunnerKind.HOSTED


def test_a_self_hosted_runner_is_refused_on_the_job_line(harness):
    result, body, line = guarded(harness, {"CURAMOIR_RUNNER": "hosted", "GITHUB_ACTIONS": "true",
                                           "RUNNER_ENVIRONMENT": "self-hosted"})

    assert (result.reason, body.runners) == (Reason.RUNNER, [])
    assert "RUNNER_ENVIRONMENT" in line["message"] and "runner" not in line


def test_only_a_hosted_runner_is_hosted():
    assert Runner(RunnerKind.HOSTED).hosted is True
    assert Runner(RunnerKind.LOCAL).hosted is False and LOCAL_RUNNER.hosted is False


def test_local_runner_is_local_with_no_transcript():
    assert LOCAL_RUNNER.kind is RunnerKind.LOCAL and LOCAL_RUNNER.transcript_url is None
    assert LOCAL_RUNNER.ledger_fields() == {"runner": "local"}


# --- session_transcript: the link ------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "url"), [
    ("cse_abc123", "https://claude.ai/code/session_abc123"),
    (SESSION, SESSION_URL),
    # Already in the link's own spelling: used as it is.
    ("session_abc123", "https://claude.ai/code/session_abc123"),
])
def test_the_session_id_becomes_the_transcript_link(value, url):
    assert session_transcript({"CLAUDE_CODE_REMOTE_SESSION_ID": value})[0] == url


@pytest.mark.parametrize("value", [
    None, "",
    # No known prefix: the link would be a guess, so there is none.
    "abc123", "cse-abc", "CSE_abc", "sess_abc",
    # A prefix and nothing after it.
    "cse_", "session_",
    # Not a plain token: no path, query, space, markup or line break.
    "cse_abc/../x", "cse_abc?x=1", "cse_a b", "cse_<b>", "cse_abc\n## forged",
    "cse_abc#x", "cse_ab%2F", "cse_abc‮",
    # Longer than any session id.
    "cse_" + "a" * 300,
])
def test_a_missing_or_invalid_session_id_gives_no_link(value):
    environ = {} if value is None else {"CLAUDE_CODE_REMOTE_SESSION_ID": value}
    assert session_transcript(environ)[0] is None


def test_a_session_id_that_redaction_would_change_gives_no_link():
    """The link goes into a PR body that is refused whole if redaction would
    change it (sweep_pr._check_redacted): a token shaped like a key is
    dropped here instead."""
    assert session_transcript({"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_sk-ant-" + "a" * 20})[0] is None


def test_the_runner_carries_the_transcript_link_whatever_its_kind():
    local = resolve_runner({"CLAUDE_CODE_REMOTE_SESSION_ID": SESSION})
    hosted = resolve_runner({"CURAMOIR_RUNNER": "hosted", **ROUTINE,
                             "CLAUDE_CODE_REMOTE_SESSION_ID": SESSION})

    assert (local.kind, local.transcript_url) == (RunnerKind.LOCAL, SESSION_URL)
    assert hosted.ledger_fields() == {"runner": "hosted", "transcript": SESSION_URL}


# --- run_guarded: once per run, on the job line ------------------------------------------

def test_no_variable_runs_local_and_the_body_is_given_the_local_runner(harness):
    result, body, line = guarded(harness, {})

    assert result.outcome == "success"
    [runner] = body.runners
    assert runner.kind is RunnerKind.LOCAL
    assert line["runner"] == "local"
    assert "transcript" not in line


@pytest.mark.parametrize("marker", [GITHUB, ROUTINE])
def test_hosted_with_a_marker_runs_hosted_and_says_so_on_the_line(harness, marker):
    result, body, line = guarded(harness, {"CURAMOIR_RUNNER": "hosted", **marker})

    assert result.outcome == "success"
    [runner] = body.runners
    assert runner.kind is RunnerKind.HOSTED
    assert line["runner"] == "hosted"


def test_a_marker_without_the_variable_runs_local(harness):
    _result, body, line = guarded(harness, {**GITHUB, **ROUTINE})

    assert body.runners[0].kind is RunnerKind.LOCAL
    assert line["runner"] == "local"


def test_hosted_without_a_marker_is_refused_with_the_reason_on_the_job_line(harness):
    result, body, line = guarded(harness, {"CURAMOIR_RUNNER": "hosted"})

    assert body.runners == []
    assert (result.outcome, result.reason) == ("refused", Reason.RUNNER)
    assert (line["outcome"], line["reason"]) == ("refused", "runner")
    assert "CURAMOIR_RUNNER=hosted" in line["message"]
    assert "GITHUB_ACTIONS=true" in line["message"]
    assert "CLAUDE_CODE_REMOTE=true" in line["message"]
    # It did not resolve, so the line claims no kind; and never a sync.
    assert "runner" not in line and "sync" not in line
    # The gateway's guard line too, as for every refusal.
    assert [(g["reason"], g["severity"]) for g in guard_lines(harness)] == [
        ("runner", "refused")]


@pytest.mark.parametrize("value", ["Hosted", "ci", "github"])
def test_an_unknown_value_is_refused_with_the_reason_on_the_job_line(harness, value):
    result, body, line = guarded(harness, {"CURAMOIR_RUNNER": value, **GITHUB})

    assert body.runners == []
    assert (result.outcome, line["outcome"], line["reason"]) == ("refused", "refused", "runner")
    assert "'hosted'" in line["message"]
    assert "runner" not in line


def test_a_paused_repo_with_a_bad_declaration_is_refused_as_paused(harness):
    """The kill switch is first in E10-R1's order, whatever else is wrong."""
    harness.write(".curamoir/PAUSE", "")

    result, body, line = guarded(harness, {"CURAMOIR_RUNNER": "hosted"})

    assert body.runners == []
    assert (result.reason, line["reason"]) == (Reason.PAUSED, "paused")


def test_a_bad_declaration_is_refused_before_an_invalid_config(harness):
    harness.write(".curamoir/curamoir.yaml", "version: 1\nbudget: [unclosed\n")

    result, _body, line = guarded(harness, {"CURAMOIR_RUNNER": "ci"})

    assert (result.reason, line["reason"]) == (Reason.RUNNER, "runner")


def test_a_bad_declaration_is_refused_after_the_kill_switch_and_the_lock(harness, monkeypatch):
    """Where an invalid config is refused: the lock is taken (and released),
    the budget never runs."""
    steps = []

    def spy(cls, name, label):
        original = getattr(cls, name)

        def wrapped(self, *args, **kwargs):
            steps.append(label)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(cls, name, wrapped)

    spy(KillSwitch, "__call__", "kill switch")
    spy(job_guard.LockSlot, "__call__", "lock")
    spy(job_guard.LockSlot, "release", "lock release")
    monkeypatch.setattr(job_guard, "Budget", lambda **_: steps.append("budget"))

    guarded(harness, {"CURAMOIR_RUNNER": "hosted"})

    assert steps == ["kill switch", "lock", "lock release"]
    assert not (harness.repo / ".curamoir" / "state" / "lock").exists()


def test_the_session_link_is_on_the_job_line(harness):
    _result, body, line = guarded(harness, {"CURAMOIR_RUNNER": "hosted", **ROUTINE,
                                            "CLAUDE_CODE_REMOTE_SESSION_ID": SESSION})

    assert line["transcript"] == SESSION_URL
    assert body.runners[0].transcript_url == SESSION_URL


def test_the_session_link_is_on_a_refused_line_too(harness):
    """A refused Routine run is the one whose transcript is most wanted."""
    _result, _body, line = guarded(harness, {"CURAMOIR_RUNNER": "hosted",
                                             "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_abc123"})

    assert line["reason"] == "runner"
    assert line["transcript"] == "https://claude.ai/code/session_abc123"


def test_an_invalid_session_id_says_the_link_is_unavailable_on_the_line(harness):
    _result, body, line = guarded(harness, {"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_a/b"})

    assert line["transcript"] == ("unavailable (the session id is not a plain token of 1 to "
                                  "128 letters, digits, '_' or '-')")
    assert "a/b" not in line["transcript"]
    assert body.runners[0].transcript_url is None


def test_a_refused_run_with_an_invalid_session_id_says_so_too(harness):
    _result, _body, line = guarded(harness, {"CURAMOIR_RUNNER": "ci",
                                             "CLAUDE_CODE_REMOTE_SESSION_ID": "abc"})

    assert line["transcript"].startswith("unavailable (the session id starts with neither")


@pytest.mark.parametrize(("value", "why"), [
    ("abc123", "starts with neither 'cse_' nor 'session_'"),
    ("cse_" + "a" * 300, "not a plain token"),
    ("cse_a b", "not a plain token"),
    ("cse_sk-ant-" + "a" * 20, "looks like a secret"),
])
def test_why_a_session_id_gives_no_link(value, why):
    link, unavailable = session_transcript({"CLAUDE_CODE_REMOTE_SESSION_ID": value})

    assert link is None and why in unavailable
    assert value not in unavailable


def test_no_session_id_gives_no_transcript_field_at_all():
    assert session_transcript({}) == (None, None)
    assert session_transcript({"CLAUDE_CODE_REMOTE_SESSION_ID": ""}) == (None, None)


# --- the runner kind's fields are the run's own ------------------------------------------

@pytest.mark.parametrize("field", ["runner", "transcript"])
def test_a_body_cannot_overwrite_the_runner_kind_fields(harness, field):
    attempts = []

    def body(run):
        for write in (lambda: run.note(**{field: "hosted"}),
                      lambda: run.succeeded("ran", **{field: "hosted"})):
            with pytest.raises(ValueError, match=field):
                write()
            attempts.append(field)
        run.succeeded("ran")

    run_guarded(harness.repo, "brief", None, body, now=october, opens_prs=False,
                environ={"CLAUDE_CODE_REMOTE_SESSION_ID": SESSION})

    line = job_lines(harness)[-1]
    assert attempts == [field, field]
    assert (line["runner"], line["transcript"]) == ("local", SESSION_URL)


def test_a_body_that_crashes_on_hosted_still_writes_runner_and_transcript(harness):
    def body(run):
        raise RuntimeError("the fix step fell over")

    with pytest.raises(RuntimeError):
        run_guarded(harness.repo, "brief", None, body, now=october, opens_prs=False,
                    environ={"CURAMOIR_RUNNER": "hosted", **ROUTINE,
                             "CLAUDE_CODE_REMOTE_SESSION_ID": SESSION})

    line = job_lines(harness)[-1]
    assert line["outcome"] == "failure"
    assert (line["runner"], line["transcript"]) == ("hosted", SESSION_URL)


def test_a_held_lock_with_a_bad_declaration_is_refused_as_locked(harness):
    """The lock is before the runner kind's check: another run holding it
    is what this run reports, and the body does not run."""
    held = hold_a_live_lock(harness)
    try:
        result, body, line = guarded(harness, {"CURAMOIR_RUNNER": "hosted"})
    finally:
        held.release()

    assert body.runners == []
    assert (result.reason, line["reason"]) == (Reason.LOCKED, "locked")


def test_the_environment_is_read_once_as_the_run_starts(harness):
    """A body that changes the variable cannot change what its run is."""
    environ = {"CURAMOIR_RUNNER": "hosted", **GITHUB}
    seen = []

    def body(run):
        environ["CURAMOIR_RUNNER"] = "ci"
        seen.append(run.runner.kind)
        run.succeeded("ran")

    run_guarded(harness.repo, "brief", None, body, now=october, opens_prs=False,
                environ=environ)

    assert seen == [RunnerKind.HOSTED]
    assert job_lines(harness)[-1]["runner"] == "hosted"


def test_run_guarded_reads_the_process_environment_when_given_none(harness, monkeypatch):
    monkeypatch.setenv("CURAMOIR_RUNNER", "hosted")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")

    body = Spy()
    run_guarded(harness.repo, "brief", None, body, now=october, opens_prs=False)

    assert body.runners[0].kind is RunnerKind.HOSTED


def test_a_job_run_made_outside_run_guarded_is_local():
    """Local is the hardened path, so it is what a JobRun nobody resolved a
    runner for gives its body (tests, and any caller outside run_guarded)."""
    assert JobRun(ledger=None, job="sweep").runner is LOCAL_RUNNER


# --- run_job: the entry point --------------------------------------------------------------

def test_run_job_refuses_a_hosted_claim_with_no_marker_in_one_sentence(repo):
    ran = run_job(repo.repo, "brief", now=october, environ={"CURAMOIR_RUNNER": "hosted"})

    assert ran.exit_code == EXIT_REFUSED
    assert ran.sentence.startswith("cúramóir ran 'brief': refused (runner).")
    assert "GITHUB_ACTIONS=true" in ran.sentence
    assert not (repo.repo / ".curamoir" / "briefs" / "latest.md").exists()
    assert job_lines(repo)[-1]["reason"] == "runner"


def test_run_job_runs_hosted_with_a_marker(repo):
    ran = run_job(repo.repo, "sweep", now=october, open_pr_listing=listing(),
                  environ={"CURAMOIR_RUNNER": "hosted", **GITHUB})

    # No origin to sweep from, so the sweep's own refusal: past the runner check.
    assert job_lines(repo)[-1]["reason"] == "sweep_base"
    assert job_lines(repo)[-1]["runner"] == "hosted"
    assert ran.exit_code == EXIT_REFUSED


# --- §8 acceptance 3: the local path is unchanged ----------------------------------------

#: The brief's job line as commit 3f50a3f (and this branch before E14.1)
#: writes it through `curamoir run brief` on the fixed clock, less `at` and
#: `run_id` (the wall clock's and a random id). Captured from that code.
PRE_E14_BRIEF_LINE = {
    "ended": "2026-10-05T00:00:00+00:00",
    "job": "brief",
    "kind": "job",
    "message": "wrote .curamoir/briefs/latest.md",
    "outcome": "success",
    "reason": None,
    "started": "2026-10-05T00:00:00+00:00",
    "sync": "skipped",
    "sync_reason": ("sync is not built yet (cm-eqp.3), so the job ran on the checkout as it "
                    "stood, unsynced"),
}


def test_with_no_runner_variable_the_brief_line_is_the_pre_e14_line_plus_runner_local(repo):
    ran = run_job(repo.repo, "brief", now=october, environ={})

    assert ran.exit_code == EXIT_SUCCESS
    assert ran.sentence == "cúramóir ran 'brief': success. wrote .curamoir/briefs/latest.md."
    line = job_lines(repo)[-1]
    assert {key: value for key, value in line.items() if key not in ("at", "run_id")} == {
        **PRE_E14_BRIEF_LINE, "runner": "local"}
    assert guard_lines(repo) == []
