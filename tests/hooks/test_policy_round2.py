"""Round-2 findings: what the rework fixed, and what it only narrowed.

Three themes, each a different way of being nearly right:

- **`segments()` did not recognise separator RUNS.** `punctuation_chars` emits
  adjacent punctuation as ONE token, so `\\n\\n`, `;\\n` and `&&\\n` were not in
  the list of operators and a blank line between two commands merged them. The
  allowlist — the primary defence, by the module's own argument — then judged
  only the first program. This was invisible to a 640-test suite because pass
  2's position scan still caught `rm`/`git`/`find` in the merged segment, so
  only a destructive program OUTSIDE that set exposes it.
- **Two bounds that bounded the wrong quantity.** `MAX_COMMAND_CHARS` bounds
  length while the destructive pass was quadratic in dangerous-token count, and
  `_looks_like_a_path` defaulted an uncertain token to "branch" when its own
  docstring said the safe reading is "path".
- **Two rules scoped to one spelling of their effect.** The inline-input test
  ran only for shells, though `make -f -` runs a makefile from stdin; and
  source truncation was judged only for `>`, though `tee` does it too.
"""

import time

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks._shell import lex, segments
from curamoir.hooks.verdict import Category

SEPARATOR_RUNS = ["a\nb", "a\n\nb", "a;\nb", "a &\nb", "a &&\nb", "a |\nb",
                  "a ||\nb", "a ;; b", "a\n \nb"]


@pytest.mark.parametrize("command", SEPARATOR_RUNS)
def test_a_run_of_separators_still_separates_two_commands(command):
    """Asserted on `segments` directly, not through a verdict: the verdict was
    right for `rm`/`git`/`find` by accident of the second pass, which is how
    this survived a green suite."""
    assert len(segments(lex(command))) == 2, segments(lex(command))


MUST_DENY = [
    # the allowlist, with pass 2 unable to help: none of these is rm/git/find
    pytest.param("git status\n\nterraform apply", id="blank_line_then_unlisted"),
    pytest.param("git status;\nterraform apply", id="semicolon_newline"),
    pytest.param("ruff check . &&\nterraform apply", id="and_newline"),
    pytest.param("git add -A\n\ntruncate -s0 src/curamoir/cli.py", id="truncate_source"),
    pytest.param("git add -A\n\nshred -u src/curamoir/cli.py", id="shred_source"),
    pytest.param("git add -A\n\nchmod -R 000 .", id="chmod_tree"),
    # git checkout over a path, including the extension-less forms
    pytest.param("git checkout src", id="checkout_directory"),
    pytest.param("git checkout Makefile", id="checkout_extensionless_file"),
    pytest.param("git checkout src/curamoir/hooks", id="checkout_nested_directory"),
    pytest.param("git checkout docs", id="checkout_docs"),
    pytest.param("git checkout .", id="checkout_dot"),
    # inline program text through a program that is not a shell
    pytest.param("make -f - <<EOF\nall:\n\trm -rf /\nEOF", id="make_stdin_makefile"),
    pytest.param("docker build -f - . <<EOF\nFROM alpine\nRUN rm -rf /\nEOF",
                 id="docker_stdin_dockerfile"),
    pytest.param("pip install -r /dev/stdin <<EOF\nevil\nEOF", id="pip_stdin_requirements"),
    # a glob that expands to cúramóir's own state
    pytest.param("mv -f .curamoi? /tmp/gone", id="glob_own_state"),
    pytest.param("mv -f .cur* /tmp/gone", id="glob_star_own_state"),
    pytest.param("echo x | tee .curamoi?/ledger.jsonl", id="glob_tee_own_state"),
    # truncating a source file without a redirection
    pytest.param("echo '' | tee src/curamoir/cli.py", id="tee_source"),
    pytest.param("cp -f /dev/null src/curamoir/cli.py", id="cp_over_source"),
    pytest.param("mv -f /dev/null src/curamoir/cli.py", id="mv_over_source"),
]

MUST_ALLOW = [
    # shell syntax INSIDE single quotes is literal text, not syntax
    pytest.param("grep -nE '(WIP|TODO)' src", id="regex_group_in_quotes"),
    pytest.param("rg '(fixme|hack)' src", id="rg_regex_group"),
    pytest.param("git commit -m '(cherry picked from 0189ad5)'", id="cherry_pick_message"),
    pytest.param("git commit -m 'fix $HOME handling'", id="dollar_in_single_quotes"),
    pytest.param("bd create 'document $PATH setup' -p 2", id="dollar_in_bead_title"),
    pytest.param("grep -F '${CLAUDE_PLUGIN_ROOT}' hooks/hooks.json", id="braced_var_quoted"),
    # a heredoc into a file is data, and must stay allowed
    pytest.param("cat > docs/security.md <<'EOF'\nNever run rm -rf /.\nEOF",
                 id="heredoc_to_file"),
    # ordinary multi-command work
    pytest.param("git add -A\n\ngit commit -m 'done'", id="blank_line_ordinary"),
    pytest.param("ruff check . &&\npytest -q", id="and_newline_ordinary"),
    # the branch tool the checkout refusal points at
    pytest.param("git switch curamoir/sweep", id="switch_branch"),
    pytest.param("git switch -c curamoir/sweep", id="switch_new_branch"),
]


@pytest.mark.parametrize("command", MUST_DENY)
def test_a_destructive_effect_is_denied(command):
    decision = judge_command(command, True)

    assert decision.deny, f"{command!r} reached a destructive effect undenied"


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_an_ordinary_job_command_is_not_refused(command):
    decision = judge_command(command, True)

    assert not decision.deny, f"refused as {decision.category}: {decision.reason}"


def test_the_checkout_refusal_names_a_tool_that_is_itself_allowed():
    """A refusal that points at a command the allowlist also refuses is a dead
    end, which E0-R5 exists to prevent."""
    decision = judge_command("git checkout src", True)

    assert "git switch" in decision.alternative
    assert not judge_command("git switch main", True).deny


@pytest.mark.parametrize("command, denied", [
    pytest.param("echo " + "rm " * 5300, True, id="many_dangerous_tokens"),
    pytest.param("echo " + "rm -r " * 2600, True, id="many_with_flags"),
    pytest.param("echo " + "git " * 3900, True, id="many_git_tokens"),
    # Allowed, and that is the point: the bound must not become the policy. A
    # long argument is also the FASTEST case, which is why the previous bound
    # test passed while the token mix was six seconds.
    pytest.param("echo " + "a" * 15_900, False, id="one_long_argument"),
])
def test_the_guard_answers_well_inside_the_hook_budget(command, denied):
    """`MAX_COMMAND_CHARS` bounds the LENGTH; the destructive pass was quadratic
    in the number of dangerous tokens, so 15,924 characters took 6.4s against a
    5s budget — under the declared bound, and a timed-out hook fails open.

    The previous bound test used one long argument, which is the FASTEST case
    (0.018s); the token mix is the slow one.
    """
    assert len(command) <= 16_000, "the point is that these are within the bound"

    started = time.monotonic()
    decision = judge_command(command, True)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"took {elapsed:.2f}s against the 5s PreToolUse budget"
    assert decision.deny is denied


def test_an_absurd_number_of_dangerous_tokens_is_refused_unverified():
    """Refusing beats inspecting 5,300 of them, on the same trade as the length
    bound: a refusal costs a retry, a timeout costs the repository."""
    decision = judge_command("echo " + "rm " * 5300, True)

    assert decision.deny
    assert decision.category is Category.UNVERIFIABLE
    assert "time budget" in decision.reason


def test_changing_branch_by_name_is_refused_toward_git_switch():
    """A deliberate reversal of an earlier decision, recorded here because three
    existing tests asserted the opposite.

    `git checkout main` was allowed on the reasoning that a bare name is a
    branch. It is not decidable: with a file named `main` in the tree, that same
    command checks out the FILE. The review's measured consequence of guessing
    was `git checkout src` silently overwriting a subtree, so the guess is gone
    and the refusal names `git switch`, which does only the branch half and is
    allowlisted.
    """
    decision = judge_command("git checkout main", True)

    assert decision.deny
    assert "git switch" in decision.alternative
    assert not judge_command("git switch main", True).deny


def test_creating_a_branch_is_not_discarding_a_tree():
    """`-b` and friends take a NEW branch name, so there is nothing to lose."""
    for command in ("git checkout -b feature/y", "git checkout -B feature/y",
                    "git checkout --orphan fresh"):
        assert not judge_command(command, True).deny, command
