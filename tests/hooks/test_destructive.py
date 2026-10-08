"""E0-R5: destructive git and filesystem commands are blocked during a job.

Every refusal names the safe alternative — that is an explicit acceptance
criterion, not a nicety. An agent that is told "no" without being told "instead,
do this" will either retry the same thing or invent something worse.

Scope follows the spec: "during any cúramóir job". The developer's own
destructive commands in their own session are their business. DEC-5 established
how a hook identifies a job: an inherited env var, with the supervisor lock as
fallback because CLAUDE_CODE_SUBPROCESS_ENV_SCRUB can strip env.
"""

from curamoir.hooks.destructive import judge_destructive

DENIED = [
    "git reset --hard",
    "git reset --hard HEAD~3",
    "git checkout -- src/app.py",
    "git push --force",
    "git push --force origin main",
    "git clean -f",
    "git clean -fd",
    "git branch -D feature/x",
    "git stash drop",
    "git stash clear",
    "rm -rf build/",
    "rm -rf /",
]

ALLOWED = [
    "git status",
    "git stash",
    "git stash list",
    "git push",
    "git push --force-with-lease",
    "git branch -d merged-branch",
    "git clean -n",
    "rm build/artifact.txt",
    "pytest -q",
]


def test_every_denylisted_command_is_denied():
    for command in DENIED:
        decision = judge_destructive(command, in_job=True)
        assert decision.deny, command


def test_every_denied_command_names_a_safe_alternative():
    for command in DENIED:
        decision = judge_destructive(command, in_job=True)
        assert decision.alternative, f"{command} was denied with no alternative"
        assert decision.alternative not in command, command


def test_ordinary_commands_are_allowed():
    for command in ALLOWED:
        assert not judge_destructive(command, in_job=True).deny, command


def test_force_with_lease_is_not_caught_by_the_force_push_rule():
    """--force-with-lease is the safe alternative this guard recommends, so
    catching it would make the advice self-contradictory."""
    assert not judge_destructive("git push --force-with-lease origin main", in_job=True).deny


def test_branch_lowercase_d_is_allowed_but_uppercase_D_is_denied():
    """-d refuses to delete unmerged work; -D discards it. One letter apart and
    opposite in consequence, which is exactly why this is denylisted."""
    assert not judge_destructive("git branch -d feature/x", in_job=True).deny
    assert judge_destructive("git branch -D feature/x", in_job=True).deny


def test_nothing_is_denied_outside_a_curamoir_job():
    """The spec scopes this to 'during any cúramóir job'. The developer's own
    destructive commands in their own session are their business."""
    for command in DENIED:
        assert not judge_destructive(command, in_job=False).deny, command


def test_a_denied_command_explains_itself_in_terms_an_agent_can_act_on():
    decision = judge_destructive("git reset --hard", in_job=True)

    assert "stash" in decision.reason.lower()
    assert "reset --hard" in decision.reason


def test_a_command_chained_after_a_separator_is_still_inspected():
    """`true && git reset --hard` must not slip past a naive prefix match."""
    assert judge_destructive("true && git reset --hard", in_job=True).deny
    assert judge_destructive("cd /tmp; rm -rf build/", in_job=True).deny
    assert judge_destructive("make build | tee log && git clean -fd", in_job=True).deny
