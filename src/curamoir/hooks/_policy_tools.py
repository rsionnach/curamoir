"""The build tools' checks for the job-time command policy: docker, podman,
go, make and cargo (policy.py is the spec of record).

These are on the allowlist with any arguments, because building and testing
the repository is what a job does with them. The checks refuse what they would
run that was not in the repository: an image's code (`docker run`), a makefile
or variables written at call time (`make --eval`, `make SHELL=...`), a module
fetched at a version (`go run pkg@latest`), a setting a later call obeys
(`go env -w`), a program named in an option (`go -toolexec`,
`go -ldflags=-extld=...`, `cargo --config`), a build's output written anywhere (`docker build
--output`), and an image changed or moved (`docker image rm`, `push`)
(cm-hve, cm-z0k, R5 review).

Each check is called as `check(program, arguments, judge)`, the shape of
policy._PROGRAM_CHECKS; none of these needs the recursive `judge`.
"""

from curamoir.hooks._policy_common import (
    _ASSIGNMENT,
    _abbreviates,
    _named_option,
    _subcommand_at,
)
from curamoir.hooks.verdict import Category

#: docker and podman subcommands a job may run: building from the
#: repository's Dockerfile and reading state. `run`, `exec` and the rest run
#: an image's code, fetched at call time, which is fetch-and-run.
_CONTAINER_SUBCOMMANDS = frozenset({
    "build", "ps", "images", "image", "inspect", "logs", "version", "info", "compose",
})

#: `docker image` subcommands a job may run: reading and building. rm,
#: prune, push, pull, import and tag change or move images (R5 review).
_IMAGE_SUBCOMMANDS = frozenset({"ls", "list", "inspect", "history", "build"})

#: `docker compose` subcommands a job may run, by the container rule:
#: `run`, `exec` and `up` start an image's code.
_COMPOSE_SUBCOMMANDS = frozenset({
    "build", "ps", "config", "logs", "images", "ls", "version", "top", "port",
})

#: Options whose value is a program to run, per program, matched however the
#: option is spelled (`-toolexec=x`, `--toolexec x`): go's flag package takes
#: one dash or two, and cargo's `--config` can set a target's runner.
_PROGRAM_OPTIONS = {
    "go": ("toolexec", "exec", "vettool"),
    "cargo": ("config",),
}


def _container_code(program, arguments, judge):
    """A refusal if docker or podman would run an image's code (a subcommand
    not on _CONTAINER_SUBCOMMANDS, a compose subcommand not on
    _COMPOSE_SUBCOMMANDS), change or move images (`image rm`), or write a
    build's files outside the repository (`build --output`); else None. The
    subcommand is found by _subcommand_at (cm-f41), and compose's by the
    same rule with its own leading options."""
    at, refused = _subcommand_at(program, arguments)
    if refused is not None or at is None:
        return refused
    subcommand, after = arguments[at], arguments[at + 1:]
    if subcommand not in _CONTAINER_SUBCOMMANDS:
        return (
            Category.NOT_ALLOWLISTED,
            f"{program} {subcommand!r} runs an image's code, fetched at call time; "
            f"a job may use {program} only to "
            + ", ".join(sorted(_CONTAINER_SUBCOMMANDS)),
        )
    if subcommand == "image":
        words = [a for a in after if not a.startswith("-")]
        if words[:1] and words[0] not in _IMAGE_SUBCOMMANDS:
            return (
                Category.NOT_ALLOWLISTED,
                f"{program} image {words[0]!r} changes or moves images; a job may use "
                f"{program} image only to " + ", ".join(sorted(_IMAGE_SUBCOMMANDS)),
            )
    if subcommand in ("build", "image") and _named_option(after, "--output", "-o"):
        return (
            Category.NOT_ALLOWLISTED,
            f"{program} build --output writes the build's files wherever it is told, "
            "outside the repository too; build an image instead",
        )
    if subcommand == "compose":
        inner, refused = _subcommand_at("compose", after)
        if refused is not None:
            return refused
        if inner is not None and after[inner] not in _COMPOSE_SUBCOMMANDS:
            return (
                Category.NOT_ALLOWLISTED,
                f"{program} compose {after[inner]!r} runs a service's image; a job may "
                "use compose only to " + ", ".join(sorted(_COMPOSE_SUBCOMMANDS)),
            )
    return None


def _make_code(program, arguments, judge):
    """A refusal if make is given a makefile or variables at call time:
    `--eval` (and any prefix of it GNU make accepts, `--ev`), `-E` alone or
    in a cluster (`-sE`), or a `NAME=value` on its command line, which
    overrides the makefile (SHELL, CC and the like are commands)."""
    for argument in arguments:
        if argument.startswith("--"):
            name = argument.split("=", 1)[0]
            if _abbreviates(name, "--eval", 4):
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


def _program_option(program, arguments, judge):
    """A refusal if an option whose value is a program (_PROGRAM_OPTIONS) is
    given, however it is spelled, else None. cargo's whole check, and the
    last of go's."""
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


#: The linker flags `go -ldflags` may pass (security review of 986347c):
#: stripping (`-s`, `-w`), stamping (`-X importpath.name=value`, `-buildid`)
#: and the link mode. An allowlist, not a list of the dangerous ones, because
#: the linker's other flags include `-extld` (the external linker to run) and
#: `-extldflags`, whose value goes to that linker, where `-fuse-ld=` and `-B`
#: name programs in turn: listing those is the denylist problem one level down.
_GO_LINK_FLAGS = frozenset({"s", "w", "X", "buildid", "linkmode", "compressdwarf"})

#: go's options whose value is flags for a tool it runs. -ldflags is read
#: against _GO_LINK_FLAGS; -gccgoflags is refused outright, since gccgo is gcc
#: and gcc's `-B` names the directory its programs are run from.
_GO_TOOL_FLAG_OPTIONS = ("ldflags", "gccgoflags")


def _go_tool_flags(arguments):
    """A refusal if go is given -gccgoflags, or -ldflags with a linker flag
    not on _GO_LINK_FLAGS, in any spelling (one dash or two, `=` or a
    separate value, with or without a `pattern=` prefix); else None."""
    for index, argument in enumerate(arguments):
        if not argument.startswith("-"):
            continue
        name, equals, value = argument.lstrip("-").partition("=")
        if name not in _GO_TOOL_FLAG_OPTIONS:
            continue
        if name == "gccgoflags":
            return (
                Category.UNVERIFIABLE,
                "go -gccgoflags passes flags to gcc, which can name the programs gcc "
                "runs, chosen at call time. Build without it",
            )
        if not equals:
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        # `-ldflags=all=-s`: a package pattern, then the flags.
        pattern, has_pattern, flags = value.partition("=")
        if has_pattern and not pattern.startswith("-"):
            value = flags
        words = value.split()
        position = 0
        while position < len(words):
            word = words[position]
            position += 1
            flag, attached, _ = word.lstrip("-").partition("=")
            if word.startswith("-") and flag in _GO_LINK_FLAGS:
                if flag == "X" and not attached:
                    position += 1   # `-X importpath.name=value`
                continue
            return (
                Category.UNVERIFIABLE,
                f"go -ldflags {word!r} is not a linker flag a job may pass (only "
                + ", ".join(f"-{f}" for f in sorted(_GO_LINK_FLAGS))
                + "): others, such as -extld, name a program for the linker to run. "
                "Build without it",
            )
    return None


def _go_code(program, arguments, judge):
    """A refusal for `go env -w`/`-u` (a persistent setting, GOFLAGS among
    them, that a later go command obeys) or `go run`/`go install` of a
    module at a version (`pkg@latest`: fetched and run), else
    _go_tool_flags' verdict, else _program_option's."""
    at, refused = _subcommand_at("go", arguments)
    if refused is not None:
        return refused
    subcommand = arguments[at] if at is not None else None
    after = arguments[at + 1:] if at is not None else []
    words = [a for a in after if not a.startswith("-")]
    # go's flag package takes one dash or two (`--w` is `-w`), so the name
    # is compared without its dashes.
    flags = {a.lstrip("-").split("=", 1)[0] for a in after if a.startswith("-")}
    if subcommand == "env" and flags & {"w", "u"}:
        return (
            Category.NOT_ALLOWLISTED,
            "go env -w/-u changes go's settings for every later go command (GOFLAGS "
            "can name a program to run). Read them with `go env` instead",
        )
    if subcommand in ("run", "install") and any("@" in word for word in words):
        return (
            Category.NOT_ALLOWLISTED,
            f"go {subcommand} of a module at a version fetches it and runs or installs "
            f"its code, chosen at call time. go {subcommand} a package in this "
            "repository instead",
        )
    return _go_tool_flags(arguments) or _program_option("go", arguments, judge)


#: Long options that run a program, name an interpreter, plugin or module to
#: load, or set configuration at call time, by program, each with the
#: shortest prefix that program's parser would still read as it (rounds 8
#: and 9). Refused outright, whatever the value, as go's -toolexec is.
#: Installed programs were read from their `--help` and man pages; the rest
#: from their documentation (tests/hooks/test_policy_round9.py lists the
#: versions and the reasons). A parser that accepts no abbreviation (rg, uv,
#: eslint, prettier, podman) has its options matched whole; argparse and GNU
#: getopt (tox, nox, mypy, pip, sort) from the shortest unambiguous prefix.
_PROGRAM_RUNNING_OPTIONS = {
    "rg": (("--pre", 5), ("--hostname-bin", 5)),
    "sort": (("--compress-program", 4),),
    "uv": (("--env-file", 10),),
    "tox": (("--override", 3), ("--discover", 4), ("--runner", 4)),
    "nox": (("--force-python", 4), ("--force-venv-backend", 4), ("--extra-python", 4),
            ("--default-venv-backend", 4)),
    "mypy": (("--python-executable", 10),),
    "eslint": (("--plugin", 8), ("--rulesdir", 10), ("--parser", 8),
               ("--resolve-plugins-relative-to", 29)),
    "prettier": (("--plugin", 8), ("--plugin-search-dir", 19)),
    "pip": (("--python", 8), ("--config-settings", 6), ("--global-option", 4),
            ("--build-option", 5), ("--install-option", 5)),
    "podman": (("--runtime", 9), ("--runtime-flag", 14), ("--conmon", 8),
               ("--hooks-dir", 11), ("--network-cmd-path", 18), ("--module", 8)),
}

#: The short spellings of those, by program: a letter is found anywhere in a
#: cluster (`tox -rx k=v`), erring toward refusing; nox's two-letter ones
#: (`-db`, `-fb`) are matched as words.
_PROGRAM_RUNNING_SHORTS = {
    "tox": frozenset("x"),
    "nox": frozenset({"P", "-db", "-fb"}),
    "pip": frozenset("C"),
    "cargo": frozenset("Z"),
}

#: eslint's built-in formatters (8.x and 9.x). Any other `-f`/`--format` is a
#: module path or package eslint loads.
_ESLINT_FORMATTERS = frozenset({
    "stylish", "json", "json-with-metadata", "html", "checkstyle", "compact",
    "jslint-xml", "junit", "tap", "unix", "visualstudio",
})

#: bd subcommands that install or configure code that runs later: git hooks
#: in `.git/hooks`, and editor integrations outside the repository.
_BD_REFUSED = frozenset({"hooks", "setup"})

#: pytest's short options that take a value, which ends a short cluster.
_PYTEST_VALUED_SHORT = frozenset("kmcoWrnp")

#: ini keys `pytest -o` may set: output, collection and warning settings. Any
#: other is refused (round 9 addendum): `addopts` is any option, `-p` among
#: them, and `required_plugins` and the rest steer what loads. An allowlist,
#: since the list of keys that can load code is the denylist problem.
_PYTEST_SAFE_INI = frozenset({
    "cache_dir", "testpaths", "python_files", "python_classes", "python_functions",
    "filterwarnings", "log_cli", "log_cli_level", "log_level", "log_format",
    "log_date_format", "junit_family", "junit_suite_name", "junit_logging",
    "junit_duration_report", "markers", "xfail_strict", "minversion", "norecursedirs",
    "console_output_style", "tmp_path_retention_count", "tmp_path_retention_policy",
    "empty_parameter_set_mark", "faulthandler_timeout", "verbosity_assertions",
    "verbosity_test_cases", "truncation_limit_lines", "truncation_limit_chars",
})


def _option_refusal(program, option):
    return (
        Category.UNVERIFIABLE,
        f"{program} {option} names a program, interpreter or module for {program} to "
        "run or load, or sets its configuration, chosen at call time. Run "
        f"{program} without it",
    )


def _runs_a_program(program, arguments):
    """A refusal if `arguments` give `program` an option that runs a program,
    loads code or sets configuration at call time (_PROGRAM_RUNNING_OPTIONS,
    _PROGRAM_RUNNING_SHORTS, cargo's `+toolchain`, eslint's non-built-in
    formatters, pytest's `-p`, `-c` and `-o`), or a bd subcommand on
    _BD_REFUSED; else None."""
    if program == "bd":
        return _bd_subcommand(arguments)
    if program == "pytest":
        return _pytest_options(arguments)
    if program == "cargo" and arguments[:1] and arguments[0].startswith("+"):
        return _option_refusal("cargo", arguments[0])
    options = _PROGRAM_RUNNING_OPTIONS.get(program, ())
    shorts = _PROGRAM_RUNNING_SHORTS.get(program, frozenset())
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        name = argument.split("=", 1)[0]
        if name.startswith("--"):
            for option, shortest in options:
                if _abbreviates(name, option, shortest):
                    return _option_refusal(program, option)
            if program == "eslint" and _abbreviates(name, "--format", 8):
                value = argument.partition("=")[2] or (
                    arguments[index + 1] if index + 1 < len(arguments) else "")
                if value not in _ESLINT_FORMATTERS:
                    return _option_refusal(program, f"--format {value}")
            continue
        if not argument.startswith("-") or argument == "-":
            continue
        if name in shorts:
            return _option_refusal(program, name)
        letters = set(name[1:]) & {short for short in shorts if len(short) == 1}
        if letters:
            return _option_refusal(program, f"-{sorted(letters)[0]}")
        if program == "eslint" and argument.startswith("-f"):
            value = argument[2:].lstrip("=") or (
                arguments[index + 1] if index + 1 < len(arguments) else "")
            if value not in _ESLINT_FORMATTERS:
                return _option_refusal(program, f"-f {value}")
    return None


def _bd_subcommand(arguments):
    """The refusal for a bd subcommand on _BD_REFUSED, found past bd's
    leading options by their arity (_LEADING_OPTIONS["bd"]; an unknown one is
    refused, so a value cannot hide the subcommand); else None."""
    at, refused = _subcommand_at("bd", arguments)
    if refused is not None:
        return refused
    if at is not None and arguments[at] in _BD_REFUSED:
        return (
            Category.NOT_ALLOWLISTED,
            f"bd {arguments[at]} installs hooks or integrations that run later, "
            "outside the diff the sweep reviews",
        )
    return None


def _pytest_options(arguments):
    """The refusal for pytest's `-p NAME` other than `-p no:<plugin>` (it
    imports NAME), `-c`/`--config-file` (another configuration file), and
    `-o`/`--override-ini KEY=VALUE` with a KEY not on _PYTEST_SAFE_INI, in any
    spelling (`-pNAME`, `-p=NAME`, in a cluster, `--override-ini=...`, an
    abbreviation); else None."""
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        following = arguments[index + 1] if index + 1 < len(arguments) else ""
        if argument.startswith("--"):
            name, equals, value = argument.partition("=")
            value = value if equals else following
            if _abbreviates(name, "--config-file", 4):
                return _option_refusal("pytest", "--config-file")
            if _abbreviates(name, "--override-ini", 5):
                refused = _pytest_ini(value)
                if refused is not None:
                    return refused
            continue
        if not argument.startswith("-") or argument == "-":
            continue
        for position, character in enumerate(argument[1:], start=2):
            value = argument[position:].lstrip("=") or following
            if character == "c":
                return _option_refusal("pytest", "-c")
            if character == "p":
                if not value.startswith("no:"):
                    return (
                        Category.UNVERIFIABLE,
                        f"pytest -p {value} loads a plugin module chosen at call time. "
                        "Run pytest without it; -p no:<plugin> is allowed",
                    )
                break
            if character == "o":
                refused = _pytest_ini(value)
                if refused is not None:
                    return refused
                break
            if character in _PYTEST_VALUED_SHORT:
                break
    return None


def _pytest_ini(setting):
    """The refusal for `pytest -o setting` unless its key is on
    _PYTEST_SAFE_INI."""
    key = setting.split("=", 1)[0].strip()
    if key in _PYTEST_SAFE_INI:
        return None
    return (
        Category.UNVERIFIABLE,
        f"pytest -o {key}=... sets configuration that can load code (addopts can "
        "add -p, for one), chosen at call time. Allowed keys: "
        + ", ".join(sorted(_PYTEST_SAFE_INI)),
    )
