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

#: Tokens that end one command and begin another.
SEPARATORS = frozenset({";", "&", "&&", "|", "||", "\n", "|&"})

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


def segments(tokens):
    """`tokens` split into one token list per command, separators dropped."""
    found, current = [], []
    for token in tokens:
        if token in SEPARATORS:
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
