"""Writes outside the reviewed diff, by the programs the allowlist admits
(policy.py is the spec of record; R5 edge-case review I1, round 6).

The sweep reviews the diff. A file written into `.git/`, outside the
repository or through a symlink is never in it, and such a file can run code
later: `.git/hooks/pre-commit` runs on the next `git commit`. So every way an
allowlisted program writes a path it is given is read here, and the path is
judged by _policy_common._write_target_refusal:

- a redirection's target (`> .git/hooks/x`), read in policy._judge before
  the redirections are stripped;
- the paths tee, touch, mkdir and mv are given, and every path cp is given,
  cp's options read by their arity, its link options refused;
- an option or argument that names an output (`sort -o`, `uniq in OUT`),
  an in-place mode (`yq -i`), and `find -fprint` and its kin, refused
  outright, since a job can redirect find's output instead;
- for the test runners, linters and formatters, every argument and option
  value: they write reports, caches and fixed files to paths in their options
  (`--junitxml`, `-o cache_dir=`, `--output-file`, `--write`), and listing
  each tool's is the denylist problem, while a path outside the repository is
  something a job rarely gives them at all.

git's own writes (`--output`, `init --separate-git-dir`, `worktree add`) are
in _policy_git.
"""

from curamoir.hooks._policy_common import _write_target_refusal
from curamoir.hooks.verdict import Category

#: Redirection operators that write to their target. `>&` writes to a file
#: unless its target is a descriptor (`2>&1`) or `-` (close).
_WRITING_REDIRECTIONS = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})

#: Redirection targets that are not files anyone reviews or runs.
_HARMLESS_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})

#: Programs that write every path they are given (mv removes its sources too).
_WRITERS = frozenset({"tee", "touch", "mkdir", "mv"})

#: Programs whose every argument and option value is judged as a path: the
#: test runners, linters and formatters, which write reports, caches and
#: fixed files wherever their options say (module docstring).
_PATH_STRICT = frozenset({
    "pytest", "tox", "nox", "ruff", "black", "mypy", "flake8", "pyflakes", "eslint",
    "prettier", "tsc", "ast-grep", "sg",
})

#: find's actions that write a file. Refused outright: redirect find's output.
_FIND_WRITES = frozenset({"-fprint", "-fprint0", "-fprintf", "-fls"})

#: cp's options, by arity (GNU and BSD). Every path cp is given is checked,
#: so an option cp would read as taking a value must be known here, or its
#: value would be taken for the paths; any other option is refused.
_CP_SHORT_FLAGS = frozenset("abcdfiHLnPpRruvxTX")
_CP_SHORT_VALUED = frozenset("tS")
_CP_SHORT_REFUSED = frozenset("sl")
_CP_LONG_FLAGS = frozenset({
    "--archive", "--attributes-only", "--backup", "--copy-contents", "--debug",
    "--dereference", "--force", "--interactive", "--keep-directory-symlink",
    "--no-clobber", "--no-dereference", "--preserve", "--no-preserve", "--parents",
    "--recursive", "--reflink", "--remove-destination", "--sparse",
    "--strip-trailing-slashes", "--update", "--verbose", "--one-file-system",
    "--no-target-directory",
})
_CP_LONG_VALUED = frozenset({"--target-directory", "--suffix"})
#: A link is a path whose content is decided elsewhere: refused.
_CP_LONG_REFUSED = frozenset({"--symbolic-link", "--link"})


def _redirection_outside_diff(segment):
    """The refusal for a redirection in `segment` (before the redirections
    are stripped) that writes outside the diff, else None."""
    for index, token in enumerate(segment[:-1]):
        if token not in _WRITING_REDIRECTIONS:
            continue
        target = segment[index + 1]
        if token == ">&" and (target.isdigit() or target == "-"):
            continue
        if target in _HARMLESS_TARGETS:
            continue
        refused = _write_target_refusal(f"redirecting output with {token}", target)
        if refused is not None:
            return refused
    return None


def _write_refusal(program, arguments):
    """A refusal if `program` with `arguments` would write a path outside the
    diff, by any of the routes the module docstring lists; else None."""
    if program == "cp":
        paths, refused = _cp_paths(arguments)
        if refused is not None:
            return refused
    elif program in _WRITERS:
        paths = _written_paths(arguments)
    elif program in _PATH_STRICT:
        paths = [path for argument in arguments for path in _path_candidates(argument)]
    elif program == "sort":
        paths = _option_values(arguments, "o", "--output", "ktST")
    elif program == "uniq":
        paths = _uniq_positionals(arguments)[1:]
    elif program == "yq":
        paths = _yq_paths(arguments)
    elif program == "find":
        written = _FIND_WRITES.intersection(arguments)
        if written:
            return (
                Category.NOT_ALLOWLISTED,
                f"find {sorted(written)[0]} writes a file, which a job does not need",
                "redirect find's output to a file inside the repository",
            )
        return None
    else:
        return None
    for path in paths:
        refused = _write_target_refusal(program, path)
        if refused is not None:
            return refused
    return None


def _written_paths(arguments):
    """Every positional, and the value of a target-directory option (`-t DIR`,
    `-tDIR`, `--target-directory=DIR`) however it is spelled."""
    paths, options_ended = [], False
    for argument in arguments:
        if options_ended or not argument.startswith("-") or argument == "-":
            paths.append(argument)
        elif argument == "--":
            options_ended = True
        elif argument.startswith("--"):
            if argument.startswith("--t"):
                paths.append(argument.partition("=")[2])
        elif "t" in argument[1:]:
            paths.append(argument[argument.index("t", 1) + 1:])
    return [path for path in paths if path]


def _cp_paths(arguments):
    """(every path cp would read or write, refusal or None), reading cp's
    options by their arity (_CP_* tables). Sources are checked as well as
    the destination: a copy FROM `.git/` or outside the repository brings in
    content nobody reviewed under a name that looks ordinary, and with a
    target directory the destination is not the last word anyway."""
    paths, index, options_ended = [], 0, False
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if options_ended or not argument.startswith("-") or argument == "-":
            paths.append(argument)
            continue
        if argument == "--":
            options_ended = True
            continue
        if argument.startswith("--"):
            name, equals, value = argument.partition("=")
            known = _CP_LONG_FLAGS | _CP_LONG_VALUED | _CP_LONG_REFUSED
            candidates = {name} if name in known else {o for o in known if o.startswith(name)}
            if candidates & _CP_LONG_REFUSED:
                return [], _cp_refusal(name)
            if len(candidates) != 1:
                return [], _cp_unknown(name)
            (option,) = candidates
            if option in _CP_LONG_VALUED:
                if not equals:
                    value = arguments[index] if index < len(arguments) else ""
                    index += 1
                paths.append(value)
            continue
        for position, character in enumerate(argument[1:], start=2):
            if character in _CP_SHORT_REFUSED:
                return [], _cp_refusal(f"-{character}")
            if character in _CP_SHORT_VALUED:
                value = argument[position:]
                if not value:
                    value = arguments[index] if index < len(arguments) else ""
                    index += 1
                paths.append(value)
                break
            if character not in _CP_SHORT_FLAGS:
                return [], _cp_unknown(f"-{character}")
    return [path for path in paths if path], None


def _cp_refusal(option):
    return (
        Category.NOT_ALLOWLISTED,
        f"cp {option} makes a link rather than a copy, whose content is decided "
        "outside the diff the sweep reviews",
        "copy the file without -s/-l",
    )


def _cp_unknown(option):
    return (
        Category.NOT_ALLOWLISTED,
        f"cp {option} is not an option cúramóir knows how to read past, so it "
        "cannot tell which words are the paths cp writes",
    )


def _option_values(arguments, short, long_name, other_valued):
    """The values `arguments` give the option `-<short>`/`long_name`, in any
    spelling: `-oV`, `-o V`, inside a cluster (`-uo V`), `--output=V`,
    `--output V`, and any prefix of `long_name` of at least one letter after
    the dashes (getopt_long accepts a unique one; erring wide). A cluster
    stops at a short option in `other_valued`, whose value it then holds."""
    values, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            break
        if argument.startswith("--"):
            name, equals, value = argument.partition("=")
            if len(name) >= 3 and long_name.startswith(name):
                if not equals:
                    value = arguments[index] if index < len(arguments) else ""
                values.append(value)
            continue
        if not argument.startswith("-") or argument == "-":
            continue
        for position, character in enumerate(argument[1:], start=2):
            if character == short:
                value = argument[position:]
                if not value:
                    value = arguments[index] if index < len(arguments) else ""
                values.append(value)
                break
            if character in other_valued:
                break
    return values


def _uniq_positionals(arguments):
    """uniq's positionals (INPUT, then OUTPUT), reading past the values of
    -f, -s and -w and their long forms. Every positional after the first is
    treated as an output, so a value misread as a positional errs strict."""
    words, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            words.extend(arguments[index:])
            break
        if not argument.startswith("-") or argument == "-":
            words.append(argument)
        elif argument.startswith("--"):
            if "=" not in argument and any(
                    o.startswith(argument) for o in ("--skip-fields", "--skip-chars",
                                                     "--check-chars")):
                index += 1
        elif argument[-1] in "fsw":
            index += 1
    return words


def _yq_paths(arguments):
    """The files `yq` would edit: every positional when it is told to edit in
    place (`-i`, `--inplace`, in a cluster or with `=`), else none."""
    in_place = any(
        a.split("=", 1)[0] in ("-i", "--inplace")
        or (a.startswith("-") and not a.startswith("--") and "i" in a[1:].split("=", 1)[0])
        for a in arguments)
    if not in_place:
        return []
    return [a for a in arguments if not a.startswith("-")]


def _path_candidates(argument):
    """The words in `argument` that may be a path: a positional itself, an
    option's `=` value or attached short value, a `key=value`'s value, and the
    part after the last `:` of each (`html:/tmp/x`, `cache_dir=/tmp/c`)."""
    if argument.startswith("--"):
        values = [argument.partition("=")[2]] if "=" in argument else []
    elif argument.startswith("-") and argument != "-":
        values = [argument[2:]] if len(argument) > 2 else []
    else:
        values = [argument]
        if "=" in argument:
            values.append(argument.partition("=")[2])
    values += [value.rsplit(":", 1)[1] for value in values if ":" in value]
    return [value for value in values if value]
