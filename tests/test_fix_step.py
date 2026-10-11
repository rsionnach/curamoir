"""E3-R2's prompt half (cm-fkd.3): the limits the fix step is told, and the
headless command that runs it.

Two things are pinned. The prompt and skills/sweep/SKILL.md state the limits
in one text, so they cannot drift apart (the spec says the prompt MUST state
them). And the fix step's command line gives the agent nothing but file
reading and editing — though the point of the last tests here is that it
does not matter what the agent does: a stand-in `claude` that ignores the
prompt and edits out of scope is caught by fix_scope all the same.

No test calls a model. The stand-in is a Python script written per test
that records its argv and working directory and makes the edit the test
gives it.

**Fixture provenance.** As tests/test_fix_scope.py: a git copy of
`fixtures/sample-repo` with tests/test_lint.py's regex rules, and the work
list from the real lint over it.
"""

import json
import os
import re
import subprocess
import sys

import pytest

from curamoir import cli
from curamoir.fix_scope import GIT_INTERNALS_CHANGED, OUTSIDE_VIOLATION, WINDOW, verify_fix
from curamoir.fix_step import (
    FIX_TOOLS,
    SCOPE_LIMITS,
    FixStepRun,
    fix_prompt,
    fix_step_argv,
    repo_deny_rules,
    repo_rules,
    run_fix_step,
)
from curamoir.principles import load_principles, parse_principles
from curamoir.sweep import work_list, work_list_document
from tests.test_fix_scope import BILLING
from tests.test_lint import ROOT

SKILL = ROOT / "skills" / "sweep" / "SKILL.md"

E3_R2 = ("Fixes MUST be scoped: only files with violations; no refactors beyond the "
         "violation; no new dependencies; no signature changes without an explicit "
         "principle permitting it.")


def flat(text):
    return " ".join(text.split())


# --- one text for both prompts ------------------------------------------------------

def test_the_limits_quote_e3_r2_verbatim():
    assert E3_R2 in flat(SCOPE_LIMITS)


def test_the_limits_state_the_window_the_review_step_checks():
    assert f"at most {WINDOW} lines either side" in flat(SCOPE_LIMITS)


def test_the_skill_states_exactly_the_limits_the_fix_step_is_given():
    """Whitespace aside (the skill's Markdown may be rewrapped), the skill
    carries SCOPE_LIMITS whole. Edit fix_step.SCOPE_LIMITS and then the
    skill to match, never one of them alone."""
    assert flat(SCOPE_LIMITS) in flat(SKILL.read_text(encoding="utf-8"))


# --- the prompt ------------------------------------------------------------------------

def test_the_prompt_carries_the_limits_and_the_work_list(swept):
    repo, work = swept

    prompt = fix_prompt(work, load_principles(repo))

    assert SCOPE_LIMITS in prompt
    document = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert document == work_list_document(work)
    assert "Principles that permit signature changes: none." in prompt


def test_the_prompt_names_the_principles_that_permit_signature_changes(swept):
    repo, work = swept
    text = (repo / ".curamoir" / "principles.md").read_text(encoding="utf-8")
    permitted = parse_principles(text.replace(
        "- **promoted_on:** null\n",
        "- **promoted_on:** null\n- **permits:** `signature_change`\n", 1))

    prompt = fix_prompt(work, permitted)

    assert "Principles that permit signature changes: structured-logging." in prompt


# --- the command line -------------------------------------------------------------------

def test_the_fix_step_can_read_and_edit_and_nothing_else(tmp_path):
    argv = fix_step_argv("the prompt", "claude-sonnet-5", tmp_path)

    def value(flag):
        return argv[argv.index(flag) + 1]

    assert argv[:3] == ["claude", "-p", "the prompt"]
    assert value("--model") == "claude-sonnet-5"
    assert value("--tools").split(",") == list(FIX_TOOLS)
    assert value("--allowedTools") == ",".join(repo_rules(tmp_path))
    assert value("--disallowedTools").split(",") == ["mcp__*", *repo_deny_rules(tmp_path)]
    assert value("--permission-mode") == "dontAsk"
    assert value("--output-format") == "json"
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert not {"Bash", "Write", "WebFetch", "WebSearch", "NotebookEdit"} & set(FIX_TOOLS)
    assert "--dangerously-skip-permissions" not in argv
    assert "--bare" not in argv
    assert "--restricted" in argv
    assert "--add-dir" not in argv


def test_the_allow_rules_reach_only_the_repo(tmp_path):
    """S1: an unscoped `Read` or `Edit` in --allowedTools approves that tool on
    every path, so under dontAsk the agent could edit ~/.bashrc or read
    ~/.ssh, and verify_fix (which reads only this repo's diff) would never
    see it. Every allow rule names the repo, absolute (`//`), recursively."""
    repo = tmp_path / "repo"
    repo.mkdir()
    real = os.path.realpath(repo)
    argv = fix_step_argv("the prompt", "m", repo)
    rules = argv[argv.index("--allowedTools") + 1].split(",")

    assert rules == [f"Read(/{real}/**)", f"Edit(/{real}/**)"]
    assert not set(rules) & {"Read", "Edit", "Grep", "Glob", "Write", "Bash"}
    assert all(re.fullmatch(r"(Read|Edit)\(//.+/\*\*\)", rule) for rule in rules)


def test_a_repo_reached_through_a_symlink_is_named_both_ways(tmp_path):
    """Allow rules "apply only when both the requested path and the file it
    resolves to match", and the agent may name either spelling."""
    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo)
    real = os.path.realpath(repo)

    assert repo_rules(link) == [f"Read(/{link}/**)", f"Read(/{real}/**)",
                                f"Edit(/{link}/**)", f"Edit(/{real}/**)"]


def test_the_repos_git_directory_is_denied_by_both_spellings(tmp_path):
    """S3: `Edit(//<repo>/**)` covers `.git/`, where a hook or a config key
    (core.fsmonitor runs on `git diff`) is code the next git command runs,
    and which `git diff` never shows. Deny rules win over allow rules ("deny,
    then ask, then allow") and match when either spelling of a symlinked path
    does."""
    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo)
    real = os.path.realpath(repo)

    rules = repo_deny_rules(link)

    for tool in ("Read", "Edit"):
        for root in (link, real):
            assert f"{tool}(/{root}/.git)" in rules
            assert f"{tool}(/{root}/.git/**)" in rules
    assert len(rules) == 8


def test_the_git_deny_rules_reach_the_command_line_exactly(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    real = os.path.realpath(repo)
    assert real == str(repo)

    argv = fix_step_argv("p", "m", repo)

    assert argv[argv.index("--disallowedTools") + 1] == (
        f"mcp__*,Read(/{real}/.git),Read(/{real}/.git/**),"
        f"Edit(/{real}/.git),Edit(/{real}/.git/**)")


def test_an_agent_that_edits_git_config_is_caught_before_git_runs_again(swept, tmp_path):
    """S3(c): the fix step snapshots the repository's git internals; an edit
    to .git/config fails the verdict, and the planted fsmonitor never runs."""
    repo, work = swept
    marker = tmp_path / "pwned"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    claude, _ = fake_claude(tmp_path, edit=(".git/config", "[core]",
                                            f"[core]\n\tfsmonitor = {hook}"))

    run = run_fix_step(repo, "p", model="m", claude=str(claude))
    result = verify_fix(repo, "HEAD", work, load_principles(repo), git_state=run.git_state)

    assert result.kinds() == (GIT_INTERNALS_CHANGED,)
    assert not marker.exists()


def test_glob_characters_in_the_repo_path_are_escaped_and_spaces_are_not(tmp_path):
    """Rules are gitignore patterns and "Rules you write yourself aren't
    escaped": an unescaped `*` or `[` in the repo's own path would widen the
    rule to sibling directories. Spaces and parentheses need no escape."""
    repo = tmp_path / "my repo (2024) [draft]*?"
    repo.mkdir()
    real = os.path.realpath(repo)
    escaped = real.replace("[", "\\[").replace("]", "\\]").replace("*", "\\*").replace("?", "\\?")

    assert repo_rules(repo)[0] == f"Read(/{escaped}/**)"
    assert repo_rules(repo)[0].endswith("my repo (2024) \\[draft\\]\\*\\?/**)")


@pytest.mark.parametrize("name", ["a,b", "a\nb"])
def test_a_repo_path_the_rule_list_cannot_carry_is_refused(tmp_path, name):
    """--allowedTools takes one comma-separated value; a comma in the path
    would split a rule in two, so the step refuses rather than guess."""
    with pytest.raises(ValueError):
        repo_rules(tmp_path / name)


def fake_claude(tmp_path, edit=None, sleep=0, exit_code=0):
    """A stand-in for `claude`: records argv and cwd to a file beside it,
    optionally replaces text in a file of the repository it is run in, and
    prints a `--output-format json`-shaped result."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "bin" / "calls.json"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        f"time.sleep({sleep})\n"
        f"open({str(log)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}))\n"
        f"edit = {edit!r}\n"
        "if edit:\n"
        "    path, old, new = edit\n"
        "    text = open(path, encoding='utf-8').read()\n"
        "    open(path, 'w', encoding='utf-8').write(text.replace(old, new))\n"
        "print(json.dumps({'type': 'result', 'result': 'done', 'total_cost_usd': 0.01}))\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log


def test_the_fix_step_runs_in_the_repo_on_the_configured_cheap_model(swept, tmp_path):
    repo, _ = swept
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        "version: 1\nmodels:\n  cheap: some-cheap-model\n", encoding="utf-8")
    claude, log = fake_claude(tmp_path)

    run = run_fix_step(repo, "the prompt", claude=str(claude))

    call = json.loads(log.read_text(encoding="utf-8"))
    assert call["argv"][call["argv"].index("--model") + 1] == "some-cheap-model"
    assert call["argv"][:2] == ["-p", "the prompt"]
    assert subprocess.run(["pwd", "-P"], cwd=repo, capture_output=True, text=True
                          ).stdout.strip() == call["cwd"]
    assert run.exit_code == 0 and not run.timed_out
    assert run.payload()["total_cost_usd"] == 0.01


def test_a_scoped_fix_from_the_fix_step_passes_review(swept, tmp_path):
    repo, work = swept
    claude, _ = fake_claude(tmp_path, edit=(BILLING, 'print("calculating line total")',
                                            'logger.info("calculating line total")'))

    run = run_fix_step(repo, fix_prompt(work, load_principles(repo)), model="m",
                       claude=str(claude))

    assert verify_fix(repo, "HEAD", work, load_principles(repo), git_state=run.git_state).ok


def test_an_agent_that_ignores_the_prompt_is_caught_by_review_anyway(swept, tmp_path):
    """The acceptance criterion's "independently of the prompt": the stand-in
    is given the limits and edits line 22 regardless."""
    repo, work = swept
    claude, _ = fake_claude(tmp_path, edit=(BILLING, "return total * (1 - percent)",
                                            "return total * (1 - percent / 100)"))

    run = run_fix_step(repo, fix_prompt(work, load_principles(repo)), model="m",
                       claude=str(claude))

    result = verify_fix(repo, "HEAD", work, load_principles(repo), git_state=run.git_state)
    assert [(b.kind, b.line) for b in result.breaches] == [(OUTSIDE_VIOLATION, 22)]


def test_a_fix_step_that_hangs_is_stopped_at_the_timeout(swept, tmp_path):
    repo, _ = swept
    claude, _ = fake_claude(tmp_path, sleep=30)

    run = run_fix_step(repo, "p", model="m", claude=str(claude), timeout=1)

    assert run.timed_out and run.exit_code is None and run.payload() is None


def test_a_missing_claude_is_reported_not_raised(tmp_path):
    run = run_fix_step(tmp_path, "p", model="m", claude=str(tmp_path / "no-such-claude"))

    assert run.exit_code is None and "could not start" in run.stderr


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]"])
def test_a_payload_that_is_not_a_result_object_is_none(stdout):
    assert FixStepRun((), 1, stdout, "").payload() is None


def test_the_work_list_document_is_what_the_cli_prints(swept, capsys):
    """One shape for the interactive skill and the headless prompt."""
    repo, _ = swept
    assert cli.main(["sweep-work-list", "--repo", str(repo)]) == 0

    assert json.loads(capsys.readouterr().out) == work_list_document(work_list(repo))
