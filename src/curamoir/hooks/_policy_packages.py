"""The package runners' checks for the job-time command policy: uv, npm, pnpm
and yarn (policy.py is the spec of record).

Each of them can run more than the repository's own code, and these checks
refuse what they would run that nobody reviewed (cm-hve, cm-hhr, cm-f41, R5
review): a package fetched and run (`pnpm dlx`, `npm create <pkg>`,
`uv tool run`, `uv run --with`); a program the allowlist would refuse on its
own, run through the runner (`uv run python -c`, `npm exec --no cowsay`,
`yarn run node -e`); and an option naming the program that runs the scripts
(`npm --script-shell`, `--node-options`).

Each check is called as `check(program, arguments, judge)`, the shape of
policy._PROGRAM_CHECKS, where `judge` is policy._nested_refusal: the program
a runner runs is judged by the whole policy, as if it were run on its own, one
level of nesting deeper.
"""

from curamoir.hooks._policy_common import (
    _SCRIPTS,
    _abbreviates,
    _is_code_runner,
    _positionals,
    _subcommand_at,
)
from curamoir.hooks.verdict import Category

#: The subcommands with which `uv`, `npm`, `pnpm` and `yarn` run a program:
#: what the allowlist would admit on its own, judged by policy._refusal_for.
#: So `uv run python -c` is refused as `python -c` is, and `uv run cowsay`
#: as `cowsay` is.
_RUN_SUBCOMMANDS = {
    "uv": frozenset({"run"}), "npm": frozenset({"exec", "x"}),
    "pnpm": frozenset({"exec"}), "yarn": frozenset({"exec"}),
}

#: Options of those runners that fetch packages into the run: `uv run --with
#: pkg pkg-cli` runs code nobody reviewed, chosen at call time. Refused by
#: name anywhere after the run subcommand, which _RUN_OPTIONS (read only up
#: to the program) does not reach: `uv run pytest --with x` too. Before the
#: program it also gives the clearer message, that the option fetches a
#: package, rather than that it is an option cúramóir cannot read past.
_FETCHING_OPTIONS = ("--with", "--with-requirements", "--with-editable")

#: Subcommands that fetch a package and run it. `npm init <pkg>` and
#: `npm create <pkg>` run create-<pkg>; `uv tool run` is `uvx`, which the
#: allowlist never admitted. The subcommand is the one _subcommand_at finds,
#: so an option's value before it is read past (`pnpm --dir web dlx pkg`).
#: `dlx` always fetches; `init` and `create` only when a package follows
#: them (_positionals), since bare `npm init` writes a package.json.
_FETCH_AND_RUN = {
    "pnpm": {"dlx", "create"}, "yarn": {"dlx", "create"},
    "npm": {"init", "innit", "create"},
}

#: Value-taking options a fetch check reads past after the subcommand, so
#: an option's value is not taken for a package (`npm init --scope foo`).
#: A plain list, not the `(flags, valued)` shape of the tables below: it is
#: read by _positionals, which is lenient on purpose (its docstring says why
#: that is safe here).
_VALUED_AFTER_SUBCOMMAND = {
    "npm": ("--scope", "-w", "--workspace"),
    "pnpm": ("--dir", "-C", "--filter", "-F"),
    "yarn": ("--cwd",),
}

#: Options that may come between a runner's run subcommand and the program
#: it runs, in the `(flags, valued)` shape _policy_common describes. uv run's
#: `-m` makes the program a module name, judged as a program (so
#: `uv run -m pytest` passes and `-m evil` does not). Absent on purpose:
#: --with* (fetching), --index-url and the other index options (a source of
#: packages), npm's --package/-p and --yes.
_RUN_OPTIONS = {
    "uv": (frozenset({"--frozen", "--locked", "--no-sync", "--isolated", "--no-project",
                      "--all-extras", "--no-dev", "-q", "--quiet", "-m", "--module"}),
           frozenset({"--extra", "--group", "--only-group", "--package", "--python", "-p",
                      "--directory", "--project",
                      "--env-file"})),
    "npm": (frozenset({"--no", "-ws", "--workspaces", "-s", "--silent", "-q", "--quiet"}),
            frozenset({"-w", "--workspace"})),
    "pnpm": (frozenset({"-r", "--recursive", "--parallel", "-s", "--silent"}),
             frozenset({"--filter", "-F", "--dir", "-C"})),
    "yarn": (frozenset(), frozenset()),
}

#: The subcommands that run a package.json script by name (npm's aliases
#: included).
_SCRIPT_SUBCOMMANDS = frozenset({"run", "run-script", "rum", "urn"})

#: Options that may come between `run` and the script's name, in the
#: `(flags, valued)` shape _policy_common describes.
_SCRIPT_OPTIONS = {
    "npm": (frozenset({"--if-present", "--silent", "-s", "-ws", "--workspaces"}),
            frozenset({"-w", "--workspace"})),
    "pnpm": (frozenset({"-r", "--recursive", "--if-present", "--parallel", "-s",
                        "--silent"}),
             frozenset({"--filter", "-F", "--dir", "-C"})),
    "yarn": (frozenset({"-T", "--top-level", "-B", "--binaries-only"}), frozenset()),
}

#: yarn run's options that make it run a binary, never a script.
_BINARIES_ONLY = frozenset({"-B", "--binaries-only"})

#: Options of npm, pnpm and yarn that name the program running a script, or
#: flags for node, refused wherever they come before `--`, since npm reads an
#: option anywhere before it (R5 review). Kept in the form _option_key gives:
#: npm's config reads `-` and `_` alike and ignores case (`--node_options`,
#: `--Script-Shell`), and `scriptshell` is the same key without its dash
#: (security review of 986347c).
_PROGRAM_NAMING_OPTIONS = frozenset({"scriptshell", "nodeoptions"})


def _option_key(argument):
    """`argument`'s option name with every `-` and `_` removed, lowercased:
    the form in which two spellings npm reads as one option compare equal."""
    return argument.split("=", 1)[0].replace("-", "").replace("_", "").lower()


def _package_script(program, arguments, judge):
    """A refusal if npm, pnpm or yarn is told which program runs its scripts
    (`--script-shell`, `--node-options`), or if `run <script>` would run
    something other than the repository's own script; else None. Not run
    for uv, which has no package.json scripts."""
    at, refused = _subcommand_at(program, arguments)
    if refused is not None or at is None:
        return refused
    subcommand, after = arguments[at], arguments[at + 1:]
    for argument in after:
        if argument == "--":
            break
        name = _option_key(argument)
        # Or a prefix of one of at least three letters (`--scr`, `--nod`),
        # in case npm's option parser expands abbreviations as it long has.
        # Three, compared without dashes or underscores: a two-letter prefix
        # (`--sc`, `--no`) is shared with options a job uses (`--no` is
        # npm exec's), and erring toward refusing costs a retry (round 6).
        if argument.startswith("-") and any(
                _abbreviates(name, option, 3) for option in _PROGRAM_NAMING_OPTIONS):
            return (
                Category.UNVERIFIABLE,
                f"{program} {argument.split('=', 1)[0]} names the program that runs "
                "the scripts, chosen at call time. Run it without",
            )
    if subcommand not in _SCRIPT_SUBCOMMANDS:
        return None
    # `run <script>` runs the repository's own script, which is allowed by
    # decision (cm-hve). But the word after `run` need not be a script:
    #
    # - A code runner (_is_code_runner) is judged as a command for all three
    #   (`yarn run node -e`, `npm run tsx -e`), whatever package.json holds.
    # - Any other name: npm and pnpm run only a script by it, and fail on a
    #   missing one (npm's "Missing script", pnpm's ERR_PNPM_NO_SCRIPT), so it
    #   runs. yarn falls back to an installed binary of that name, so a name
    #   that is not one of package.json's scripts (judge_command's `scripts`)
    #   is judged as `yarn exec` judges it (R5 review: `yarn run rimraf .`);
    #   when the scripts are unknown, every name is.
    # - With yarn's --cwd the scripts read are the hook's directory's, not
    #   --cwd's, so they are treated as unknown; with -B/--binaries-only yarn
    #   runs the binary whatever the scripts are, so likewise.
    #
    # Options before the name are read by their arity (_SCRIPT_OPTIONS) and
    # any other refused, so a value cannot hide the name
    # (`yarn run --require ./x.js node`).
    start, refused = _subcommand_at(program, after, _SCRIPT_OPTIONS)
    if refused is not None:
        return refused
    if start is None:
        return None
    if _is_code_runner(after[start]):
        return judge(after[start:])
    scripts = _SCRIPTS.get()
    if program == "yarn" and "--cwd" in {a.split("=", 1)[0] for a in arguments}:
        scripts = None
    if program == "yarn" and _BINARIES_ONLY.intersection(after[:start]):
        # Security review of 986347c: `yarn run -B build` with a `build` script.
        scripts = None
    if program == "yarn" and (scripts is None or after[start] not in scripts):
        return judge(after[start:])
    return None


def _runner_code(program, arguments, judge):
    """A refusal if uv, npm, pnpm or yarn fetches a package and runs it
    (_FETCH_AND_RUN, `uv tool run`, `uv run --with`), or runs a program
    (_RUN_SUBCOMMANDS: `uv run`, `npm exec`, `pnpm exec`, `yarn exec`) that
    the allowlist would refuse on its own; else None."""
    at, refused = _subcommand_at(program, arguments)
    if refused is not None or at is None:
        return refused
    subcommand, after = arguments[at], arguments[at + 1:]
    following = _positionals(after, _VALUED_AFTER_SUBCOMMAND.get(program, ()))
    if subcommand in _FETCH_AND_RUN.get(program, ()) and (subcommand == "dlx" or following):
        return (
            Category.NOT_ALLOWLISTED,
            f"{program} {subcommand} fetches a package and runs it, code nobody "
            "reviewed and chosen at call time",
        )
    # Anywhere among the words: an option's value can come before `run`
    # (`uv tool --cache-dir /tmp/c run pkg`).
    if program == "uv" and subcommand == "tool" and "run" in following:
        return (
            Category.NOT_ALLOWLISTED,
            "uv tool run fetches a package and runs it, code nobody reviewed "
            "and chosen at call time",
        )
    if subcommand not in _RUN_SUBCOMMANDS.get(program, ()):
        return None
    if program == "npm":
        return _npm_exec(subcommand, after, judge)
    for argument in after:
        if argument.split("=", 1)[0] in _FETCHING_OPTIONS:
            return (
                Category.NOT_ALLOWLISTED,
                f"{program} {subcommand} {argument.split('=', 1)[0]} fetches a package "
                "into the run, code nobody reviewed and chosen at call time",
            )
    start, refused = _subcommand_at(program, after, _RUN_OPTIONS)
    if refused is not None:
        return refused
    if start is None:
        return None
    # The program run must pass the allowlist on its own.
    return judge(after[start:])


def _npm_exec(subcommand, after, judge):
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
        return (
            Category.NOT_ALLOWLISTED,
            f"npm {subcommand} without --no (and without --yes) before the program fetches "
            "and runs a package that is not installed, code nobody reviewed",
            f"npm {subcommand} --no <program>, which runs only what is installed",
        )
    if not words:
        return None
    # The program run must pass the allowlist on its own.
    return judge(words)
