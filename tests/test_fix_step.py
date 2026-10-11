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
import time
from types import SimpleNamespace

import pytest

from curamoir import cli
from curamoir.fix_scope import (
    FILE_ADDED,
    FILE_NOT_IN_WORK_LIST,
    GIT_INTERNALS_CHANGED,
    OUTSIDE_VIOLATION,
    SIGNATURE_CHANGE,
    WINDOW,
    base_principles,
    verify_fix,
)
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
from curamoir.git_guard import GitGuardError, prepare_sandbox
from curamoir.principles import load_principles, parse_principles
from curamoir.sweep import work_list, work_list_document
from tests.conftest import committed_sample
from tests.test_fix_scope import BILLING, PRINCIPLES, V, git_repo
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


def test_the_limits_allow_the_one_deletion_the_review_step_allows():
    """fix_scope accepts deleting a file only for a file-level violation (the
    empty region at 1:1 a `target: path` rule reports); the prompt says the
    same, so a compliant agent is never refused for following it."""
    text = flat(SCOPE_LIMITS)
    assert "Do not create or rename a file" in text
    assert "Delete a file only when one of its violations is the file itself" in text
    assert "line 1, column 1" in text


def test_the_limits_tie_a_signature_permission_to_a_violation_in_that_definition():
    assert ("unless the principle of a violation in that function or class says "
            "`permits: signature_change`") in flat(SCOPE_LIMITS)


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


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]"])
def test_a_payload_that_is_not_a_result_object_is_none(stdout):
    assert FixStepRun((), 1, stdout, "").payload() is None


def test_the_work_list_document_is_what_the_cli_prints(swept, capsys):
    """One shape for the interactive skill and the headless prompt."""
    repo, _ = swept
    assert cli.main(["sweep-work-list", "--repo", str(repo)]) == 0

    assert json.loads(capsys.readouterr().out) == work_list_document(work_list(repo))


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


def fake_claude(tmp_path, edits=(), writes=None, sleep=0, exit_code=0, background=None):
    """A stand-in for `claude`: records argv, cwd and the GIT_* variables it
    was given to a file beside it, replaces text in files of the directory
    it is run in (`edits`: (path, old, new) triples), writes new files there
    (`writes`: path -> text), optionally starts `background` (a shell
    command) as a grandchild, and prints a `--output-format json`-shaped
    result."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "bin" / "calls.json"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        f"background = {background!r}\n"
        "if background:\n"
        "    subprocess.Popen(['/bin/sh', '-c', background])\n"
        f"time.sleep({sleep})\n"
        f"open({str(log)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "    'git_env': sorted(k for k in os.environ if k.startswith('GIT_'))}))\n"
        f"for path, old, new in {list(edits)!r}:\n"
        "    text = open(path, encoding='utf-8').read()\n"
        "    open(path, 'w', encoding='utf-8').write(text.replace(old, new))\n"
        f"for path, text in {dict(writes or {})!r}.items():\n"
        "    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)\n"
        "    open(path, 'w', encoding='utf-8').write(text)\n"
        "print(json.dumps({'type': 'result', 'result': 'done', 'total_cost_usd': 0.01}))\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log


def prompt_for(sandbox):
    return fix_prompt(sandbox.work_list, base_principles(sandbox))


# --- the fix step, in its sandbox ------------------------------------------------------

def test_the_fix_step_runs_in_the_sandbox_on_the_bases_cheap_model(tmp_path):
    """cwd and both rule lists name the sandbox, never the user's checkout;
    the model is the one committed in the base's config."""
    repo = committed_sample(tmp_path)
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        "version: 1\nmodels:\n  cheap: some-cheap-model\n", encoding="utf-8")
    subprocess.run(["git", "add", ".curamoir/curamoir.yaml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "cheap model"], cwd=repo, check=True)
    claude, log = fake_claude(tmp_path)

    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        run = run_fix_step(sandbox, "the prompt", claude=str(claude))
        real = os.path.realpath(sandbox.path)

    call = json.loads(log.read_text(encoding="utf-8"))
    assert call["argv"][call["argv"].index("--model") + 1] == "some-cheap-model"
    assert call["argv"][:2] == ["-p", "the prompt"]
    assert os.path.realpath(call["cwd"]) == real != os.path.realpath(repo)
    allowed = call["argv"][call["argv"].index("--allowedTools") + 1]
    assert f"Edit(/{real}/**)" in allowed.split(",") and str(repo) not in allowed
    assert run.exit_code == 0 and not run.timed_out
    assert run.payload()["total_cost_usd"] == 0.01


def test_the_fix_step_runs_only_in_a_sandbox(swept, tmp_path):
    repo, _ = swept
    claude, log = fake_claude(tmp_path)

    with pytest.raises(GitGuardError, match="sandbox"):
        run_fix_step(repo, "p", model="m", claude=str(claude))
    assert not log.exists()


def test_a_scoped_fix_from_the_fix_step_passes_review(sandboxed, tmp_path):
    claude, _ = fake_claude(tmp_path, edits=[(BILLING, 'print("calculating line total")',
                                              'logger.info("calculating line total")')])

    run = run_fix_step(sandboxed, prompt_for(sandboxed), model="m", claude=str(claude))
    result = verify_fix(sandboxed, git_state=run.git_state)

    assert result.ok and result.paths == (BILLING,), result.render()
    assert b"logger.info" in sandboxed.patch(result.paths)


def test_an_agent_that_ignores_the_prompt_is_caught_by_review_anyway(sandboxed, tmp_path):
    """The acceptance criterion's "independently of the prompt": the stand-in
    is given the limits and edits line 22 regardless."""
    claude, _ = fake_claude(tmp_path, edits=[(BILLING, "return total * (1 - percent)",
                                              "return total * (1 - percent / 100)")])

    run = run_fix_step(sandboxed, prompt_for(sandboxed), model="m", claude=str(claude))

    result = verify_fix(sandboxed, git_state=run.git_state)
    assert [(b.kind, b.line) for b in result.breaches] == [(OUTSIDE_VIOLATION, 22)]


def test_an_agent_that_edits_the_sandboxs_git_config_is_caught_before_git_runs(
        sandboxed, tmp_path):
    marker = tmp_path / "pwned"
    claude, _ = fake_claude(tmp_path, edits=[(".git/config", "[core]",
                                              f"[core]\n\tfsmonitor = touch {marker}")])

    run = run_fix_step(sandboxed, "p", model="m", claude=str(claude))
    result = verify_fix(sandboxed, git_state=run.git_state)

    assert result.kinds() == (GIT_INTERNALS_CHANGED,)
    assert not marker.exists()


def test_the_fix_step_is_given_no_git_environment(sandboxed, tmp_path, monkeypatch):
    """A GIT_DIR inherited by the runner would point anything git-aware in the
    agent's process tree at another repository."""
    monkeypatch.setenv("GIT_DIR", str(sandboxed.source))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor'='true'")
    claude, log = fake_claude(tmp_path)

    run_fix_step(sandboxed, "p", model="m", claude=str(claude))

    assert json.loads(log.read_text(encoding="utf-8"))["git_env"] == []


def test_a_fix_step_that_hangs_is_stopped_at_the_timeout(sandboxed, tmp_path):
    claude, _ = fake_claude(tmp_path, sleep=30)

    run = run_fix_step(sandboxed, "p", model="m", claude=str(claude), timeout=1)

    assert run.timed_out and run.exit_code is None and run.payload() is None


def test_a_timed_out_fix_step_takes_its_whole_process_group_with_it(sandboxed, tmp_path):
    """A helper the agent started must not go on editing the sandbox after
    the step was stopped and the review began: the stand-in starts a
    grandchild that writes two seconds later, and the step is stopped at one."""
    marker = tmp_path / "late"
    claude, _ = fake_claude(tmp_path, sleep=30, background=f"sleep 2; touch {marker}")

    run = run_fix_step(sandboxed, "p", model="m", claude=str(claude), timeout=1)
    time.sleep(3)

    assert run.timed_out
    assert not marker.exists(), "the grandchild outlived the fix step"


def test_a_missing_claude_is_reported_not_raised(sandboxed, tmp_path):
    run = run_fix_step(sandboxed, "p", model="m", claude=str(tmp_path / "no-such-claude"))

    assert run.exit_code is None and "could not start" in run.stderr
    assert run.git_state is not None


# --- the escapes in-checkout review missed, run against the sandbox --------------------

def test_an_owner_include_path_cannot_carry_a_filter_into_the_review(tmp_path):
    """The Critical repro. The owner's `.git/config` includes a work-tree file
    (`include.path = ../local.gitconfig`) and `*.py filter=custom` is
    committed. In the checkout, an agent that writes a clean command into
    local.gitconfig had it run by the review's own `git diff`. In the
    sandbox, the clone's config includes nothing: the edit is an
    out-of-scope file, and the command never runs."""
    marker = tmp_path / "filtered"
    repo = git_repo(tmp_path, {"m.py": "x = 1\n", ".gitattributes": "*.py filter=custom\n",
                               "local.gitconfig": "# the owner's local settings\n"})
    subprocess.run(["git", "config", "include.path", "../local.gitconfig"], cwd=repo,
                   check=True)
    planted = f"[filter \"custom\"]\n\tclean = touch {marker}; cat\n"
    claude, _ = fake_claude(tmp_path, edits=[("m.py", "x = 1", "x = 2")],
                            writes={"local.gitconfig": planted})

    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        sandbox.work_list = SimpleNamespace(items=[V("m.py", 1)])
        run = run_fix_step(sandbox, "p", model="m", claude=str(claude))
        result = verify_fix(sandbox, git_state=run.git_state)
        sandbox.patch(("m.py",))

        assert (sandbox.path / "local.gitconfig").read_text() == planted
        assert result.kinds() == (FILE_NOT_IN_WORK_LIST,)
        assert result.breaches[0].path == "local.gitconfig"
    assert not marker.exists()

    # The control: the same edit in the user's checkout, and a plain `git
    # diff` there runs the planted command. This is what the sandbox prevents.
    (repo / "local.gitconfig").write_text(planted, encoding="utf-8")
    (repo / "m.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "diff"], cwd=repo, check=True, capture_output=True)
    assert marker.exists()


def test_a_file_written_under_an_ignored_directory_is_refused(tmp_path):
    """The ignored-file repro: `.venv/` is ignored at the base, the agent
    plants `.venv/lib/requests.py`, and the in-checkout review (untracked
    files with --exclude-standard) said ok."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n", ".gitignore": ".venv/\n"})
    claude, _ = fake_claude(tmp_path, edits=[("m.py", "x = 1", "x = 2")],
                            writes={".venv/lib/requests.py": "def get(*a):\n    return 1\n",
                                    "sub/.gitignore": "*\n", "sub/boot.py": "import os\n"})

    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        sandbox.work_list = SimpleNamespace(items=[V("m.py", 1)])
        run = run_fix_step(sandbox, "p", model="m", claude=str(claude))
        result = verify_fix(sandbox, git_state=run.git_state)

    assert {(b.kind, b.path) for b in result.breaches} == {
        (FILE_ADDED, ".venv/lib/requests.py"), (FILE_ADDED, "sub/.gitignore"),
        (FILE_ADDED, "sub/boot.py")}


def test_a_permission_the_agent_writes_into_principles_grants_nothing(sandboxed, tmp_path):
    """The principles repro: the agent adds `permits: signature_change` to
    the principle whose violation sits beside `def line_total`, then changes
    that signature. The review reads principles from the base commit."""
    claude, _ = fake_claude(tmp_path, edits=[
        (PRINCIPLES, "- **promoted_on:** null\n",
         "- **promoted_on:** null\n- **permits:** `signature_change`\n"),
        (BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")])

    run = run_fix_step(sandboxed, prompt_for(sandboxed), model="m", claude=str(claude))
    result = verify_fix(sandboxed, git_state=run.git_state)

    assert "permits" in (sandboxed.path / PRINCIPLES).read_text(encoding="utf-8")
    assert {(b.kind, b.path) for b in result.breaches} == {
        (SIGNATURE_CHANGE, BILLING), (FILE_NOT_IN_WORK_LIST, PRINCIPLES)}
