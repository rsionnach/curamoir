"""DEC-9: the command policy for a cúramóir job — allowlist first, denylist second.

A denylist must understand every way a command can be written; an allowlist only
has to recognise the few things cúramóir's own jobs legitimately do.

The asymmetry is the whole argument:

- a denylist that is behind lets a destructive command run, and the work is gone;
- an allowlist that is behind refuses a legitimate command, costing a retry and a
  bead to widen the list.

So: **unrecognised is refused, and unverifiable is refused without being
parsed.** Command substitution, process substitution, `eval` and a shell's `-c`
are refused *because the guard cannot see what they run* — which is what closes
the holes three parsers could not. The refusal says so, rather than claiming the
command was destructive.

The denylist is kept as a second pass, because an allowlisted program can still
be used destructively: `git push` is legitimate, `git push --force` is not.

Scope is E0-R5's: this applies during a cúramóir job. The developer's own
commands in their own session are their business.

Stdlib only, and no config read: this runs on every Bash call during a job.
"""

import re

from curamoir.hooks._shell import (
    HEREDOCS,
    MAX_COMMAND_CHARS,
    SHELLS,
    lex,
    segments,
    strip_redirections,
    unquoted,
)
from curamoir.hooks.destructive import judge_destructive
from curamoir.hooks.verdict import Category, Decision

# Construction whose effect this guard cannot determine by reading it. Refused
# rather than parsed — the distinction matters, and the message says which.
_UNVERIFIABLE = (
    (re.compile(r"\$\("), "command substitution `$(...)`"),
    (re.compile(r"`"), "command substitution with backticks"),
    # A token that OPENS a parenthesis group: a process substitution lexes as
    # `<` then `(rm`, and a subshell group as `(rm`. Anchored at the start of
    # the token so that `--format=%(refname:short)`, one shell word the sweep
    # runs, is not caught by it.
    (re.compile(r"^\("), "a subshell or process substitution `(...)`"),
    (re.compile(r"\$\{?[A-Za-z_]"), "a shell variable, whose value is not knowable here"),
)

# `NAME=value` before the program is a shell ASSIGNMENT PREFIX, not a command:
# the sweep's own test command is `PYTHONPATH=src python3 -m pytest -q` (E3-R4).
# A variable REFERENCE (`$R`) is still unverifiable; an assignment is not.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Programs that can run anything, so allowlisting them would allowlist everything.
_INTERPRETERS = {
    "eval", "exec", "source", ".", "env", "python", "python3", "python2",
    "perl", "ruby", "node", "deno", "bun", "php", "awk", "xargs", "parallel",
    "sudo", "doas", "su", "ssh", "nohup", "setsid", "script",
}

# (program -> allowed subcommands, or None for "any arguments").
# Deliberately small. Being behind here costs a retry, which is the trade DEC-9
# was taken to make.
ALLOWED = {
    "git": {
        # reads
        "status", "diff", "log", "show", "rev-parse", "symbolic-ref", "describe",
        "for-each-ref", "ls-files", "cat-file", "blame", "shortlog", "reflog",
        "config", "remote", "branch", "tag", "worktree", "submodule", "gc",
        "rev-list", "merge-base", "ls-remote", "check-ignore", "var", "version",
        # the writes a sweep needs
        "add", "commit", "checkout", "switch", "push", "fetch", "pull", "stash",
        "restore", "init", "merge", "rebase", "cherry-pick", "revert", "apply", "am",
        # Present so the DENYLIST can refine them and name the safe alternative.
        # `reset` and `clean` have legitimate non-destructive forms a job needs
        # (`reset --soft`, `reset <path>` to unstage, `clean -n` to preview), and
        # omitting them entirely meant `git reset --hard` was refused by the
        # allowlist with a generic message instead of by the denylist with
        # "use git stash" — which E0-R5 requires.
        "reset", "clean",
    },
    "gh": {"pr", "issue", "label", "repo", "api", "auth", "run", "workflow"},
    "bd": None,
    "pytest": None,
    "tox": None,
    "nox": None,
    "ruff": None,
    "black": None,
    "mypy": None,
    "flake8": None,
    "pyflakes": None,
    "eslint": None,
    "prettier": None,
    "tsc": None,
    "ast-grep": None,
    "sg": None,
    "uv": None,
    "pip": None,
    "make": None,
    "npm": None,
    "pnpm": None,
    "yarn": None,
    "cargo": None,
    "go": None,
    # read-only file and text tools
    "cat": None, "head": None, "tail": None, "ls": None, "wc": None,
    "grep": None, "rg": None, "egrep": None, "fgrep": None,
    "sort": None, "uniq": None, "cut": None, "tr": None, "diff": None,
    "find": None, "which": None, "basename": None, "dirname": None,
    "echo": None, "printf": None, "true": None, "false": None,
    "jq": None, "yq": None, "date": None, "pwd": None, "test": None,
    "mkdir": None, "touch": None, "cp": None, "mv": None, "tee": None,
    "docker": None, "podman": None,
    # curamoir's own helpers
    "curamoir": None, "curamoir-lint": None, "curamoir-grade": None,
}

# `python3 -m <module>` is how the test runner and our own scripts are invoked,
# so it is allowed even though a bare interpreter is not: `-m` names a module
# rather than inline code.
_PYTHON = {"python", "python3", "python2"}
_ALLOWED_PYTHON_MODULES = {"pytest", "pyflakes", "ruff", "mypy", "black", "curamoir.cli"}


def _strip_heredoc_bodies(tokens):
    """(tokens with heredoc bodies removed, whether one was left unterminated).

    A heredoc body is data, not commands. Without removing it, writing a file
    that merely MENTIONS `rm -rf` — security documentation, a rule fixture,
    this module's own tests — is refused, and a guard that blocks writing its
    own documentation is one the developer switches off.

    The operator and its terminator word stay, because they are the command's
    own tokens and the shell branch needs to see them: `bash <<EOF` is inline
    code however the body reads.

    An unterminated heredoc returns True, and the caller must refuse. Dropping
    the remainder instead — which is what the previous regex did, over the raw
    string, to the end of the command — is how `echo "see <<EOF"` followed by
    `rm -rf /` came to be allowed.
    """
    kept, index, unterminated = [], 0, False
    while index < len(tokens):
        token = tokens[index]
        kept.append(token)
        index += 1
        if token not in HEREDOCS or token == "<<<":
            continue
        if index >= len(tokens):
            unterminated = True
            break
        terminator = tokens[index]
        kept.append(terminator)
        index += 1
        closed = False
        while index < len(tokens):
            if tokens[index] == terminator:
                index += 1
                closed = True
                break
            index += 1
        if not closed:
            unterminated = True
    return kept, unterminated


def _unverifiable(tokens):
    """The construction in `tokens` whose effect cannot be read, if any.

    `tokens` must come from `unquoted`, not from `lex`: posix lexing strips the
    quotes, so a single-quoted `$HOME` or `'(WIP|TODO)'` — literal text to the
    shell — is indistinguishable from the real thing and was refused. The
    docstring here used to claim that per-token checking solved that; it did
    not, because the quoting was already gone by then.
    """
    for token in tokens:
        for pattern, description in _UNVERIFIABLE:
            if pattern.search(token):
                return description
    return None


#: Arguments that hand a program its own text to run, rather than naming a file
#: that someone reviewed.
_INLINE_INPUT = frozenset({"<<", "<<-", "<<<", "-s", "/dev/stdin", "-"})

#: Options whose VALUE is program text rather than data. A shell is not the only
#: program that will run what it is handed on stdin: `make -f -` reads a
#: makefile whose recipes are shell, `docker build -f -` a Dockerfile whose RUN
#: lines are shell, `pip install -r /dev/stdin` a requirements file.
#:
#: This is NOT the "`make` runs repo-controlled code, which is an accepted
#: trade" case: that trade is about a committed Makefile. Text composed at call
#: time is the line this module draws.
_PROGRAM_TEXT_OPTIONS = frozenset({
    "-f", "--file", "--makefile", "-r", "--requirement", "-c", "--config",
    "--script",
})

#: Options that mean "run this string", whatever the string is. `-c` on
#: npm/yarn/pnpm means what `-c` means on bash: `npm exec -c 'rm -rf /'` runs
#: inline shell. Separate from the table above, which is about a value that
#: happens to be a stdin marker.
_INLINE_CODE_OPTIONS = {
    "npm": frozenset({"-c", "--call"}),
    "yarn": frozenset({"-c", "--call"}),
    "pnpm": frozenset({"-c", "--call"}),
}

#: Programs a heredoc may legitimately feed, because they consume it as DATA.
#:
#: An allowlist, deliberately, and the second time this module has had to make
#: that move. The previous rule listed the programs whose stdin is program text
#: — and `uv run -` takes its script as a POSITIONAL, so it was not on the list
#: and ran anyway. Enumerating the dangerous side of this is the denylist
#: problem all over again: every runner that grows a `-` spelling is a new hole
#: nobody notices. Naming the harmless side instead means an unrecognised
#: program with a heredoc is refused, which costs a retry and a line here.
_HEREDOC_CONSUMERS = frozenset({
    "cat", "tee", "grep", "egrep", "fgrep", "rg", "sort", "uniq", "wc", "head",
    "tail", "cut", "tr", "diff", "jq", "yq",
    # `gh` and `bd` READ their stdin as a request body or a record. That is the
    # distinction that matters, and `git` is on the other side of it: a
    # consumer may read its stdin, and `git apply` APPLIES it. A heredoc body
    # is the normal way to give `gh pr create` a multi-line body without a temp
    # file, and opening PRs is core to a sweep, so removing these two with
    # `git` was collateral rather than a decision.
    "gh", "bd",
})

# Deliberately NOT on that list, each for a stated reason:
#
# - `git`. `git apply <<EOF` and `git am <<EOF` apply a patch composed at call
#   time, which rewrites the working tree with no backup — the same family as
#   `git checkout <path>`, which this module already refuses by name. The
#   asymmetry was stated by the guard itself: `cat > app.py <<EOF` was denied
#   while `git apply <<EOF` doing the same thing to the same file was allowed.
# - `awk` and `sed`. They take a PROGRAM, not just data, and both are already
#   refused by the allowlist (`awk` as an interpreter, `sed` by absence) — so
#   listing them here would be dead weight that reads as permission.
# - `column` and `fmt`, for that last reason alone: harmless, but not on the
#   allowlist, so naming them here would describe a permission that does not
#   exist. A test asserts this list is a subset of the allowlist, so the two
#   cannot drift into disagreeing about what a job may run.


def _option_pairs(arguments):
    """`(name, value)` for each option in `arguments`, however it is spelled.

    `-f V`, `-fV`, `--file=V` and `--file V` are the same option with the same
    value, and testing whole tokens saw only the first: `make --file=-` and
    `make -f-` both reopened a door that `make -f -` had closed.

    `-f=V` is deliberately NOT read as `-f` with value `V`. GNU make treats
    `-f=-` as a file literally named `=-` ("No rule to make target '=-'"), so
    normalising it would refuse a command that does nothing of the kind.
    """
    pairs = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if not argument.startswith("-") or argument in ("-", "--"):
            continue
        if argument.startswith("--"):
            if "=" in argument:
                name, _, value = argument.partition("=")
                pairs.append((name, value))
                continue
            name = argument
        else:
            name, attached = argument[:2], argument[2:]
            if attached and not attached.startswith("="):
                pairs.append((name, attached))
                continue
            if attached:
                continue   # `-f=V`: the value really is "=V"
        following = arguments[index] if index < len(arguments) else None
        pairs.append((name, following))
    return pairs


def _inline_program_text(program, arguments):
    """A refusal if `arguments` hand `program` text to run, else None."""
    for name, value in _option_pairs(arguments):
        if name in _INLINE_CODE_OPTIONS.get(program, ()):
            return (
                Category.UNVERIFIABLE,
                f"{program} {name} runs the string that follows as a command, "
                "composed at call time and verifiable by nothing. Put it in a "
                "committed script and run that instead",
            )
        if name in _PROGRAM_TEXT_OPTIONS and value in _INLINE_INPUT:
            return (
                Category.UNVERIFIABLE,
                f"{program!r} is being handed its {name} from stdin, so the "
                "guard cannot read what it would run. Put it in a committed "
                "file and name that file instead",
            )

    # A surviving heredoc or here-string operator is legitimate only into a
    # program that consumes it as DATA. Inverted from a list of the programs
    # that would RUN it, which is the denylist shape: `uv run -` takes its
    # script as a positional and so was not on any option list.
    if HEREDOCS.intersection(arguments) and program not in _HEREDOC_CONSUMERS:
        return (
            Category.UNVERIFIABLE,
            f"{program!r} is being fed a heredoc, and it is not one of the "
            "programs cúramóir knows to consume one as data — so the guard "
            "cannot tell whether the body is data or a program. Write the text "
            "to a file and name that file instead",
        )
    return None


def _inline_code(program):
    return (
        Category.UNVERIFIABLE,
        f"{program!r} with -c runs code composed at call time, which the "
        "guard cannot verify. Put the script in a file and run that instead",
    )


def _refusal_for(tokens):
    """(Category, reason) if this command must be refused, else None.

    The category is returned, never inferred from the message: prose is not a
    safe way to tell outcomes apart.

    UNVERIFIABLE is reserved for cases where the guard genuinely cannot determine
    the effect — inline code, a bare interpreter. A program we recognise and
    decline is NOT_ALLOWLISTED, because we read it perfectly well and said no.
    """
    # Drop leading assignment prefixes so the real program is judged.
    while tokens and _ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None

    program = tokens[0].strip("\"'").rsplit("/", 1)[-1]
    arguments = tokens[1:]

    fed_inline = _inline_program_text(program, arguments)
    if fed_inline is not None:
        return fed_inline

    if program in SHELLS:
        # A committed SCRIPT may run; inline code may not.
        #
        # The line is reviewed-artefact versus generated-at-call-time. cúramóir
        # already allows `pytest`, `make` and `npm`, each of which executes
        # arbitrary repo-controlled code, so a committed script is no worse.
        # Inline code is different: it is composed at call time and is not an
        # artefact anyone reviewed.
        #
        # `-c` is only the best-known route to it. A heredoc, a here-string,
        # `-s` and `/dev/stdin` all feed a shell code that no one reviewed, and
        # each of those reads as "a positional", so testing for a positional
        # read `bash <<EOF` as a committed script.
        if _INLINE_INPUT.intersection(arguments):
            return (
                Category.UNVERIFIABLE,
                f"{program!r} is being fed code to run inline rather than a "
                "committed script, which the guard cannot verify. Put the "
                "script in a file and run that instead",
            )
        # Match `-c` exactly for a long flag, and character by character for a
        # clustered short one. `"c" in flag` matched any flag whose NAME
        # contains a c, so `bash --rcfile /dev/null script.sh` was refused.
        for flag in (a for a in arguments if a.startswith("-")):
            if flag.startswith("--"):
                if flag == "--command":
                    return _inline_code(program)
            elif "c" in flag[1:]:
                return _inline_code(program)
        positionals = [a for a in arguments if not a.startswith("-")]
        if positionals:
            return None
        return (
            Category.NOT_ALLOWLISTED,
            f"{program!r} with no script runs an interactive shell",
        )

    if program in _PYTHON:
        if len(arguments) >= 2 and arguments[0] == "-m":
            if arguments[1] in _ALLOWED_PYTHON_MODULES:
                return None
            # The module is plainly readable; we simply decline it.
            return (
                Category.NOT_ALLOWLISTED,
                f"{program} -m {arguments[1]!r} is not on cúramóir's allowlist; "
                "allowed modules: " + ", ".join(sorted(_ALLOWED_PYTHON_MODULES)),
            )
        return (
            Category.UNVERIFIABLE,
            f"{program!r} without `-m <module>` can run inline code the guard "
            "cannot verify",
        )

    if program in _INTERPRETERS:
        # Recognised and declined: these can run anything, so allowlisting them
        # would allowlist everything.
        return (
            Category.NOT_ALLOWLISTED,
            f"{program!r} can run arbitrary programs, so allowlisting it would "
            "allowlist everything",
        )

    if program not in ALLOWED:
        return (
            Category.NOT_ALLOWLISTED,
            f"{program!r} is not on cúramóir's allowlist for jobs",
        )

    subcommands = ALLOWED[program]
    if subcommands is None:
        return None

    for argument in arguments:
        if argument.startswith("-"):
            continue
        if argument in subcommands:
            return None
        return (
            Category.NOT_ALLOWLISTED,
            f"{program} {argument!r} is not on cúramóir's allowlist for jobs",
        )
    # No subcommand at all (`git`, `gh`) prints usage: harmless.
    return None


def judge_command(command, in_job):
    """Whether to block `command` during a cúramóir job.

    Allowlist first, then the denylist as a second pass over the same string.
    """
    if not in_job:
        return Decision(deny=False)

    command = command or ""

    # Before any parsing, because this bound exists to stop the parse. Lexing is
    # superlinear in length, a PreToolUse hook has 5s, and a command hook that
    # times out FAILS OPEN — so without the cap a padded command is a reliable
    # way to switch the guard off rather than merely a slow one.
    if len(command) > MAX_COMMAND_CHARS:
        return Decision(
            deny=True,
            reason=(
                f"blocked by cúramóir: this command is {len(command)} characters, "
                f"beyond the {MAX_COMMAND_CHARS} the guard will parse. It is "
                "refused unverified rather than left to time out, because a "
                "hook that times out permits the action."
            ),
            alternative="put a command this long in a committed script and run that",
            category=Category.UNVERIFIABLE,
        )

    tokens = lex(command)
    if tokens is None:
        return Decision(
            deny=True,
            reason=(
                "blocked by cúramóir: this command could not be parsed "
                "(unbalanced quoting), so the guard cannot verify what it "
                "would run."
            ),
            alternative="rewrite the command with balanced quoting",
            category=Category.UNPARSEABLE,
        )

    tokens, unterminated = _strip_heredoc_bodies(tokens)
    if unterminated:
        return Decision(
            deny=True,
            reason=(
                "blocked by cúramóir: this command opens a heredoc that is never "
                "terminated, so the guard cannot tell where the data ends and "
                "the next command begins."
            ),
            alternative="terminate the heredoc, or put the text in a file",
            category=Category.UNVERIFIABLE,
        )

    # Asked once, over the whole command, and over the UNQUOTED tokens so that
    # shell syntax inside single quotes is the literal text it is. Not inside
    # the segment loop: the question is "does any token contain a construction
    # the guard cannot read", which does not depend on where commands divide —
    # and keeping two differently-lexed token lists in step across heredoc
    # stripping and segmentation would be a seam with nothing holding it.
    #
    # Before redirections are stripped, too: a process substitution lexes as a
    # `<` operator followed by `(rm`, so stripping first would drop both and
    # leave `cat -rf /tmp/x` looking allowlisted.
    bare = unquoted(command)
    reason = _unverifiable(bare if bare is not None else tokens)
    if reason is not None:
        return Decision(
            deny=True,
            reason=(
                f"blocked by cúramóir: this command contains {reason}, so the "
                "guard cannot verify what it would run. Refused for that "
                "reason rather than because it is known to be destructive. "
                "Rewrite it without substitution, or run the inner command "
                "directly."
            ),
            alternative="run the inner command directly",
            category=Category.UNVERIFIABLE,
        )

    for segment in segments(tokens):
        segment = strip_redirections(segment)
        if not segment:
            continue

        refusal = _refusal_for(segment)
        if refusal is not None:
            category, reason = refusal
            return Decision(
                deny=True,
                reason=(
                    f"blocked by cúramóir: {reason}. A job may run only recognised "
                    "commands, because a guard that is behind on an allowlist "
                    "costs a retry while one behind on a denylist costs the "
                    "repository. If this command is legitimate, the list is "
                    "ALLOWED in curamoir/hooks/policy.py — widening it is a "
                    "code change, so file a bead rather than editing config."
                ),
                alternative=(
                    "file a bead to widen ALLOWED in curamoir/hooks/policy.py"
                ),
                category=category,
            )

    # Second pass: an allowlisted program can still be used destructively, and
    # judged on tokens so that the two passes can never disagree about where one
    # command ends and the next begins.
    return judge_destructive(tokens, in_job=True)
