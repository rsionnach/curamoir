"""Two fixes that were the right shape and the wrong width.

Both were mine, from round 6, and both matched one spelling of the thing they
were meant to catch:

- `_PROGRAM_TEXT_OPTIONS` tested whole tokens, so it saw `-f -` and not
  `--file=-` or `-f-`. The same options reopened the door the moment the value
  was attached. And two allowlisted runners reach inline code by a route that
  is not an option value at all: `uv run -` takes the script as a POSITIONAL,
  and `npm exec -c '<shell>'` takes inline shell as the value of `-c`, which
  means on npm exactly what it means on bash.
- `_SINGLE_QUOTED` was a regex with no notion of quote state, so inside a
  double-quoted string two apostrophes matched as a single-quoted span and were
  deleted — taking any `$(...)` between them with them. Bash expands `$(...)`
  inside double quotes whatever the apostrophes do, so the command ran while
  pass 1 saw a token with no substitution in it.

The lesson in both is the same, and it is this module's own argument: a rule
written as a list of the spellings you thought of is a denylist. Both are now
allowlist-shaped — a heredoc is legitimate only into a data consumer, and
quoting is decided by the state machine the shell itself uses.
"""

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks._shell import unquoted
from curamoir.hooks.verdict import Category

INLINE_CODE_DOORS = [
    # the option-value spellings every CLI accepts
    pytest.param("make --file=- <<EOF\nall:\n\trm -rf /\nEOF", id="make_equals_dash"),
    pytest.param("make -f- <<EOF\nall:\n\trm -rf /\nEOF", id="make_attached_dash"),
    pytest.param("docker build --file=- . <<EOF\nFROM alpine\nEOF", id="docker_equals_dash"),
    pytest.param("pip install -r/dev/stdin <<EOF\nx\nEOF", id="pip_attached_stdin"),
    pytest.param("pip install --requirement=- <<EOF\nx\nEOF", id="pip_equals_dash"),
    # routes that are not an option value at all
    pytest.param("uv run - <<EOF\nimport os\nEOF", id="uv_positional_dash"),
    pytest.param("uv run --script - <<EOF\nimport os\nEOF", id="uv_script_dash"),
    pytest.param("npm exec -c 'rm -rf /'", id="npm_inline_shell"),
    pytest.param("yarn exec -c 'rm -rf /'", id="yarn_inline_shell"),
    pytest.param("pnpm exec -c 'rm -rf /'", id="pnpm_inline_shell"),
    pytest.param("docker run -i alpine sh <<EOF\nrm -rf /\nEOF", id="docker_run_shell_heredoc"),
    # the space-separated forms, which must stay closed
    pytest.param("make -f - <<EOF\nall:\n\trm -rf /\nEOF", id="make_space_dash"),
]

STILL_ALLOWED = [
    # a heredoc into a DATA CONSUMER is the case the stripping exists for
    pytest.param("cat > docs/security.md <<'EOF'\nNever run rm -rf /.\nEOF", id="cat_to_file"),
    pytest.param("tee notes.txt <<'EOF'\nnotes\nEOF", id="tee_to_file"),
    pytest.param("grep -F rm <<'EOF'\nrm -rf /\nEOF", id="grep_heredoc"),
    pytest.param("jq . <<'EOF'\n{}\nEOF", id="jq_heredoc"),
    # ordinary use of the same programs, with a real file
    pytest.param("make test", id="make_target"),
    pytest.param("make -f Makefile.ci test", id="make_named_file"),
    pytest.param("pip install -r requirements.txt", id="pip_named_file"),
    pytest.param("docker build -f Dockerfile .", id="docker_named_file"),
    # `--no`: npm's "never install" (cm-hhr, user decision 2026-10-28).
    pytest.param("npm exec --no tsc", id="npm_exec_program"),
]


@pytest.mark.parametrize("command", INLINE_CODE_DOORS)
def test_inline_program_text_is_refused_however_the_value_is_attached(command):
    decision = judge_command(command, True)

    assert decision.deny, f"{command.splitlines()[0]!r} runs code composed at call time"
    assert decision.category is Category.UNVERIFIABLE, decision.category


@pytest.mark.parametrize("command", STILL_ALLOWED)
def test_a_heredoc_into_a_data_consumer_is_still_allowed(command):
    decision = judge_command(command, True)

    assert not decision.deny, f"refused as {decision.category}: {decision.reason}"


def test_an_equals_sign_is_not_a_value_separator_for_make():
    """`make -f=-` names a FILE called `=-`; GNU make says "No rule to make
    target '=-'". Pinned so that normalising option values does not over-reach
    and start reading it as `-f` with the value `-`."""
    assert not judge_command("make -f=- test", True).deny


# --- the quote state machine ----------------------------------------------

QUOTING_MUST_DENY = [
    pytest.param('echo "don\'t $(terraform apply) won\'t"', id="apostrophes_around_substitution"),
    pytest.param('echo "it\'s $(rm -rf /) o\'clock"', id="apostrophes_around_rm"),
    pytest.param('echo "a\' $HOME \'b"', id="apostrophes_around_variable"),
    pytest.param('echo "plain $(terraform apply)"', id="unquoted_substitution"),
    pytest.param('echo "a `terraform apply` b"', id="backticks_in_double_quotes"),
]

QUOTING_MUST_ALLOW = [
    pytest.param("echo 'literal $(date) in single quotes'", id="substitution_single_quoted"),
    pytest.param("git commit -m 'fix $HOME handling'", id="variable_single_quoted"),
    pytest.param("grep -nE '(WIP|TODO)' src", id="group_single_quoted"),
    pytest.param("git commit -m 'it is $(date) now'", id="substitution_in_message"),
    pytest.param("echo 'don'\\''t'", id="escaped_apostrophe"),
]


@pytest.mark.parametrize("command", QUOTING_MUST_DENY)
def test_a_substitution_the_shell_would_expand_is_refused(command):
    """Inside double quotes the shell expands `$(...)`, `$VAR` and backticks
    whatever apostrophes appear around them."""
    assert judge_command(command, True).deny, f"{command!r} would be expanded by the shell"


@pytest.mark.parametrize("command", QUOTING_MUST_ALLOW)
def test_shell_syntax_inside_single_quotes_is_literal_text(command):
    decision = judge_command(command, True)

    assert not decision.deny, f"refused as {decision.category}: {decision.reason}"


def test_an_apostrophe_inside_double_quotes_does_not_blank_the_token():
    """The mechanism, asserted directly: a verdict-level test cannot show that
    the substitution was deleted rather than judged."""
    tokens = unquoted('echo "don\'t $(terraform apply) won\'t"')

    assert any("$(" in token for token in tokens), tokens


# Attached and `=`-joined values with NO heredoc, so the data-consumer rule
# cannot be what catches them: these reach `_option_pairs` or nothing does.
ATTACHED_WITHOUT_HEREDOC = [
    pytest.param("npm exec -c'rm -rf /'", id="npm_attached_inline_shell"),
    pytest.param("npm exec --call='rm -rf /'", id="npm_equals_inline_shell"),
    pytest.param("make --file=/dev/stdin test", id="make_equals_stdin_path"),
    pytest.param("make -f/dev/stdin test", id="make_attached_stdin_path"),
    pytest.param("pip install -r/dev/stdin", id="pip_attached_stdin_path"),
    pytest.param("pip install --requirement=/dev/stdin", id="pip_equals_stdin_path"),
]


@pytest.mark.parametrize("command", ATTACHED_WITHOUT_HEREDOC)
def test_an_attached_option_value_is_read_as_the_option_it_is(command):
    """`-f V`, `-fV` and `--file=V` are one option with one value, and testing
    whole tokens saw only the first. Without a heredoc in the command the
    data-consumer rule cannot help, so this is `_option_pairs` or nothing."""
    decision = judge_command(command, True)

    assert decision.deny, f"{command!r} hands over program text"
    assert decision.category is Category.UNVERIFIABLE


# The consumer allowlist is only as good as what is on it. These were on it and
# should not have been: a heredoc into `git apply` is a patch composed at call
# time, which rewrites the working tree with no backup — the same family as
# `git checkout <path>`, already refused — and `gh api --input -` posts a
# call-time body to an API with destructive endpoints.
CONSUMERS_THAT_ARE_NOT = [
    pytest.param("git apply <<EOF\npatch\nEOF", id="git_apply_patch_from_stdin"),
    pytest.param("git am <<EOF\npatch\nEOF", id="git_am_patch_from_stdin"),
]

# The other side of the same line, and the reason it is a line rather than a
# list: a consumer may READ its stdin, and `git apply` APPLIES it. A heredoc
# body is how `gh pr create` takes a multi-line body without a temp file, and
# opening PRs is core to a sweep.
CONSUMERS_THAT_ARE = [
    pytest.param("gh pr create --body-file - <<EOF\nbody\nEOF", id="gh_pr_body"),
    pytest.param("gh api --input - <<EOF\n{}\nEOF", id="gh_api_body"),
    pytest.param("bd create 'title' --description=- <<EOF\nd\nEOF", id="bd_description"),
]


@pytest.mark.parametrize("command", CONSUMERS_THAT_ARE_NOT)
def test_a_program_that_acts_on_its_stdin_is_not_a_data_consumer(command):
    decision = judge_command(command, True)

    assert decision.deny, f"{command.splitlines()[0]!r} acts on call-time input"


@pytest.mark.parametrize("command", CONSUMERS_THAT_ARE)
def test_a_program_that_reads_its_stdin_as_data_may_have_a_heredoc(command):
    decision = judge_command(command, True)

    assert not decision.deny, f"refused as {decision.category}: {decision.reason}"


def test_the_ordinary_use_of_those_programs_is_untouched():
    """Removing them from the consumer list must not refuse the commands a job
    actually runs — none of which feeds them a heredoc."""
    for command in ("git commit -m 'done'", "git apply fix.patch",
                    "gh pr create --title t --body b", "bd ready --json"):
        assert not judge_command(command, True).deny, command


def test_the_consumer_list_holds_only_programs_the_allowlist_admits():
    """A program on the consumer list that the allowlist refuses anyway is dead
    weight that READS as permission — `awk` and `sed` were both there, and both
    are refused before the heredoc rule is ever reached."""
    from curamoir.hooks.policy import _HEREDOC_CONSUMERS, _INTERPRETERS, ALLOWED

    for program in _HEREDOC_CONSUMERS:
        assert program in ALLOWED, f"{program} is on the consumer list but not allowlisted"
        assert program not in _INTERPRETERS, f"{program} is both a consumer and an interpreter"


# A backslash-escaped apostrophe is a LITERAL apostrophe to the shell, not a
# quote — so there is no single-quoted span, and everything between two of them
# is expanded. The three-state scan read them as quotes and blanked the middle,
# which is the same deletion the regex it replaced performed, reached by a
# different spelling.
ESCAPES_MUST_DENY = [
    pytest.param("echo \\'$(terraform apply)\\'", id="escaped_quotes_around_substitution"),
    pytest.param("echo \\'$HOME\\'", id="escaped_quotes_around_variable"),
    pytest.param("echo \\'`terraform apply`\\'", id="escaped_quotes_around_backticks"),
    pytest.param('echo "a\\\\" $(terraform apply)', id="escaped_double_quote"),
]


@pytest.mark.parametrize("command", ESCAPES_MUST_DENY)
def test_an_escaped_quote_is_a_literal_character_not_a_quote(command):
    decision = judge_command(command, True)

    assert decision.deny, f"the shell expands {command!r}"


def test_a_backslash_inside_single_quotes_is_itself_literal():
    """Inside single quotes a backslash has no special meaning, so it must not
    start an escape and swallow the closing quote."""
    assert not judge_command("echo 'a\\\\b $(date)'", True).deny
