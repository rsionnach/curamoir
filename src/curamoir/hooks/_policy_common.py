"""The pieces of the job-time command policy that every program family shares.

policy.py is the pipeline and the spec of record; the program families' checks
live in _policy_git.py, _policy_packages.py and _policy_tools.py, and this
module holds what more than one of them reads: how a program's subcommand is
found, how an abbreviated long option is recognised, which words name a
program that runs code, and the package.json script names for the command
being judged. It imports nothing from policy.py, so the families can import it
and policy.py can import them without a cycle; the one thing a family needs
from the pipeline, the recursive judge, is handed to each check as an argument.

Stdlib only, and no config read, as policy.py.
"""

import re
from contextvars import ContextVar

from curamoir.hooks._shell import SHELLS
from curamoir.hooks.verdict import Category

# `NAME=value` before the program is a shell ASSIGNMENT PREFIX, not a command:
# the sweep's own test command is `PYTHONPATH=src python3 -m pytest -q` (E3-R4).
# A variable REFERENCE (`$R`) is still unverifiable; an assignment is not.
# Here rather than in policy.py because make's check reads it too: a
# `NAME=value` on make's command line overrides the Makefile.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Programs that can run anything, so allowlisting them would allowlist everything.
# The pipeline in policy.py refuses them; here because _is_code_runner reads
# them too, and policy.py re-exports the name.
_INTERPRETERS = {
    "eval", "exec", "source", ".", "env", "python", "python3", "python2",
    "perl", "ruby", "node", "deno", "bun", "php", "awk", "xargs", "parallel",
    "sudo", "doas", "su", "ssh", "nohup", "setsid", "script",
}

# `python3 -m <module>` is how the test runner and our own scripts are invoked,
# so it is allowed even though a bare interpreter is not: `-m` names a module
# rather than inline code. A fixed set, because it is the list of names that
# policy.py lets run `-m`; here, and re-exported there, for _is_code_runner.
_PYTHON = {"python", "python3", "python2"}

#: The repository's package.json script names for the command being judged
#: (judge_command's `scripts`), or None when they are not known. Only yarn
#: needs them: `yarn run <name>` runs an installed binary when <name> is not
#: a script, so a name that is not one is judged as `yarn exec` judges it.
#:
#: A ContextVar rather than a parameter because the policy is recursive:
#: _refusal_for calls a program's checks, which call _refusal_for again on
#: the program a runner runs (`uv run`, `npm exec`, `yarn run`), and a
#: parameter would have to be threaded through every check and every level
#: to reach the one place that reads it. judge_command sets it and resets it
#: on the way out.
_SCRIPTS = ContextVar("curamoir_policy_scripts", default=None)


# Tables of options, one shape for all of them (cm-f41). Each maps a program
# to a pair `(flags, valued)`: `flags` are the options that take no value,
# `valued` the options that take one, either attached (`--git-dir=dir`) or as
# the next word (`-C dir`). A reader of such a table (_subcommand_at, and
# _npm_exec in _policy_packages) reads past exactly these, a valued one
# together with its value, and refuses any other option where they are read:
# without an option's arity its value would be taken for the subcommand or the
# program (`git -C log config core.pager x` ran `config` while the checks saw
# `log`). The tables of this shape are _LEADING_OPTIONS below and
# _RUN_OPTIONS and _SCRIPT_OPTIONS in _policy_packages. The same rule finds
# the word in each; _VALUED_AFTER_SUBCOMMAND (_policy_packages) is not of this
# shape, and is read leniently by _positionals.

#: Options that may come BEFORE a program's subcommand. `compose` is not a
#: program in ALLOWED but a pseudo-program key: `docker compose` and
#: `podman compose` have options of their own before their subcommand.
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
    # go's only option before its subcommand, in either dash spelling.
    "go": (frozenset(), frozenset({"-C", "--C"})),
}


def _subcommand_at(program, arguments, table=_LEADING_OPTIONS):
    """(index of the subcommand in `arguments`, or None if there is none;
    refusal or None), reading past only the options `table` gives `program`
    (the `(flags, valued)` shape above). With _RUN_OPTIONS or _SCRIPT_OPTIONS
    as the table, the word found is the program or script a runner runs."""
    flags, valued = table[program]
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


def _abbreviates(name, option, min_length):
    """Whether `name` is `option` or a prefix of it at least `min_length`
    characters long: a long option written the way a parser that expands
    abbreviations would still read as `option` (`--ex` for `--exec`).

    The minimum is each caller's: git, docker and GNU make accept a prefix of
    two letters after the dashes, so their callers pass 4 with the dashes
    counted; npm's caller compares names without dashes or underscores and passes 6. A prefix
    is matched whether or not another option shares it: refusing an ambiguous
    abbreviation costs a retry, and the parser may resolve it either way."""
    return len(name) >= min_length and option.startswith(name)


def _positionals(arguments, valued):
    """The words of `arguments` that are not options, reading past each
    option in `valued` together with its separate value.

    Lenient on purpose, unlike _subcommand_at: an option it does not know is
    skipped as a flag, so that option's value is counted as a word. That is
    safe where it is used (whether a package follows `npm init`, whether
    `run` follows `uv tool`), because there an extra word can only make the
    check refuse; the words it skips are only the values of the options it is
    given."""
    words, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith("-"):
            index += 2 if argument in valued else 1
            continue
        words.append(argument)
        index += 1
    return words


def _named_option(arguments, long_name, short_name):
    """Whether `arguments` give the option `long_name` (or a prefix of it of
    at least two letters, as git and docker accept) or `short_name`, however
    spelled."""
    for argument in arguments:
        name = argument.split("=", 1)[0]
        if name == short_name:
            return True
        # A short option inside a cluster (`-qo out`), as pflag reads one.
        if len(short_name) == 2 and argument.startswith("-") and not argument.startswith("--") \
                and short_name[1] in argument[1:].split("=", 1)[0]:
            return True
        if name.startswith("--") and _abbreviates(name, long_name, 4):
            return True
    return False


#: Programs a package may install that run code they are handed (`tsx -e`,
#: `ts-node -e`, `zx`), judged as code runners after `run` (R5 review).
_SCRIPT_RUNNERS = frozenset({
    "tsx", "ts-node", "ts-node-esm", "esno", "esr", "vite-node", "zx", "jiti", "nodejs",
    "babel-node", "coffee",
})


def _is_code_runner(word):
    """Whether `word` names a program that runs code it is handed.

    Any `python[0-9.]*` counts (`python3.12`), by regex, while _PYTHON is a
    fixed set: the two answer different questions. This one decides whether a
    word after `run` must be judged as a command rather than let run as a
    script, and should err wide, since judging a harmless name costs nothing.
    _PYTHON is the list of names the pipeline lets run `-m <module>`, and
    should be exactly what it means (a `python3.12` is refused there as a
    program not on the allowlist)."""
    name = word.strip("\"'").rsplit("/", 1)[-1]
    return (name in SHELLS or name in _INTERPRETERS or name in _PYTHON
            or name in _SCRIPT_RUNNERS or bool(re.fullmatch(r"python[0-9.]*", name)))
