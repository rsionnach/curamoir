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
commands in their own session are their business, except while the supervisor
lock exists (hooks/destructive.py says why).

**What it stops, and what it does not** (cm-hve, a decision of 2026-10-28).
It stops code composed at call time: a shell's or interpreter's inline code,
however it is reached (directly, through a package runner such as `uv run` or
`pnpm exec`, which may run only what the allowlist admits, through
`make --eval` or make variables, or through a heredoc); a package or image
fetched and run (`pnpm dlx`, `npm create`, `uv tool run`, `uv run --with`,
`docker run`, `go run pkg@version`); a program named in an option
(`go -toolexec`, `cargo --config`, `npm --script-shell`); a command written
into a git subcommand's options (`git rebase --exec`, `git fetch
--upload-pack`, `git submodule foreach`); a write the sweep's review of the
diff never sees, into `.git/`, outside the repository or through a symlink to
either (`> .git/hooks/x`, `cp`/`mv`/`tee`/`touch`/`mkdir` there, `sort -o`,
`git diff --output`, `pytest --junitxml`, `docker build --output`), or to a
glob the guard cannot resolve; words the shell rewrites before the program
sees them (`--{force,quiet}`, `$'...'`, zsh's `*(e:cmd:)` and `=(cmd)`);
and configuration whose value is a
command: `git -c`, any `git config` write outside a short list of safe keys,
`go env -w`, and any environment variable outside a short list (cm-z0k made
each of these an allowlist). It does NOT stop code the agent writes to a file
and then runs through an allowlisted runner: `pytest`, `make`, `go run`,
`cargo run` or `bash script.sh` run whatever the repository holds at that
moment, the agent's edits included. Telling a reviewed file from one written a
moment ago would not close that, since a test file is one too. What bounds it
is the sweep's scope review of every changed path (fix_scope), and, beneath
that, the README's word: guards are a ratchet against drift, not a security
boundary.

**Where it lives.** This module is the pipeline and what every program shares:
the unverifiable constructions, the environment and heredoc rules, ALLOWED (the
one allowlist of programs and their subcommands), and _refusal_for, which
judges one command. Each family's own checks, which run on an allowlisted
program before its subcommand is checked, are in _policy_git.py (git),
_policy_packages.py (uv, npm, pnpm, yarn) and _policy_tools.py (docker,
podman, go, make, cargo); every program's writes to paths it is given are
judged in _policy_writes.py; and the helpers they share, the outside-the-diff
rule among them, are in _policy_common.py.

It fails closed: an exception while judging, or runners nested more than
_MAX_NESTING deep, is a refusal, because a guard that raises is silent and a
silent PreToolUse hook permits the call.

Stdlib only, and no config read: this runs on every Bash call during a job.
The one fact from the repository it uses, package.json's script names, is read
by pretooluse and handed to judge_command as `scripts`.
"""

import re
from contextvars import ContextVar

# _INTERPRETERS, _PYTHON and _ASSIGNMENT are the pipeline's own lists; they
# are defined in _policy_common only because _is_code_runner and _make_code
# read them there, and importing them back from here would be a cycle.
from curamoir.hooks._policy_common import (
    _ASSIGNMENT,
    _CWD,
    _INTERPRETERS,
    _LEADING_OPTIONS,
    _PYTHON,
    _SCRIPTS,
    _UNQUOTED_GLOB,
    _has_unquoted_glob,
    _subcommand_at,
)
from curamoir.hooks._policy_git import (
    _git_command_setting,
    _git_inline_command,
    _git_writes,
)
from curamoir.hooks._policy_packages import _package_script, _runner_code
from curamoir.hooks._policy_tools import (
    _container_code,
    _go_code,
    _make_code,
    _program_option,
)
from curamoir.hooks._policy_writes import _redirection_outside_diff, _write_refusal
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
    # `<` then `(rm`, and a subshell group as `(rm`. Named on its own for the
    # message; an unquoted `(` anywhere else in a word is refused after this
    # table (_UNQUOTED_PARENTHESIS).
    (re.compile(r"^\("), "a subshell or process substitution `(...)`"),
    (re.compile(r"\$\{?[A-Za-z_]"), "a shell variable, whose value is not knowable here"),
)

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
    # npm accepts undocumented spellings (`npm exe` is `exec` on npm 11), so
    # its subcommands are listed, with their documented aliases, as git's
    # are (cm-f41). Not `config`/`c`: `npm config set script-shell` chooses
    # the shell every later `npm run` uses.
    "npm": {
        "test", "t", "tst", "run", "run-script", "rum", "urn", "start", "stop",
        "restart", "ci", "clean-install", "ic", "install-clean", "isntall-clean",
        "install", "i", "add", "in", "ins", "inst", "insta", "instal", "isnt",
        "isnta", "isntal", "isntall", "ls", "list", "outdated", "audit", "view",
        "info", "pack", "version", "help", "explain", "why", "fund", "doctor",
        "prune", "exec", "x", "init", "create", "innit",
    },
    # pnpm runs an unknown word as a script and then as `pnpm exec`, and yarn
    # has `yarn node` and `yarn workspace <name> <command>`, so both are
    # listed as npm is (R5 review, 2026-10-28). Not `config`: it sets the
    # shell every later script runs in. A script by its bare name (`pnpm lint`,
    # `yarn lint`) is refused: spell it `run lint`.
    "pnpm": {
        "install", "i", "add", "remove", "rm", "uninstall", "un", "update", "up",
        "upgrade", "run", "run-script", "test", "t", "start", "exec", "dlx", "create",
        "list", "ls", "outdated", "audit", "why", "prune", "fetch", "install-test", "it",
        "init", "rebuild", "rb", "licenses", "help",
    },
    "yarn": {
        "install", "add", "remove", "upgrade", "up", "run", "test", "start", "exec",
        "dlx", "create", "list", "info", "why", "outdated", "audit", "init", "version",
        "help", "dedupe",
    },
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

# `python3 -m <module>` runs these modules, and no others (_PYTHON, in
# _policy_common, is the interpreters it is allowed for).
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
        bare = _outside_double_quotes(token)
        if any(_BRACE_LIST.search(group.group()) for group in _BRACE_GROUP.finditer(bare)):
            return ("a brace expansion `{a,b}` or `{a..b}`, which the shell turns into "
                    "words the guard never sees")
        if "(" in bare:
            return _UNQUOTED_PARENTHESIS
    return None


#: What an unquoted `(` anywhere in a word is refused as (round 6). zsh, the
#: shell the Bash tool runs on macOS, reads `x(...)` as a glob with
#: qualifiers, and `e:cmd:` and `+cmd` among them run commands; `=(cmd)` is a
#: process substitution. git's format atoms are no exception: zsh can read
#: `--format=%(refname:fe:id:)` as qualifiers that run `id`, and no rule over
#: the atom's text closes that, so `%(` is refused unquoted too and the
#: refusal says to quote it (`--format='%(refname:short)'`). The sweep's own
#: git calls run from Python, not through this hook.
_UNQUOTED_PARENTHESIS = (
    "an unquoted `(` in a word, which zsh reads as a glob qualifier "
    "(`*(e:cmd:)` runs cmd) or a process substitution (`=(cmd)`)"
)


#: An unquoted brace expansion: braces holding a `,` or a `..` (R5 edge-case
#: review). The shell expands it before the program runs, so
#: `git push origin main --{force,quiet}` is `--force --quiet` and
#: `git reset {--hard,}` is `git reset --hard`, while the guard read one word
#: that is neither. `{}` alone is not one, which `find -exec cmd {} ;` needs.
_BRACE_GROUP = re.compile(r"\{[^{}]*\}")
#: Found in two steps, a group and then a list inside it, so the search is
#: linear: one pattern with `[^{}]*` either side of the `,` backtracked
#: quadratically over a long run of commas (round 6).
_BRACE_LIST = re.compile(r",|\.\.")


def _outside_double_quotes(token):
    """`token` (from `unquoted`, single-quoted spans already blanked) with its
    double-quoted spans and backslash-escaped characters blanked, since the
    shell expands no braces in either. A scan rather than a regex for the
    reason _shell._blank_single_quoted gives: an escaped quote does not open
    a span, and a regex would read `\\"{a,b}\\"` as quoted."""
    kept, in_double, escaped = [], False, False
    for character in token:
        if escaped:
            escaped = False
            kept.append("_")
            continue
        if character == "\\":
            escaped = True
            kept.append("_")
            continue
        if character == '"':
            in_double = not in_double
            kept.append("_")
            continue
        kept.append("_" if in_double else character)
    return "".join(kept)


def _ansi_c_or_locale_quoting(command):
    """Whether `command` uses `$'...'` or `$"..."` outside quotes (R5
    edge-case review). `$'...'` decodes escapes, so `git reset -$'-hard'` is
    `git reset --hard` and `$'\\x2d'` is any character at all; `$"..."` is
    translated by the locale. Scanned over the raw command, because by the
    time `unquoted` has blanked the single-quoted span the `$` has nothing
    after it. Inside double quotes neither is special (`"costs $"` is a
    literal dollar), and inside single quotes nothing is."""
    in_single = in_double = escaped = False
    for index, character in enumerate(command):
        if escaped:
            escaped = False
            continue
        if character == "\\" and not in_single:
            escaped = True
            continue
        if character == "'" and not in_double:
            in_single = not in_single
            continue
        if character == '"' and not in_single:
            in_double = not in_double
            continue
        if (character == "$" and not in_single and not in_double
                and command[index + 1:index + 2] in ("'", '"')):
            return True
    return False


#: Arguments that hand a program its own text to run, rather than naming a file
#: that someone reviewed.
_INLINE_INPUT = frozenset({"<<", "<<-", "<<<", "-s", "/dev/stdin", "-"})

#: Options whose VALUE is program text rather than data. A shell is not the only
#: program that will run what it is handed on stdin: `make -f -` reads a
#: makefile whose recipes are shell, `docker build -f -` a Dockerfile whose RUN
#: lines are shell, `pip install -r /dev/stdin` a requirements file.
#:
#: This is NOT the "`make` runs repo-controlled code, which is an accepted
#: trade" case: that trade is about a Makefile in the repository. Text composed
#: at call time is the line this module draws.
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

# cm-hve, rewritten allowlist-first by cm-z0k after a security review got
# past the first version's lists of dangerous spellings: an abbreviated
# `make --ev=`, a `--get` after the value, an option value hiding `dlx`, an
# unlisted environment variable. Each rule below, and each in the _policy_*
# modules, names what is ALLOWED and refuses the rest, as the rest of this
# module does.

#: Environment variables a job may set in an assignment prefix. Any other
#: name is refused: a variable's value can be a command (GIT_PAGER, EDITOR),
#: a library to load (LD_PRELOAD), flags (GOFLAGS=-toolexec=..., MAKEFLAGS)
#: or a config location (HOME, GIT_CONFIG_*), and listing those is the
#: denylist problem. PYTHONPATH is here because the sweep's own test command
#: sets it; the code it reaches is the repository's, the door `pytest`
#: already is.
_ALLOWED_ENV = frozenset({
    "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONHASHSEED",
    "PYTHONIOENCODING", "CI", "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "TERM", "COLUMNS",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "NODE_ENV", "RUST_BACKTRACE",
})

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
                "script file and run that instead",
            )
        if name in _PROGRAM_TEXT_OPTIONS and value in _INLINE_INPUT:
            return (
                Category.UNVERIFIABLE,
                f"{program!r} is being handed its {name} from stdin, so the "
                "guard cannot read what it would run. Put it in a "
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


#: The program-specific checks of cm-hve and cm-z0k, run on an allowlisted
#: program's arguments before its subcommand is checked, in order, the first
#: refusal winning. Each is called as `check(program, arguments, judge)`,
#: where `judge` is _nested_refusal (_refusal_for, one runner deeper): a
#: runner's checks judge the program it runs by the whole policy, and taking
#: the judge as an argument is what lets the family modules do that without
#: importing this one.
_PROGRAM_CHECKS = {
    "git": (_git_command_setting, _git_inline_command, _git_writes),
    "make": (_make_code,),
    "go": (_go_code,),
    "cargo": (_program_option,),
    "uv": (_runner_code,),
    "npm": (_package_script, _runner_code),
    "pnpm": (_package_script, _runner_code),
    "yarn": (_package_script, _runner_code),
    "docker": (_container_code,),
    "podman": (_container_code,),
}


def _inline_code_refusal(program):
    """The refusal for a shell given inline code with `-c` or `--command`."""
    return (
        Category.UNVERIFIABLE,
        f"{program!r} with -c runs code composed at call time, which the "
        "guard cannot verify. Put the script in a file and run that instead",
    )


#: How many runners deep a command may nest (`uv run pnpm exec yarn run ...`),
#: each judging the next through _nested_refusal. Far above anything a job
#: runs; it exists so that a deep enough nesting is a refusal and not a
#: RecursionError (R5 edge-case review).
_MAX_NESTING = 8

#: How many runners deep the command now being judged is.
_NESTING = ContextVar("curamoir_policy_nesting", default=0)


def _nested_refusal(tokens):
    """_refusal_for one runner deeper: the `judge` every program check is
    handed for the program a runner runs. Past _MAX_NESTING, a refusal."""
    depth = _NESTING.get() + 1
    if depth > _MAX_NESTING:
        return (
            Category.UNVERIFIABLE,
            f"runners nested more than {_MAX_NESTING} deep are more than the guard "
            "will follow, so it cannot verify what the innermost one runs",
        )
    token = _NESTING.set(depth)
    try:
        return _refusal_for(tokens)
    finally:
        _NESTING.reset(token)


def _refusal_for(tokens):
    """(Category, reason) if this command must be refused, else None.

    The category is returned, never inferred from the message: prose is not a
    safe way to tell outcomes apart.

    UNVERIFIABLE is reserved for cases where the guard genuinely cannot determine
    the effect — inline code, a bare interpreter. A program we recognise and
    decline is NOT_ALLOWLISTED, because we read it perfectly well and said no.
    """
    # Drop leading assignment prefixes so the real program is judged, unless
    # the variable's value is itself a command.
    while tokens and _ASSIGNMENT.match(tokens[0]):
        name = tokens[0].split("=", 1)[0]
        if name not in _ALLOWED_ENV:
            return (
                Category.UNVERIFIABLE,
                f"setting {name} is not on cúramóir's list of variables a job may "
                "set: a variable's value can be a command, a library to load or "
                "a config to read, chosen at call time. Allowed: "
                + ", ".join(sorted(_ALLOWED_ENV)),
            )
        tokens = tokens[1:]
    if not tokens:
        return None

    program = tokens[0].strip("\"'").rsplit("/", 1)[-1]
    arguments = tokens[1:]

    fed_inline = _inline_program_text(program, arguments)
    if fed_inline is not None:
        return fed_inline

    if program in SHELLS:
        # A script FILE may run; inline code may not.
        #
        # The line is a file in the repository versus text composed at call
        # time. cúramóir already allows `pytest`, `make` and `npm`, each of
        # which runs whatever code the repository holds, so a script file is
        # no worse. Whether that file is committed is NOT checked: the agent
        # can write one and run it, as it can write a test and run pytest
        # (module docstring, "What it stops, and what it does not").
        #
        # `-c` is only the best-known route to inline code. A heredoc, a
        # here-string, `-s` and `/dev/stdin` all feed a shell code composed at
        # call time, and each of those reads as "a positional", so testing for
        # a positional read `bash <<EOF` as a script file.
        if _INLINE_INPUT.intersection(arguments):
            return (
                Category.UNVERIFIABLE,
                f"{program!r} is being fed code to run inline rather than a "
                "script file, which the guard cannot verify. Put the "
                "script in a file and run that instead",
            )
        # Match `-c` exactly for a long flag, and character by character for a
        # clustered short one. `"c" in flag` matched any flag whose NAME
        # contains a c, so `bash --rcfile /dev/null script.sh` was refused.
        for flag in (a for a in arguments if a.startswith("-")):
            if flag.startswith("--"):
                if flag == "--command":
                    return _inline_code_refusal(program)
            elif "c" in flag[1:]:
                return _inline_code_refusal(program)
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
                # `python3 -m pytest` writes where `pytest` does.
                return _write_refusal(arguments[1], arguments[2:])
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

    refused = _write_refusal(program, arguments)
    if refused is not None:
        return refused

    for check in _PROGRAM_CHECKS.get(program, ()):
        refused = check(program, arguments, _nested_refusal)
        if refused is not None:
            return refused

    subcommands = ALLOWED[program]
    if subcommands is None:
        return None

    if program in _LEADING_OPTIONS:
        at, refused = _subcommand_at(program, arguments)
        if refused is not None:
            return refused
        arguments = arguments[at:] if at is not None else []
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


def judge_command(command, in_job, scripts=None, cwd=None):
    """Whether to block `command` during a cúramóir job.

    Allowlist first, then the denylist as a second pass over the same string.
    `scripts` is the repository's package.json script names, when the caller
    could read them (pretooluse does); without them a `yarn run <name>` is
    judged as the binary it may be. It affects only `yarn run`: npm and pnpm
    run nothing but a script by that name.

    `cwd` is the directory the command runs in, when the caller knows it
    (pretooluse does): with it, a write's absolute path inside it is judged as
    the relative one, and a write through a symlink is refused; without it,
    every absolute path is outside and symlinks are not looked for.

    `scripts` and `cwd` are held in ContextVars (_SCRIPTS, _CWD) for the
    length of the call rather than passed down, because the judgement recurses: _refusal_for runs
    a program's checks, which run _refusal_for on the program a runner runs,
    and a parameter would be threaded through every check to reach the one
    place that reads it.
    """
    if not in_job:
        return Decision(deny=False)
    token = _SCRIPTS.set(_script_names(scripts))
    cwd_token = _CWD.set(cwd if isinstance(cwd, str) and cwd else None)
    glob_token = _UNQUOTED_GLOB.set(True)
    try:
        return _judge(command)
    except Exception as error:
        # Fail closed (R5 edge-case review). A guard that raises protects
        # nothing: pretooluse would exit silently and a silent PreToolUse hook
        # PERMITS the call, so `uv run uv run ... rm -rf /` deep enough to hit
        # the recursion limit ran unjudged. RecursionError is an Exception.
        return Decision(
            deny=True,
            reason=(
                "blocked by cúramóir: the guard could not judge this command "
                f"({type(error).__name__}), so it is refused rather than allowed"
            ),
            alternative="simplify the command",
            category=Category.UNVERIFIABLE,
        )
    finally:
        _UNQUOTED_GLOB.reset(glob_token)
        _CWD.reset(cwd_token)
        _SCRIPTS.reset(token)


def _script_names(scripts):
    """`scripts` as a frozenset of names, or None (unknown, so yarn is judged
    strictly) when it is not a set, frozenset, tuple or list of strings. A
    string is not taken as a collection: frozenset("build") is five letters,
    and `yarn run b` would then pass (M7)."""
    if not isinstance(scripts, (set, frozenset, tuple, list)):
        return None
    if not all(isinstance(name, str) for name in scripts):
        return None
    return frozenset(scripts)


def _judge(command):
    """judge_command's verdict for a command run inside a job."""

    command = command or ""
    _UNQUOTED_GLOB.set(_has_unquoted_glob(command))

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
            alternative="put a command this long in a script file and run that",
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
    if reason is None and _ansi_c_or_locale_quoting(command):
        reason = "ANSI-C or locale quoting (`$'...'`, `$\"...\"`), which the shell decodes"
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
            alternative=(
                "quote the format string, or the word holding the parenthesis "
                "(`--format='%(refname:short)'`)"
                if reason == _UNQUOTED_PARENTHESIS else "run the inner command directly"),
            category=Category.UNVERIFIABLE,
        )

    for segment in segments(tokens):
        # The redirection targets are read before strip_redirections drops
        # them: where the output goes is the effect (I1).
        refusal = _redirection_outside_diff(segment)
        segment = strip_redirections(segment)
        if refusal is None and not segment:
            continue

        if refusal is None:
            refusal = _refusal_for(segment)
        if refusal is not None:
            # A refusal may name its own alternative as a third element; the
            # default is the allowlist's own remedy.
            category, reason, *named = refusal
            return Decision(
                deny=True,
                reason=(
                    f"blocked by cúramóir: {reason}. A job may run only recognised "
                    "commands, because a guard that is behind on an allowlist "
                    "costs a retry while one behind on a denylist costs the "
                    "repository. If this command is legitimate, the lists are "
                    "the tables in curamoir/hooks/policy.py and its _policy_*.py "
                    "modules — widening one is a code change, so file a bead "
                    "rather than editing config."
                ),
                alternative=(named[0] if named else
                             "file a bead to widen the tables in curamoir/hooks/policy.py "
                             "and its _policy_*.py modules"),
                category=category,
            )

    # Second pass: an allowlisted program can still be used destructively, and
    # judged on tokens so that the two passes can never disagree about where one
    # command ends and the next begins.
    return judge_destructive(tokens, in_job=True)
