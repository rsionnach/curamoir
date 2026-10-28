"""Round 10: the guard re-scoped to its job, catching an agent's HONEST
destructive mistakes with a useful alternative, and never failing open (the
hostile agent is the sandbox and clone boundary's job). The final edge-case
review found:

1. a redirect that empties its own input (`sort a > a`);
2. branch force-moves that drop commits (`git checkout -B`, `git branch -f`);
3. deletions through gh and bd, and `git tag -d`;
4. deleting and truncating programs refused with the generic "file a bead",
   where the agent needs to hear what to do instead;
5. wrapped destructive git (`timeout 5 git reset --hard`) refused with the
   wrapper's generic message instead of the rule's alternative;
6. over-refusals: an append, and a new file in a new directory;
7. the hook reading the payload before knowing it is in a job, so a malformed
   payload inside a job exited silently.
"""

import io
import json

import pytest

from curamoir.hooks import judge_command, pretooluse
from curamoir.hooks.verdict import Category


def verdict(command, cwd=None):
    return judge_command(command, in_job=True, cwd=cwd)


# --- 1: a redirect that empties its own input -----------------------------------

@pytest.mark.parametrize("command", [
    "jq . a.json > a.json", "sort a > a", "cat x > x", "cat x >| x", "cat x &> x",
    "sort ./a > a",
])
def test_a_redirect_over_its_own_input_is_refused(command):
    decision = verdict(command)
    assert decision.deny and decision.category == Category.DESTRUCTIVE, decision
    assert "mv it over" in decision.alternative


@pytest.mark.parametrize("command", ["cat x >> x", "sort a > b", "jq . a.json > out.json"])
def test_a_redirect_to_another_file_or_an_append_passes(command):
    assert not verdict(command).deny, verdict(command).reason


# --- 2: branch force-moves -------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git checkout -B main", "git checkout -B main origin/main", "git switch -C x",
    "git switch --force-create x", "git switch --force-c x", "git branch -f x HEAD~3",
    "git branch --force x HEAD~3", "git branch -M x", "git branch -C a b",
    "git branch -fv x HEAD",
])
def test_a_branch_force_move_is_refused(command):
    decision = verdict(command)
    assert decision.deny and decision.category == Category.DESTRUCTIVE, decision
    assert "git switch -c" in decision.alternative


@pytest.mark.parametrize("command", [
    "git checkout -b x", "git switch -c x", "git branch x", "git branch -m new",
    "git branch --list",
])
def test_creating_or_renaming_a_branch_passes(command):
    assert not verdict(command).deny, verdict(command).reason


# --- 3: deletions through gh, bd and git tag -------------------------------------

@pytest.mark.parametrize("command,alternative", [
    ("gh repo delete a/b --yes", "say in the PR"),
    ("gh pr close 1 --delete-branch", "leave the branch"),
    ("gh pr close 1 -d", "leave the branch"),
    ("gh api -X DELETE repos/a/b", "does not change it"),
    ("gh api -XDELETE repos/a/b", "does not change it"),
    ("gh api --method=delete repos/a/b", "does not change it"),
    ("gh api --method DELETE repos/a/b", "does not change it"),
    ("git tag -d v1", "leave it"),
    ("git tag --delete v1", "leave it"),
    ("bd delete x-1", "bd close"),
    ("bd --json delete x-1", "bd close"),
])
def test_a_remote_or_tracker_deletion_is_refused(command, alternative):
    decision = verdict(command)
    assert decision.deny and decision.category == Category.DESTRUCTIVE, decision
    assert alternative in decision.alternative


@pytest.mark.parametrize("command", [
    "gh pr close 1", "gh api repos/a/b", "gh api -X GET repos/a/b", "git tag v1",
    "git tag -l", "bd close x-1",
])
def test_non_deleting_gh_bd_and_tag_pass(command):
    assert not verdict(command).deny, verdict(command).reason


# --- 4: deleting and truncating programs ------------------------------------------

@pytest.mark.parametrize("command", [
    "rm a.py", "rmdir x", "unlink x", "truncate -s0 x", "shred x", "sed -i s/a/b/ x",
    "git rm x", "git rm -r src",
])
def test_deleting_and_truncating_get_a_specific_alternative(command):
    decision = verdict(command)
    assert decision.deny
    assert "git rm --cached" in decision.alternative, decision.alternative
    assert "file a bead" not in decision.alternative


def test_git_rm_cached_passes():
    assert not verdict("git rm --cached x").deny


# --- 5: wrapped destructive git keeps its rule -------------------------------------

@pytest.mark.parametrize("command", [
    "time git reset --hard", "nice git reset --hard", "nice -n 5 git reset --hard",
    "timeout 5 git reset --hard", "command git reset --hard",
    "PYTHONPATH=x git reset --hard",
])
def test_a_wrapped_destructive_git_keeps_its_alternative(command):
    decision = verdict(command)
    assert decision.deny and decision.alternative == "git stash", decision


# --- 6: over-refusals ------------------------------------------------------------

def test_an_append_to_a_source_file_passes(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert not verdict("echo x >> a.py", cwd=str(tmp_path)).deny
    assert not verdict("echo x >> a.py").deny


def test_a_new_source_file_in_a_new_directory_passes(tmp_path):
    assert not verdict("echo x > new_dir/new.py", cwd=str(tmp_path)).deny
    assert not verdict("echo x > new.py", cwd=str(tmp_path)).deny


def test_overwriting_an_existing_source_file_is_still_refused(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert verdict("echo x > a.py", cwd=str(tmp_path)).deny
    assert verdict("echo x > a.py").deny   # cwd unknown: cannot tell, refused


# --- 7: the hook decides "in a job" before reading the rest -------------------------

def _main(payload):
    stdout = io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    assert pretooluse.main(stdin=io.StringIO(raw), stdout=stdout) == 0
    out = stdout.getvalue().strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": 7},
    {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": ["x"]},
    {"tool_name": "Bash", "tool_input": "git status", "cwd": "."},
    [{"tool_name": "Bash"}],
    "not json at all",
])
def test_a_malformed_payload_inside_a_job_is_denied(payload, monkeypatch):
    monkeypatch.setenv("CURAMOIR_JOB", "sweep")
    monkeypatch.setenv("CURAMOIR_USE_DCG", "0")
    decision = _main(payload)
    assert decision is not None and decision["permissionDecision"] == "deny"


def test_a_malformed_payload_outside_a_job_is_silent(monkeypatch, tmp_path):
    monkeypatch.delenv("CURAMOIR_JOB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _main([{"tool_name": "Bash"}]) is None
    assert _main("not json") is None


def test_a_non_bash_tool_inside_a_job_is_ignored(monkeypatch):
    monkeypatch.setenv("CURAMOIR_JOB", "sweep")
    assert _main({"tool_name": "Read", "tool_input": {"file_path": "x"}}) is None
