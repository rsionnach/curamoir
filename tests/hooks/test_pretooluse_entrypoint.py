"""The hook as Claude Code actually invokes it: a subprocess, JSON on stdin.

Verified against the current hook contract (DEC-5):
  input   {"hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {"command": ...}, "cwd": ...}
  block   {"hookSpecificOutput": {"hookEventName": "PreToolUse",
           "permissionDecision": "deny", "permissionDecisionReason": ...}}
  allow   exit 0 with no decision — normal permission flow applies.

permissionDecisionReason is the channel the message reaches the agent through,
which is why E0-R5's "name the safe alternative" lands there.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_hook(command, env=None, cwd=None, tool_name="Bash"):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "cwd": str(cwd or REPO_ROOT),
    }
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    environment.pop("CURAMOIR_JOB", None)
    environment.update(env or {})
    result = subprocess.run(
        [sys.executable, "-m", "curamoir.hooks.pretooluse"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(REPO_ROOT),
    )
    decision = json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None
    return result, decision


def test_a_destructive_command_inside_a_job_is_denied_with_a_reason():
    result, decision = run_hook("git reset --hard", env={"CURAMOIR_JOB": "sweep"})

    assert result.returncode == 0, result.stderr
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "stash" in decision["permissionDecisionReason"]


def test_an_ordinary_command_produces_no_decision():
    result, decision = run_hook("git status", env={"CURAMOIR_JOB": "sweep"})

    assert result.returncode == 0, result.stderr
    assert decision is None


def test_outside_a_job_nothing_is_denied():
    result, decision = run_hook("git reset --hard")

    assert result.returncode == 0, result.stderr
    assert decision is None


def test_the_supervisor_lock_is_the_fallback_when_env_is_scrubbed(tmp_path):
    """CLAUDE_CODE_SUBPROCESS_ENV_SCRUB can strip CURAMOIR_JOB, so the lock the
    runner already maintains (E0-R2) is the second signal. Without it a scrubbed
    environment would silently stop enforcing."""
    (tmp_path / ".curamoir" / "state").mkdir(parents=True)
    (tmp_path / ".curamoir" / "state" / "lock").write_text("{}", encoding="utf-8")

    result, decision = run_hook("rm -rf build/", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert decision["permissionDecision"] == "deny"


def test_a_non_bash_tool_is_ignored():
    _, decision = run_hook("irrelevant", env={"CURAMOIR_JOB": "sweep"}, tool_name="Read")

    assert decision is None


def test_malformed_input_does_not_crash_the_hook():
    """A hook that raises adds noise to every tool call and, because hooks fail
    open, protects nothing anyway. Exit clean and let normal flow apply."""
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "curamoir.hooks.pretooluse"],
        input="this is not json",
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr


def test_the_hook_applies_the_allowlist_not_only_the_denylist():
    """DEC-9. The three holes three denylist rewrites could not close must be
    refused through the real hook, not merely in the policy unit tests."""
    for command in ("bash -lc 'rm -rf /'", "echo $(rm -rf $(pwd))", "cat <(rm -rf /tmp/x)"):
        result, decision = run_hook(command, env={"CURAMOIR_JOB": "sweep"})
        assert result.returncode == 0, result.stderr
        assert decision is not None, f"NOT REFUSED THROUGH THE HOOK: {command}"
        assert decision["permissionDecision"] == "deny"


def test_the_hook_still_lets_a_job_do_its_work():
    for command in ("git status --porcelain", "git add -A", "pytest -q",
                    "bd list --json", "gh pr create --title x --body y"):
        result, decision = run_hook(command, env={"CURAMOIR_JOB": "sweep"})
        assert result.returncode == 0, result.stderr
        assert decision is None, f"WRONGLY REFUSED THROUGH THE HOOK: {command}"


def test_every_refusal_that_reaches_the_agent_names_something_to_do():
    """E0-R5 makes "name the safe alternative" an acceptance criterion, and the
    spec's reason is that an agent told only "no" retries the same thing or
    invents worse.

    `Decision.alternative` had no reader: the hook emitted only `reason`.
    `destructive.py` folds its alternative into `reason` itself, so the denylist
    criterion held — but the length cap, the unparseable case and the
    unterminated heredoc each set `alternative` and never mentioned it, so
    those three reached the agent as a refusal with no next step.
    """
    from curamoir.hooks import judge_command
    from curamoir.hooks.pretooluse import refusal_text

    commands = [
        "echo " + "a" * 20_000,                  # the length cap
        'git push -f "unclosed',                 # unparseable
        "cat <<EOF\nrm -rf /",                   # unterminated heredoc
        "terraform apply",                       # not allowlisted
        "git reset --hard",                      # destructive
        "bash -c 'rm -rf /'",                    # unverifiable
    ]
    for command in commands:
        decision = judge_command(command, in_job=True)
        assert decision.deny, command
        text = refusal_text(decision)
        assert decision.alternative, f"{command!r} sets no alternative"
        assert decision.alternative in text, (
            f"the agent is told {text!r} and never told what to do instead"
        )


def test_the_lock_the_runner_writes_makes_the_fallback_fire(harness, monkeypatch):
    """cm-x5a.6: the fallback above, from the lock `curamoir run` itself
    holds rather than a hand-written one. With CURAMOIR_JOB stripped (as
    CLAUDE_CODE_SUBPROCESS_ENV_SCRUB would), a destructive command in the
    repository is refused while the run holds its lock, and allowed again
    once the run has released it."""
    import dataclasses

    from curamoir.runner import EXIT_SUCCESS, JOBS, run_job
    from curamoir.scaffold import init_repo

    init_repo(harness.repo)
    during = {}

    def body(_repo, run):
        during["result"], during["decision"] = run_hook("rm -rf build/", cwd=harness.repo)
        run.succeeded("asked the hook")

    monkeypatch.setitem(JOBS, "brief", dataclasses.replace(JOBS["brief"], body=body))

    assert run_job(harness.repo, "brief").exit_code == EXIT_SUCCESS

    assert during["result"].returncode == 0, during["result"].stderr
    assert during["decision"]["permissionDecision"] == "deny"
    _, after = run_hook("rm -rf build/", cwd=harness.repo)
    assert after is None
