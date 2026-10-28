"""The policy split and stripped raw text before tokenising, so quoting did not count.

One root cause under four separate failures. `SEGMENT.split()` and the heredoc
regex both ran over the command string, where the shell's own rules do not
apply: inside quotes `&` is not a separator and `<<EOF` is not a heredoc. That
cut both ways at once, which is the giveaway that it is one bug and not four —

  - too permissive: a command that merely MENTIONS `<<EOF` had everything after
    it discarded as a heredoc body, so the rest of the command was never judged;
  - too strict: `git commit -m "fix: A & B"` was split mid-string into two
    unbalanced halves and refused as unparseable.

A guard that both misses `rm -rf /` and refuses `git commit` is worse than
either, because the second failure is what gets it switched off.

The fix is to tokenise ONCE, with a lexer that knows about quoting, and judge
the token list: separators, heredocs and redirections are then tokens rather
than substrings. `tests/hooks/test_destructive_forms.py` pins the effects; this
file pins the parsing.
"""

import time

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks.verdict import Category

# Every one of these reaches a destructive effect. The first two were allowed
# because stripping hid them; the next two because a heredoc is "a positional",
# so `bash` looked like it was running a reviewed script.
MUST_DENY = [
    pytest.param('echo "see <<EOF for details"\nrm -rf /', id="mention_hides_tail"),
    pytest.param('grep -n "<<EOF" docs/security.md\ngit push --force origin main',
                 id="mention_hides_force_push"),
    pytest.param("bash <<EOF\nrm -rf /\nEOF", id="bash_heredoc"),
    pytest.param("bash <<< 'rm -rf /'", id="bash_here_string"),
    pytest.param("bash -s", id="bash_stdin_flag"),
    pytest.param("sh /dev/stdin", id="sh_stdin_path"),
    pytest.param("git checkout .", id="checkout_dot"),
    pytest.param("git checkout src/curamoir/cli.py", id="checkout_path"),
    pytest.param("find . -delete", id="find_delete"),
    pytest.param("find . -exec rm -rf {} +", id="find_exec"),
    pytest.param("mv -f .curamoir /tmp/gone", id="move_own_state"),
    pytest.param("echo x | tee .curamoir/state/ledger.jsonl", id="overwrite_own_ledger"),
    pytest.param("cat /dev/null > src/curamoir/cli.py", id="redirect_truncates_source"),
]

# Everything here is an ordinary command a job runs. Each was refused, and a
# guard that refuses `git commit` is a guard the developer turns off.
MUST_ALLOW = [
    pytest.param('git commit -m "fix: A & B"', id="ampersand_in_message"),
    pytest.param('bd create "Found bug & fix" -p 1', id="ampersand_in_bead_title"),
    pytest.param('grep -n "a|b" src', id="pipe_in_pattern"),
    pytest.param('git log --oneline -5 2>&1', id="stderr_redirect"),
    pytest.param("git status >/dev/null 2>&1", id="discard_output"),
    # A pipe into tee beside pytest is refused since round 8 (a non-reader
    # runs alongside the write); the redirection is the same output.
    pytest.param("pytest -q > out.log 2>&1", id="pipe_to_tee"),
    pytest.param("PYTHONPATH=src python3 -m pytest -q", id="the_sweeps_own_test_command"),
    pytest.param('git commit -m "use <<EOF for the fixture"', id="heredoc_mentioned_in_message"),
    pytest.param("bash scripts/curamoir-sweep", id="committed_script"),
]


@pytest.mark.parametrize("command", MUST_DENY)
def test_a_destructive_effect_is_denied_however_it_is_spelled(command):
    decision = judge_command(command, True)

    assert decision.deny, f"{command!r} reached a destructive effect undenied"
    assert decision.reason, "a denial with no reason cannot be acted on"


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_an_ordinary_job_command_is_not_refused(command):
    decision = judge_command(command, True)

    assert not decision.deny, (
        f"{command!r} was refused as {decision.category}: {decision.reason}"
    )


def test_an_unterminated_heredoc_is_refused_rather_than_silently_truncating():
    """With no terminator the old code discarded the rest of the command. If the
    guard cannot see where the data ends it cannot verify what runs."""
    decision = judge_command("cat <<EOF\nrm -rf /", True)

    assert decision.deny
    assert decision.category is Category.UNVERIFIABLE


def test_a_heredoc_body_is_still_treated_as_data_not_as_commands():
    """The permissive direction the stripping existed for, which must survive:
    this project's own docs/security.md contains the string `rm -rf`."""
    decision = judge_command(
        "cat > docs/security.md <<'EOF'\nNever run rm -rf / in a job.\nEOF",
        True,
    )

    assert not decision.deny, decision.reason


def test_a_command_too_long_to_parse_is_denied_rather_than_timed_out():
    """A command hook that times out FAILS OPEN, so a slow parse is a hole and
    not an outage: pad a destructive command past the hook's 5s budget and the
    guard never answers. A refusal costs a retry; a timeout costs the repo."""
    command = "echo " + "a" * 200_000 + " & git reset --hard"

    started = time.monotonic()
    decision = judge_command(command, True)
    elapsed = time.monotonic() - started

    assert decision.deny
    assert decision.category is Category.UNVERIFIABLE
    assert elapsed < 1.0, (
        f"took {elapsed:.1f}s against the 5s PreToolUse budget in hooks.json, "
        "and a timed-out command hook permits the action"
    )


def test_a_command_within_the_bound_is_still_judged_on_its_merits():
    """The cap must not become the whole policy: a long but ordinary command
    still gets a real answer."""
    command = "grep -n " + "x" * 2_000 + " src"

    assert not judge_command(command, True).deny
