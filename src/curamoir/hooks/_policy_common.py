"""The pieces of the job-time command policy that every program family shares.

policy.py is the pipeline and the spec of record; the program families' checks
live in _policy_git.py, _policy_packages.py and _policy_tools.py, and this
module holds what more than one of them reads: how a program's subcommand is
found, how an abbreviated long option is recognised, which words name a
program that runs code, whether a path written lies outside the reviewed
diff, and the per-call facts (the package.json script names, the hook's
working directory) for the command being judged. It imports nothing from policy.py, so the families can import it
and policy.py can import them without a cycle; the one thing a family needs
from the pipeline, the recursive judge, is handed to each check as an argument.

Stdlib only, and no config read, as policy.py.
"""

import os
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
    at, refused, _ = _read_leading(program, arguments, table)
    return at, refused


def _read_leading(program, arguments, table=_LEADING_OPTIONS):
    """_subcommand_at's answer, and the `(name, value)` of every valued option
    read on the way: one walk, so a check that judges an option's value
    (git's -C) reads exactly the options, at exactly the arity, that find the
    subcommand (round 7: `--namespace x -C /tmp` had shifted them)."""
    flags, valued = table[program]
    index, pairs = 0, []
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-") or argument == "-":
            return index, None, pairs
        if argument == "--":
            following = index + 1
            return (following if following < len(arguments) else None), None, pairs
        name, equals, value = argument.partition("=")
        if name in valued:
            if not equals:
                value = arguments[index + 1] if index + 1 < len(arguments) else ""
            pairs.append((name, value))
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
        ), pairs
    return None, None, pairs


def _abbreviates(name, option, min_length):
    """Whether `name` is `option` or a prefix of it at least `min_length`
    characters long: a long option written the way a parser that expands
    abbreviations would still read as `option` (`--ex` for `--exec`).

    The minimum is each caller's: git, docker and GNU make accept a prefix of
    two letters after the dashes, so their callers pass 4 with the dashes
    counted; npm's caller compares names without dashes or underscores and passes 3. A prefix
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


#: The directory the command runs in (judge_command's `cwd`, which pretooluse
#: supplies from the hook payload), or None when it is not known. Read only by
#: _write_target_refusal, to resolve a write's real target; a ContextVar for
#: the reason _SCRIPTS is one.
_CWD = ContextVar("curamoir_policy_cwd", default=None)

#: Whether the command being judged holds an unquoted glob character anywhere
#: (_has_unquoted_glob). True by default, so a caller that did not ask is
#: judged strictly.
_UNQUOTED_GLOB = ContextVar("curamoir_policy_unquoted_glob", default=True)

#: Whether an earlier segment of the command being judged may change the
#: working tree (a git apply, checkout, pull, ...; policy._TREE_CHANGING), set
#: by policy._judge as it walks the segments. A write after one is refused:
#: the tree the guard looked at is not the tree the write meets (round 7).
_TREE_MAY_HAVE_CHANGED = ContextVar("curamoir_policy_tree_changed", default=False)

#: Characters that make a word a glob the shell expands before the program
#: sees it.
_GLOB_CHARACTERS = frozenset("*?[")


def _has_unquoted_glob(command):
    """Whether `command` holds `*`, `?` or `[` outside quotes and unescaped.

    Per command rather than per word, because `lex` has already removed the
    quotes by the time a word is judged: when this is False no word was
    globbed, and when it is True every write target holding one of these is
    read as a glob (round 6), which errs strict."""
    in_single = in_double = escaped = False
    for character in command:
        if escaped:
            escaped = False
        elif character == "\\" and not in_single:
            escaped = True
        elif character == "'" and not in_double:
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        elif character in _GLOB_CHARACTERS and not in_single and not in_double:
            return True
    return False


def _write_target_refusal(what, path):
    """The refusal for `what` writing `path` outside the diff the sweep
    reviews, else None (I1, R5 edge-case review; round 6).

    - A glob (`*`, `?`, `[` in a command that globs) is refused as
      unverifiable: which files it names is the shell's answer, and emulating
      bash's matching here (`[!x]` against `[^x]`, dotfiles, options) is a
      second implementation to get wrong.
    - Literally: `~`, a `.git` component in any case (macOS's filesystem is
      case-insensitive), and, when the working directory is unknown, an
      absolute path or a `..` component.
    - When the working directory is known: the real path of the target
      (os.path.realpath, which follows a symlink in ANY component and
      collapses `..`) must lie under the working directory's real path, with
      no component of it, relative to that, named `.git` in any case. That
      covers a symlink to `.git` or outside, wherever in the path it sits.
    - After a segment that may change the tree (_TREE_MAY_HAVE_CHANGED), any
      write target at all: the guard looked at the tree before it changed.

    Best effort, and said so: the real path is read when the command is
    judged, and a link can appear between that and the write (another
    process, or a step of the command the guard does not model). The boundary
    for that is not this check but cm-xq9 (no job agent gets a shell without
    an OS sandbox) and cm-4w5."""
    if not path:
        return None
    if _TREE_MAY_HAVE_CHANGED.get():
        return (
            Category.UNVERIFIABLE,
            f"{what} writes to {path!r} after an earlier command that may change the "
            "working tree, so the tree may change before this write; run them "
            "separately",
            "run the git command first, then the write as its own command",
        )
    if _GLOB_CHARACTERS.intersection(path) and _UNQUOTED_GLOB.get():
        return (
            Category.UNVERIFIABLE,
            f"{what} writes to {path!r}, a glob: a write target with a glob cannot "
            "be checked against the diff the sweep reviews, since which files it "
            "names is the shell's answer",
            "name the file",
        )
    cwd = _CWD.get()
    components = path.split("/")
    outside = path.startswith("~") or any(c.lower() == ".git" for c in components)
    if cwd is None:
        outside = outside or path.startswith("/") or ".." in components
    elif not outside:
        try:
            root = os.path.realpath(cwd)
            real = os.path.realpath(os.path.join(cwd, path))
        except (OSError, ValueError):
            outside = True
        else:
            # Relative to the root, and `.git` compared without case in every
            # component: on macOS's case-insensitive filesystem a symlink to
            # `.GIT` reaches `.git` while realpath keeps the case it was
            # given, and a nested repository's `.git` is as unreviewed.
            relative = os.path.relpath(real, root)
            outside = (relative == ".." or relative.startswith(".." + os.sep)
                       or any(c.lower() == ".git" for c in relative.split(os.sep)))
    if not outside:
        return None
    return (
        Category.NOT_ALLOWLISTED,
        f"{what} writes to {path!r}, outside the diff the sweep reviews: .git/, "
        "paths outside the repository and paths through a symlink to either are "
        "never in it, and a file there can run code later (.git/hooks/pre-commit "
        "runs on the next git commit)",
        "write inside the repository's working tree, where the change is reviewed",
    )
