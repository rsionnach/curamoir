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
`docker run`); a program named in an option (`go -toolexec`, `cargo
--config`); and configuration whose value is a command: `git -c`, any
`git config` write outside a short list of safe keys, and any environment
variable outside a short list (cm-z0k made each of these an allowlist). It does NOT stop code the agent writes to a file and
then runs through an allowlisted runner: `pytest`, `make`, `go run`,
`cargo run` or `bash script.sh` run whatever the repository holds at that
moment, the agent's edits included. Telling a reviewed file from one written a
moment ago would not close that, since a test file is one too. What bounds it
is the sweep's scope review of every changed path (fix_scope), and, beneath
that, the README's word: guards are a ratchet against drift, not a security
boundary.

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
# unlisted environment variable. Each rule below names what is ALLOWED and
# refuses the rest, as the rest of this module does.

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

#: The programs `uv run`, `npm exec`, `pnpm exec` and `yarn exec` may run:
#: what the allowlist would admit on its own, judged by _refusal_for. So
#: `uv run python -c` is refused as `python -c` is, and `uv run cowsay` as
#: `cowsay` is.
_RUN_SUBCOMMANDS = {
    "uv": frozenset({"run"}), "npm": frozenset({"exec", "x"}),
    "pnpm": frozenset({"exec"}), "yarn": frozenset({"exec"}),
}

#: Options of those runners that fetch packages into the run: `uv run --with
#: pkg pkg-cli` runs code nobody reviewed, chosen at call time.
_FETCHING_OPTIONS = ("--with", "--with-requirements", "--with-editable")

#: Subcommands that fetch a package and run it. `npm init <pkg>` and
#: `npm create <pkg>` run create-<pkg>; `uv tool run` is `uvx`, which the
#: allowlist never admitted. Found among the first two words, because an
#: option's value can come first (`pnpm --dir web dlx pkg`).
_FETCH_AND_RUN = {
    "pnpm": {"dlx", "create"}, "yarn": {"dlx", "create"},
    "npm": {"init", "innit", "create"},
}

#: Options that may come BEFORE a program's subcommand, as (flag-only,
#: value-taking), cm-f41. The subcommand is found by reading past exactly
#: these, a value-taking one together with its value (`-C dir` or
#: `--git-dir=dir`); any other option there is refused, because without its
#: arity its value would be taken for the subcommand (`git -C log config
#: core.pager x` ran `config` while the checks saw `log`). The same index
#: drives every check that depends on the subcommand.
_LEADING_OPTIONS = {
    "git": (frozenset({"--no-pager", "-P", "--paginate", "-p", "--bare",
                       "--no-replace-objects", "--literal-pathspecs", "--glob-pathspecs",
                       "--noglob-pathspecs", "--icase-pathspecs", "--no-optional-locks",
                       "--version", "--help"}),
            frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})),
    "docker": (frozenset({"--debug", "-D"}),
               frozenset({"--context", "-c", "--host", "-H", "--log-level", "-l"})),
    "podman": (frozenset({"--debug"}), frozenset({"--log-level", "--connection", "-c"})),
    "compose": (frozenset(), frozenset({"-f", "--file", "-p", "--project-name", "--profile",
                                        "--env-file", "--project-directory"})),
    "npm": (frozenset({"-s", "--silent", "-q", "--quiet", "-ws", "--workspaces",
                       "--no-audit", "--no-fund", "--ignore-scripts", "--offline"}),
            frozenset({"--prefix", "-w", "--workspace", "--loglevel"})),
    "pnpm": (frozenset({"-r", "--recursive", "-s", "--silent"}),
             frozenset({"--dir", "-C", "--filter", "-F"})),
    "yarn": (frozenset({"-s", "--silent"}), frozenset({"--cwd"})),
    "uv": (frozenset({"-q", "--quiet", "-v", "--verbose", "--offline", "--no-cache",
                      "--no-progress"}),
           frozenset({"--directory", "--project"})),
}

#: Options that may come between a runner's run subcommand and the program
#: it runs, by the same rule. uv run's `-m` makes the program a module name,
#: judged as a program (so `uv run -m pytest` passes and `-m evil` does
#: not). Absent on purpose: --with* (fetching), --index-url and the other
#: index options (a source of packages), npm's --package/-p and --yes.
_RUN_OPTIONS = {
    "uv": (frozenset({"--frozen", "--locked", "--no-sync", "--isolated", "--no-project",
                      "--all-extras", "--no-dev", "-q", "--quiet", "-m", "--module"}),
           frozenset({"--extra", "--group", "--only-group", "--package", "--python", "-p",
                      "--env-file"})),
    "npm": (frozenset({"--no", "-ws", "--workspaces", "-s", "--silent", "-q", "--quiet"}),
            frozenset({"-w", "--workspace"})),
    "pnpm": (frozenset({"-r", "--recursive", "--parallel", "-s", "--silent"}),
             frozenset({"--filter", "-F", "--dir", "-C"})),
    "yarn": (frozenset(), frozenset()),
}

#: `docker compose` subcommands a job may run, by the container rule:
#: `run`, `exec` and `up` start an image's code.
_COMPOSE_SUBCOMMANDS = frozenset({
    "build", "ps", "config", "logs", "images", "ls", "version", "top", "port",
})

#: docker and podman subcommands a job may run: building from the
#: repository's Dockerfile and reading state. `run`, `exec` and the rest run
#: an image's code, fetched at call time, which is fetch-and-run.
_CONTAINER_SUBCOMMANDS = frozenset({
    "build", "ps", "images", "image", "inspect", "logs", "version", "info", "compose",
})

#: git settings a job may WRITE with `git config`. Any other key is refused:
#: a key's value can be a command git runs on the next ordinary call
#: (core.pager, core.sshCommand, core.hooksPath, filters, aliases), a config
#: to include, or a transport to allow, and listing those is the denylist
#: problem. Lowercase, as git compares keys.
_GIT_WRITABLE_KEY = re.compile(
    r"user\.(name|email)|core\.(autocrlf|filemode|ignorecase|quotepath|safecrlf|eol)"
    r"|color\..+|advice\..+|init\.defaultbranch|pull\.(rebase|ff)"
    r"|push\.(default|autosetupremote)|(commit|tag)\.gpgsign|merge\.conflictstyle"
    r"|rerere\.enabled|branch\.[^.]+\.(remote|merge|rebase)"
)

#: `git config` options that make it a read. Only options BEFORE the first
#: positional count: git config stops parsing options there, so in
#: `git config core.pager 'sh -c id' --get` the `--get` is a value pattern
#: and the call SETS core.pager (checked against git 2.39, cm-z0k).
_GIT_CONFIG_READS = frozenset({
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color",
    "--get-colorbool", "--list", "-l",
})
#: Every other option `git config` may be given during a job (cm-f41): scope,
#: type and output flags, `--type=` only in its `=` form. Any other option
#: is refused, among them -f/--file and --blob (another file, and a value read
#: as the key), --default, --comment and --value, and `--type` with a
#: separate value, which was read as the key.
_GIT_CONFIG_OPTIONS = frozenset({
    "--local", "--global", "--system", "--worktree", "--bool", "--int", "--bool-or-int",
    "--path", "--expiry-date", "--show-origin", "--show-scope", "--name-only", "-z",
    "--null", "--includes", "--no-includes", "--fixed-value", "--type",
})
#: `git config` options that write, and the new-style subcommands.
_GIT_CONFIG_WRITES = frozenset({
    "--add", "--replace-all", "--unset", "--unset-all", "--rename-section",
    "--remove-section", "--edit", "-e",
})
_GIT_CONFIG_SUBCOMMANDS = {"get": "read", "list": "read", "set": "write", "unset": "write",
                           "rename-section": "refuse", "remove-section": "refuse",
                           "edit": "refuse"}

#: Options whose value is a program to run, per program, matched however the
#: option is spelled (`-toolexec=x`, `--toolexec x`): go's flag package takes
#: one dash or two, and cargo's `--config` can set a target's runner.
_PROGRAM_OPTIONS = {
    "go": ("toolexec", "exec"),
    "cargo": ("config",),
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


def _leading(program, arguments, table=None):
    """(index of the subcommand in `arguments`, or None if there is none;
    refusal or None), reading past only the options `table` (default
    _LEADING_OPTIONS) gives `program`."""
    flags, valued = (table or _LEADING_OPTIONS)[program]
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-") or argument == "-":
            return index, None
        if argument == "--":
            following = index + 1
            return (following if following < len(arguments) else None), None
        name, equals, _ = argument.partition("=")
        if name in valued:
            index += 1 if equals else 2
            continue
        if not equals and argument in flags:
            index += 1
            continue
        return None, (
            Category.NOT_ALLOWLISTED,
            f"{program} {name} is not an option cúramóir knows how to read past at "
            "this point, so it cannot tell which word is the subcommand or program "
            "(an option's value can look like one)",
        )
    return None, None


def _wrapped_code(program, arguments):
    """A refusal if a package runner fetches code to run, or runs a program
    the allowlist would refuse on its own; or if docker/podman would run a
    container; else None. The subcommand is found by _leading (cm-f41)."""
    at, refused = _leading(program, arguments)
    if refused is not None or at is None:
        return refused
    subcommand, after = arguments[at], arguments[at + 1:]
    if program in ("docker", "podman"):
        if subcommand not in _CONTAINER_SUBCOMMANDS:
            return (
                Category.NOT_ALLOWLISTED,
                f"{program} {subcommand!r} runs an image's code, fetched at call time; "
                f"a job may use {program} only to "
                + ", ".join(sorted(_CONTAINER_SUBCOMMANDS)),
            )
        if subcommand == "compose":
            inner, refused = _leading("compose", after)
            if refused is not None:
                return refused
            if inner is not None and after[inner] not in _COMPOSE_SUBCOMMANDS:
                return (
                    Category.NOT_ALLOWLISTED,
                    f"{program} compose {after[inner]!r} runs a service's image; a job may "
                    "use compose only to " + ", ".join(sorted(_COMPOSE_SUBCOMMANDS)),
                )
        return None
    following = [a for a in after if not a.startswith("-")]
    if subcommand in _FETCH_AND_RUN.get(program, ()) and (subcommand == "dlx" or following):
        return (
            Category.NOT_ALLOWLISTED,
            f"{program} {subcommand} fetches a package and runs it, code nobody "
            "reviewed and chosen at call time",
        )
    if program == "uv" and subcommand == "tool" and following[:1] == ["run"]:
        return (
            Category.NOT_ALLOWLISTED,
            "uv tool run fetches a package and runs it, code nobody reviewed "
            "and chosen at call time",
        )
    if subcommand not in _RUN_SUBCOMMANDS.get(program, ()):
        return None
    if program == "npm":
        return _npm_exec(subcommand, after)
    for argument in after:
        if argument.split("=", 1)[0] in _FETCHING_OPTIONS:
            return (
                Category.NOT_ALLOWLISTED,
                f"{program} {subcommand} {argument.split('=', 1)[0]} fetches a package "
                "into the run, code nobody reviewed and chosen at call time",
            )
    start, refused = _leading(program, after, _RUN_OPTIONS)
    if refused is not None:
        return refused
    if start is None:
        return None
    # The program run must pass the allowlist on its own.
    return _refusal_for(after[start:])


def _npm_exec(subcommand, after):
    """A refusal for `npm exec`/`npm x` with `after` (what follows the
    subcommand), else None (cm-hhr, cm-f41).

    npm reads an option ANYWHERE before a literal `--` as its own, after the
    program name too (`npm exec --no node --version` prints npm's version,
    npm 11.9.0), so `npm exec --no tsc --yes` would install after all. Every
    option before `--` is therefore read past with npm's _RUN_OPTIONS or
    refused, `--no` must be among them, and arguments for the program go
    after `--`."""
    flags, valued = _RUN_OPTIONS["npm"]
    words, options, index = [], set(), 0
    while index < len(after):
        argument = after[index]
        if argument == "--":
            words.extend(after[index + 1:])
            break
        if not argument.startswith("-"):
            words.append(argument)
            index += 1
            continue
        name, equals, _ = argument.partition("=")
        if name in valued:
            options.add(name)
            index += 1 if equals else 2
            continue
        if not equals and argument in flags:
            options.add(argument)
            index += 1
            continue
        return (
            Category.NOT_ALLOWLISTED,
            f"npm {subcommand} reads {name} as its own option wherever it comes before "
            "`--`, and it is not one a job may give npm",
            f"npm {subcommand} --no -- <program> <its arguments>",
        )
    if "--no" not in options:
        return _npm_without_no(subcommand)
    if not words:
        return None
    # The program run must pass the allowlist on its own.
    return _refusal_for(words)


def _npm_without_no(subcommand):
    return (
        Category.NOT_ALLOWLISTED,
        f"npm {subcommand} without --no (and without --yes) before the program fetches "
        "and runs a package that is not installed, code nobody reviewed",
        f"npm {subcommand} --no <program>, which runs only what is installed",
    )


def _make_code(arguments):
    """A refusal if make is given a makefile or variables at call time:
    `--eval` (and any prefix of it GNU make accepts, `--ev`), `-E` alone or
    in a cluster (`-sE`), or a `NAME=value` on its command line, which
    overrides the makefile (SHELL, CC and the like are commands)."""
    for argument in arguments:
        if argument.startswith("--"):
            name = argument.split("=", 1)[0]
            if len(name) >= 4 and "--eval".startswith(name):
                return (
                    Category.UNVERIFIABLE,
                    f"make {name} runs a makefile written at call time, and its "
                    "recipes are shell. Put the rule in the Makefile instead",
                )
        elif argument.startswith("-") and "E" in argument[1:]:
            return (
                Category.UNVERIFIABLE,
                f"make {argument} includes -E, a makefile written at call time whose "
                "recipes are shell. Put the rule in the Makefile instead",
            )
        elif _ASSIGNMENT.match(argument):
            return (
                Category.UNVERIFIABLE,
                f"make {argument.split('=', 1)[0]}=... overrides the Makefile at call "
                "time, and a variable such as SHELL or CC is a command. Run make "
                "without command-line variables",
            )
    return None


def _program_option(program, arguments):
    """A refusal if an option whose value is a program (_PROGRAM_OPTIONS) is
    given, however it is spelled, else None."""
    names = _PROGRAM_OPTIONS.get(program, ())
    for argument in arguments:
        if not argument.startswith("-"):
            continue
        name = argument.lstrip("-").split("=", 1)[0]
        if name in names:
            return (
                Category.UNVERIFIABLE,
                f"{program} -{name} names a program for {program} to run, chosen at "
                f"call time. Run {program} without it",
            )
    return None


def _git_command_setting(arguments):
    """A refusal if git is given configuration for this call (`-c`,
    `--config-env`) or its helpers' directory (`--exec-path`), or if
    `git config` writes a key not on _GIT_WRITABLE_KEY, renames, removes or
    edits sections, or is given an option not on _GIT_CONFIG_OPTIONS; else
    None."""
    for argument in arguments:
        if not argument.startswith("-"):
            break
        if argument.startswith(("-c", "--config-env", "--exec-path")):
            return (
                Category.UNVERIFIABLE,
                f"git {argument.split('=', 1)[0]} changes git's configuration or "
                "helper programs for this call, and a setting such as core.pager "
                "or core.sshCommand is a command git runs. Run the git command "
                "without it",
            )
    at, refused = _leading("git", arguments)
    if refused is not None or at is None or arguments[at] != "config":
        return refused
    after = arguments[at + 1:]
    # Options up to the first positional, as git reads them; everything from
    # there on is a positional, whatever it looks like.
    options, rest = set(), []
    for index, argument in enumerate(after):
        if not argument.startswith("-"):
            rest = after[index:]
            break
        options.add(argument.split("=", 1)[0])
        if argument == "--type":
            # `--type` with its value apart: the value would be read as the key.
            options.add("--type <value>")
    mode = _GIT_CONFIG_SUBCOMMANDS.get(rest[0]) if rest else None
    if mode is not None:
        rest = rest[1:]
    if mode == "refuse" or options & {"--rename-section", "--remove-section", "--edit", "-e"}:
        return (
            Category.NOT_ALLOWLISTED,
            "git config may not rename, remove or edit sections during a job: that "
            "can move a key into a setting git runs",
        )
    unknown = options - _GIT_CONFIG_OPTIONS - _GIT_CONFIG_READS - _GIT_CONFIG_WRITES
    if unknown:
        return (
            Category.NOT_ALLOWLISTED,
            f"git config {sorted(unknown)[0]} is not an option a job may give it: a "
            "file, blob or value option can make another word the key, or write "
            "somewhere git reads settings from",
        )
    if mode is None:
        if options & _GIT_CONFIG_READS:
            mode = "read"
        elif options & _GIT_CONFIG_WRITES or len(rest) >= 2:
            mode = "write"
        else:
            mode = "read"
    if mode == "read":
        return None
    key = rest[0].lower() if rest else ""
    if _GIT_WRITABLE_KEY.fullmatch(key):
        return None
    return (
        Category.UNVERIFIABLE,
        f"git config may write only known-safe settings during a job, and "
        f"{key or 'this setting'!r} is not one: a setting's value can be a command "
        "git runs on the next ordinary call (core.pager, core.sshCommand, "
        "core.hooksPath). Leave the repository's git configuration as it is",
    )


#: The program-specific checks of cm-hve and cm-z0k, run on an allowlisted
#: program's arguments before its subcommand is checked.
_PROGRAM_CHECKS = {
    "git": (_git_command_setting,),
    "make": (_make_code,),
    "go": (lambda arguments: _program_option("go", arguments),),
    "cargo": (lambda arguments: _program_option("cargo", arguments),),
    **{runner: ((lambda arguments, runner=runner: _wrapped_code(runner, arguments)),)
       for runner in ("uv", "npm", "pnpm", "yarn", "docker", "podman")},
}


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

    for check in _PROGRAM_CHECKS.get(program, ()):
        refused = check(arguments)
        if refused is not None:
            return refused

    subcommands = ALLOWED[program]
    if subcommands is None:
        return None

    if program in _LEADING_OPTIONS:
        at, refused = _leading(program, arguments)
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
            # A refusal may name its own alternative as a third element; the
            # default is the allowlist's own remedy.
            category, reason, *named = refusal
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
                alternative=(named[0] if named else
                             "file a bead to widen ALLOWED in curamoir/hooks/policy.py"),
                category=category,
            )

    # Second pass: an allowlisted program can still be used destructively, and
    # judged on tokens so that the two passes can never disagree about where one
    # command ends and the next begins.
    return judge_destructive(tokens, in_job=True)
