"""E0-R5: the denylist must decide on what a command DOES, not how it is spelled.

The first implementation matched literal flag spellings and let through
`git push -f`, `rm -r -f`, `rm -Rf`, `rm --recursive --force`,
`git clean -d -f -x`, `git -C path reset --hard`, `git restore` and
`git push origin +main` — while denying `--force-with-lease --force-if-includes`,
the safest force-push git offers.

`-f` is what a model writes most of the time, so that was not an exotic gap: it
was the common case. These tables exist so a future flag spelling cannot quietly
reopen it.
"""

import pytest

from curamoir.hooks.denylist import decide

# Every one of these destroys work with no recovery path. All must be denied.
MUST_DENY = [
    # git reset --hard, in its spellings and behind git global options
    "git reset --hard",
    "git reset --hard HEAD~3",
    "git reset --merge",
    "git -C /repo reset --hard",
    "git -c core.editor=true reset --hard",
    "git --git-dir=/r/.git reset --hard",
    # force push: short, long, clustered, and the +refspec form
    "git push --force",
    "git push -f",
    "git push -f origin main",
    "git push --force origin main",
    "git push origin +main",
    "git push origin +refs/heads/main:refs/heads/main",
    # worktree-destroying checkout/restore
    "git checkout -- src/app.py",
    "git checkout HEAD -- src/app.py",
    "git restore src/app.py",
    "git restore --worktree src/app.py",
    # clean, in every flag arrangement
    "git clean -f",
    "git clean -fd",
    "git clean -xdf",
    "git clean -d -f -x",
    "git clean --force",
    "git clean -d --force",
    # branch deletion that discards unmerged work
    "git branch -D feature/x",
    "git branch -fd feature/x",
    "git branch --delete --force feature/x",
    "git branch --force --delete feature/x",
    # stash destruction
    "git stash drop",
    "git stash clear",
    "git stash drop stash@{2}",
    # recursive force removal, in every arrangement
    "rm -rf build/",
    "rm -fr build/",
    "rm -r -f build/",
    "rm -Rf build/",
    "rm -R -f build/",
    "rm --recursive --force build/",
    "rm -r --force build/",
    "rm --force -r build/",
    "rm -rf /",
    "rm -r --no-preserve-root /",
    # hidden behind shell separators
    "true && git push -f",
    "cd /tmp; rm -Rf build",
    "make build | tee log && git clean -d -f",
]

# None of these destroys unrecoverable work. All must be allowed — a guardrail
# that blocks safe work is one the developer switches off.
MUST_ALLOW = [
    "git status",
    "git stash",
    "git stash list",
    "git stash show",
    "git push",
    "git push origin main",
    "git push --force-with-lease",
    "git push --force-with-lease origin main",
    "git push --force-with-lease --force-if-includes",
    "git push --force-if-includes --force-with-lease origin main",
    "git branch -d merged-branch",
    "git branch --delete merged-branch",
    "git branch -a",
    "git clean -n",
    "git clean --dry-run",
    "git clean -nd",
    "git checkout feature/x",
    "git checkout -b feature/y",
    "git restore --staged src/app.py",
    "git reset",
    "git reset --soft HEAD~1",
    "git reset HEAD~1",
    "rm build/artifact.txt",
    "rm -f build/artifact.txt",
    "rm -i build/artifact.txt",
    "rmdir build",
    "pytest -q",
    "grep -rf patterns.txt src/",
    "echo 'rm -rf is dangerous'",
]


@pytest.mark.parametrize("command", MUST_DENY)
def test_destructive_forms_are_denied(command):
    decision = decide(command, in_job=True)
    assert decision.deny, f"NOT DENIED: {command}"
    assert decision.alternative, f"denied with no alternative: {command}"


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_safe_forms_are_allowed(command):
    assert not decide(command, in_job=True).deny, f"WRONGLY DENIED: {command}"


def test_the_recommended_alternative_is_never_itself_denied():
    """The rule must not block the thing its own message tells you to use.
    `--force-with-lease --force-if-includes` was denied while being recommended."""
    for command in MUST_DENY:
        decision = decide(command, in_job=True)
        suggestion = decision.alternative
        if suggestion.startswith("git ") or suggestion.startswith("rm "):
            assert not decide(suggestion, in_job=True).deny, (
                f"{command!r} suggests {suggestion!r}, which is itself denied"
            )


def test_an_unparseable_command_still_gets_inspected():
    """Unbalanced quotes must not become a bypass: shlex raises on them, and
    falling back to no inspection would make a quote the way round the guard."""
    assert decide('git push -f "unclosed', in_job=True).deny


def test_nothing_is_denied_outside_a_job():
    for command in MUST_DENY[:5]:
        assert not decide(command, in_job=False).deny


# --- prefix words and nested payloads must not be a bypass -----------------
#
# The tokenising rewrite dispatched on tokens[0], so ANY prefix word defeated
# the whole denylist — `sudo rm -rf /` passed a guard that the previous regex
# caught. The regex searched the whole segment; argv[0] dispatch made the prefix
# the bypass. Ten forms regressed at once, so this table is the regression gate.

PREFIXED_MUST_DENY = [
    "sudo rm -rf /",
    "doas rm -rf /tmp/x",
    "time rm -rf /tmp/x",
    "nice -n 5 rm -rf /tmp/x",
    "ionice -c3 rm -rf /tmp/x",
    "timeout 5 git reset --hard",
    "timeout --signal=KILL 5 rm -rf /tmp/x",
    "env FOO=1 git push -f",
    "FOO=1 BAR=2 rm -rf /tmp/x",
    "command rm -rf /tmp/x",
    "exec rm -rf /tmp/x",
    "nohup rm -rf /tmp/x &",
    "xargs rm -rf",
    "stdbuf -o0 rm -rf /tmp/x",
    "sudo -u root git push --force",
    "/usr/bin/sudo rm -rf /",
]

NESTED_MUST_DENY = [
    "echo $(rm -rf /tmp/x)",
    "echo `rm -rf /tmp/x`",
    "VAR=$(git reset --hard) echo done",
    "bash -c 'rm -rf /tmp/x'",
    "sh -c \"git push -f\"",
    "zsh -c 'git clean -fd'",
    "find . -name '*.pyc' -exec rm -rf {} ;",
    "find . -type d -exec rm -rf {} +",
    "echo hi & rm -rf /tmp/x",
    "echo one & echo two & git push -f",
]


@pytest.mark.parametrize("command", PREFIXED_MUST_DENY)
def test_a_prefix_word_is_not_a_bypass(command):
    assert decide(command, in_job=True).deny, f"PREFIX BYPASS: {command}"


@pytest.mark.parametrize("command", NESTED_MUST_DENY)
def test_a_nested_payload_is_not_a_bypass(command):
    assert decide(command, in_job=True).deny, f"NESTED BYPASS: {command}"


# These contain the same words but destroy nothing. A guardrail that blocks them
# is one the developer switches off.
PREFIXED_MUST_ALLOW = [
    "echo 'rm -rf is dangerous'",
    "echo \"never run rm -rf\"",
    "grep -rf patterns.txt src/",
    "grep -r 'rm -rf' docs/",
    "sudo apt-get install git",
    "git log --grep='rm -rf'",
    "printf 'rm -rf %s\\n' /tmp",
    "rm -- -rf",
    "rm -- -r -f",
    "sudo rm file.txt",
    "time pytest -q",
    "echo $(git status)",
    "bash -c 'git status'",
]


@pytest.mark.parametrize("command", PREFIXED_MUST_ALLOW)
def test_mentions_and_safe_prefixed_commands_are_allowed(command):
    assert not decide(command, in_job=True).deny, f"WRONGLY DENIED: {command}"


def test_recursion_into_payloads_terminates():
    """A deeply nested payload must not hang or blow the stack."""
    command = "bash -c " + " ".join(["'bash -c"] * 20) + " 'git status'" + "'" * 20

    decide(command, in_job=True)  # must simply return


# --- round 3: rule-table gaps (each one line, independent of the parser) ---

RULE_GAPS_MUST_DENY = [
    # Discards the working tree exactly as `git reset --hard` does, and that is
    # the rule two lines above it in the same table.
    "git checkout -f",
    "git checkout --force .",
    "git switch --discard-changes main",
    # Deletes a remote branch outright: strictly worse than the force-push the
    # push rule exists to stop.
    "git push --delete origin main",
    "git push origin :main",
    # The commands that make `git reset --hard` actually unrecoverable. The
    # module's own refusal text calls that command irrecoverable, which is only
    # true once the reflog is gone — and this is the documented recipe for it.
    "git reflog expire --expire=now --all",
    "git gc --prune=now",
    "git update-ref -d refs/heads/main",
    "git worktree remove --force wt",
    "git submodule deinit -f .",
    "git filter-branch --force --all",
    # restore targeting the worktree, however it is spelled
    "git restore --staged --worktree .",
    "git restore -SW .",
]

RULE_GAPS_MUST_ALLOW = [
    # -S is git's documented short form of --staged; staged-only restore touches
    # no working-tree content, so the two spellings must agree.
    "git restore -S src/app.py",
    "git restore --staged src/app.py",
    "git checkout main",
    "git switch main",
    "git push origin main",
    "git gc",
    "git gc --auto",
    "git worktree list",
    "git submodule update --init",
    "git reflog",
]


@pytest.mark.parametrize("command", RULE_GAPS_MUST_DENY)
def test_equivalent_destruction_is_also_denied(command):
    assert decide(command, in_job=True).deny, f"NOT DENIED: {command}"


@pytest.mark.parametrize("command", RULE_GAPS_MUST_ALLOW)
def test_the_safe_halves_of_those_rules_are_allowed(command):
    assert not decide(command, in_job=True).deny, f"WRONGLY DENIED: {command}"
