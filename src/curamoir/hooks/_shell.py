"""Shell lexing shared by the allowlist and the destructive-rules passes.

Separated because both passes need it: a name imported by a sibling module is
not private, and reaching past another module's underscore to get it was the
smell that prompted this split.

Both passes used to split the raw command string on a separator regex and only
then tokenise each piece. The shell does the opposite, and the difference is not
cosmetic: inside quotes `&` is not a separator and `<<EOF` is not a heredoc. So
`git commit -m "fix: A & B"` was cut into two unbalanced halves and refused,
while `echo "see <<EOF"` followed by `rm -rf /` had its tail discarded as a
heredoc body and was allowed. One mistake, failing in both directions.

So: lex ONCE, with a lexer that knows about quoting, and let the passes judge
tokens. Separators, redirections and heredocs are tokens here, not substrings.
"""

import re
import shlex

#: Characters the lexer emits as their own tokens rather than folding into
#: words. `shlex`'s own default for `punctuation_chars=True` is "();<>|&", with
#: two changes:
#:
#: - the newline is added, because it separates two commands just as `;` does,
#:   and without it `echo hi\nrm -rf /` lexes `rm` as an argument to `echo` —
#:   so the second command would never be judged as a command at all;
#: - the parentheses are removed, because they also appear inside ordinary
#:   arguments: `git for-each-ref --format=%(refname:short)` is one shell word,
#:   and splitting it refused a command the sweep runs. A subshell group stays
#:   covered by `program_name`, which strips the leading paren off `(rm`.
PUNCTUATION = ";<>|&\n"

#: Characters that, alone or in any combination, end one command and begin
#: another. A SET rather than a list of operators, because `punctuation_chars`
#: emits a RUN of adjacent punctuation as one token: a blank line between two
#: commands lexes as `"\n\n"`, and `cmd;\n` as `";\n"`. Neither was in the old
#: list of individual operators, so `segments()` did not split there and the
#: allowlist — the primary defence — judged only the first program of the
#: command. Every redirection operator contains `<` or `>`, so testing against
#: this set leaves redirections alone.
SEPARATOR_CHARACTERS = frozenset(";&|\n")

#: Redirection operators. The shell consumes the following token as a filename,
#: so it is not a program name and must not be judged as one — `pytest 2>&1`
#: was refused with "'1' is not on cúramóir's allowlist".
REDIRECTIONS = frozenset({">", ">>", "<", ">&", "<&", ">|", "&>", "&>>", "<>"})

#: Operators that introduce inline input: a heredoc body or a here-string.
HEREDOCS = frozenset({"<<", "<<-", "<<<"})

#: Programs whose argument is a script or inline code rather than data.
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"})

#: Longest command this guard will parse. Lexing is superlinear in length: a
#: 200 KB command took 20.9s against the 5s PreToolUse budget in hooks.json,
#: and a command hook that times out FAILS OPEN — so an over-long command is a
#: reliable way to switch the guard off for one call, not merely a slow one.
#: Refused above this bound, because a refusal costs a retry and a timeout
#: costs the repository. Comfortably above any command a job legitimately runs.
MAX_COMMAND_CHARS = 16_000


def lex(command):
    """Every token in `command`, or None if it could not be lexed.

    None means unbalanced quoting, and the callers must refuse: a command the
    guard cannot take apart is one whose effect it cannot determine. The old
    code fell back to `str.split()` here, which looks safer but is not — it
    reports tokens that the shell would never produce.
    """
    lexer = shlex.shlex(command or "", posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace_split = True
    # The newline is a separator, not whitespace, so that `punctuation_chars`
    # gets to emit it. Inside quotes it stays part of its token either way.
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    try:
        return list(lexer)
    except ValueError:
        return None


def is_separator(token):
    """Whether `token` is entirely made of command separators."""
    return bool(token) and set(token) <= SEPARATOR_CHARACTERS


def unquoted(command):
    """`command`'s tokens with single-quoted spans blanked.

    NOT positionally aligned with `lex`: this lexes with `posix=False`, which
    splits differently where quotes abut — `echo 'a'"'"'b'` is two tokens to
    `lex` and four here. Only `_unverifiable` consumes this, and it scans for
    patterns without ever indexing across the two lists. An earlier docstring
    claimed a 1:1 correspondence, which is the same class of mistake as the one
    that claimed quoted `$` was literal: a reader would reach for the
    correspondence and it is not there.

    Posix lexing strips the quotes, so by the time a token reaches a pattern
    there is no way to tell `'(WIP|TODO)'` — literal text to the shell — from a
    subshell, or a single-quoted `$HOME` from a variable reference. Both were
    refused, and a guard that refuses `grep -nE '(WIP|TODO)'` and
    `git commit -m 'fix $HOME handling'` is one the developer turns off.
    """
    lexer = shlex.shlex(command or "", posix=False, punctuation_chars=PUNCTUATION)
    lexer.whitespace_split = True
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    try:
        raw = list(lexer)
    except ValueError:
        return None
    return [_blank_single_quoted(token) for token in raw]


def _blank_single_quoted(token):
    """`token` with the contents of single-quoted spans removed.

    A three-state scan, not a regex, because whether an apostrophe opens a
    quote depends on the state to its left — which a regex cannot express. The
    regex this replaces was `'[^']*'`, and inside `"don't … won't"` it matched
    the span `'t … won'` and deleted it, taking any `$(...)` between the two
    apostrophes with it. The shell expands `$(...)` inside double quotes
    whatever apostrophes appear around it, so the command ran while the guard
    saw a token with no substitution left in it.

    The shell's state machine for quotes AND escapes. The escape half is named
    because its absence was invisible without it: the first version tracked only
    the two quote states and claimed in this docstring to be complete, so
    `echo \\'$(terraform apply)\\'` read the escaped apostrophes as quotes and
    blanked everything between them — the same deletion the regex it replaced
    performed, reached by a different spelling. A backslash-escaped quote is a
    literal character, and the shell expands what sits between two of them.

    Double-quoted spans are KEPT, because the shell still expands `$`,
    `` ` `` and `${...}` inside them — refusing there is correct rather than a
    false positive.
    """
    kept = []
    in_single = in_double = escaped = False
    for character in token:
        if escaped:
            # Consumed as data, whatever it is, and crucially without being
            # allowed to change the quote state.
            escaped = False
            if not in_single:
                kept.append(character)
            continue
        # A backslash is literal INSIDE single quotes, where nothing escapes.
        if character == "\\" and not in_single:
            escaped = True
            kept.append(character)
            continue
        if character == "'" and not in_double:
            in_single = not in_single
            continue
        if character == '"' and not in_single:
            in_double = not in_double
        if not in_single:
            kept.append(character)
    return "".join(kept)


def segments(tokens):
    """`tokens` split into one token list per command, separators dropped."""
    found, current = [], []
    for token in tokens:
        if is_separator(token):
            if current:
                found.append(current)
            current = []
        else:
            current.append(token)
    if current:
        found.append(current)
    return found


def strip_redirections(tokens):
    """`tokens` without redirection operators or the filenames they consume.

    The target is dropped from the command's own argument list because it is not
    an argument: it is where output goes. Whether writing THERE is acceptable is
    a separate question, which `destructive` answers from `redirection_targets`.
    """
    kept, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if token in REDIRECTIONS:
            index += 2  # the operator and its filename
            continue
        # `2>&1`-style tokens lex as "2", ">&", "1": the fd that precedes the
        # operator is part of the redirection too, not a program argument.
        if index + 1 < len(tokens) and tokens[index + 1] in REDIRECTIONS and token.isdigit():
            index += 3
            continue
        kept.append(token)
        index += 1
    return kept


def redirection_targets(tokens):
    """The filenames `tokens` would write to, for the destructive pass to judge.

    `cat /dev/null > src/foo.py` truncates a source file while every program in
    it is allowlisted, so the write target has to be judged on its own.
    """
    targets = []
    for index, token in enumerate(tokens):
        if token in {">", ">>", ">|", "&>", "&>>", "<>"} and index + 1 < len(tokens):
            targets.append(tokens[index + 1])
    return targets


def program_name(token):
    """The bare program name a token refers to, ignoring path and decoration.

    The leading parenthesis of a subshell group is stripped, so that `(rm` is
    recognised as `rm`: `(rm -rf /)` runs exactly what `rm -rf /` runs.
    """
    return token.strip("\"'").lstrip("(").rsplit("/", 1)[-1]
