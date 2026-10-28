"""git's checks for the job-time command policy (policy.py is the spec of record).

git is on the allowlist with its subcommands (ALLOWED in policy.py), and these
checks run before the subcommand is checked. They refuse a command git would
run that was written at call time: in configuration for this call or written
by `git config` (cm-hve, cm-z0k, cm-f41), and in a subcommand's options
(`rebase --exec`, `--upload-pack`, `submodule foreach`; R5 review).

Each check is called as `check(program, arguments, judge)`, the shape of
policy._PROGRAM_CHECKS; git's never need the recursive `judge`.
"""

import re

from curamoir.hooks._policy_common import (
    _abbreviates,
    _read_leading,
    _subcommand_at,
    _write_target_refusal,
)
from curamoir.hooks._policy_writes import _path_candidates
from curamoir.hooks.verdict import Category

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

#: git options that run a command written at call time, by subcommand (R5
#: review, 2026-10-28). Long options are matched with git's own rule that
#: an unambiguous prefix is accepted (`--ex` is `--exec` in rebase); `-x`
#: (rebase) in a short-option cluster too.
_GIT_COMMAND_OPTIONS = {
    "rebase": ("--exec",),
    "fetch": ("--upload-pack",),
    "pull": ("--upload-pack",),
    "ls-remote": ("--upload-pack", "--exec"),
    "push": ("--receive-pack", "--exec"),
}


def _git_command_setting(program, arguments, judge):
    """A refusal if git is given configuration for this call (`-c`,
    `--config-env`) or its helpers' directory (`--exec-path`), or if
    `git config` writes a key not on _GIT_WRITABLE_KEY, renames, removes or
    edits sections, or is given an option not on _GIT_CONFIG_OPTIONS; else
    None. An option before the subcommand that _LEADING_OPTIONS does not list
    is _git_inline_command's to refuse, which runs after this."""
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
    at, refused = _subcommand_at("git", arguments)
    if refused is not None or at is None or arguments[at] != "config":
        return None
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
        # The new-style subcommands (git 2.46+) read options after their
        # positionals too: `git config set user.name x --file=/tmp/f`.
        options |= {a.split("=", 1)[0] for a in rest[1:] if a.startswith("-")}
        rest = [a for a in rest[1:] if not a.startswith("-")]
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


def _git_inline_command(program, arguments, judge):
    """A refusal if git's subcommand is told to run a command written at
    call time (`rebase -x`, `submodule foreach`, `--upload-pack`,
    `--receive-pack`), else None. The subcommand is found by _subcommand_at,
    as every check that depends on it finds it."""
    at, refused = _subcommand_at("git", arguments)
    if refused is not None or at is None:
        return refused
    subcommand, after = arguments[at], arguments[at + 1:]
    if subcommand == "submodule" and "foreach" in after:
        return (
            Category.UNVERIFIABLE,
            "git submodule foreach runs the command that follows in every submodule, "
            "written at call time. Run the command in each submodule directly",
        )
    for argument in after:
        if argument == "--":
            break
        name = argument.split("=", 1)[0]
        dangerous = [option for option in _GIT_COMMAND_OPTIONS.get(subcommand, ())
                     if name.startswith("--") and _abbreviates(name, option, 4)]
        if not dangerous and subcommand == "rebase" and argument.startswith("-") \
                and not argument.startswith("--") and "x" in argument[1:]:
            dangerous = ["-x"]
        if dangerous:
            return (
                Category.UNVERIFIABLE,
                f"git {subcommand} {dangerous[0]} runs a command written at call time. "
                f"Run git {subcommand} without it, and the command on its own",
            )
    return None


#: git's options before the subcommand that name the directory it works in or
#: on. Judged as write targets (round 6): git writes there (`git -C /tmp/x
#: commit`, `--git-dir` into another repository's `.git`), and a directory
#: outside the diff or inside `.git` is one the sweep's review never sees.
#: `--namespace` names refs, not a directory.
_GIT_DIRECTORY_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree"})


def _git_writes(program, arguments, judge):
    """A refusal if git would write outside the diff the sweep reviews
    (round 6): a directory given to `-C`, `--git-dir` or `--work-tree` that
    lies outside it, a file named by `--output` (any subcommand, `git diff
    --output=/tmp/x`, and any long option beginning `--out`), a repository
    initialised elsewhere or with its git directory elsewhere
    (`git init /tmp/x`, `--separate-git-dir`, a `--template` outside, whose
    hooks are copied in), or a second work tree (`git worktree add`, which a
    job does not need and whose files are not in the diff); else None."""
    # git's leading options, read with the arity that finds the subcommand
    # (_read_leading), so a valued option before them cannot shift which
    # word is read as a directory (round 7, S1). Every directory one is
    # judged, a repeated -C included.
    at, refused, leading = _read_leading("git", arguments)
    elsewhere = False
    for name, value in leading:
        if name in _GIT_DIRECTORY_OPTIONS:
            elsewhere = True
            refused_directory = _write_target_refusal(f"git {name}", value)
            if refused_directory is not None:
                return refused_directory
    if refused is not None or at is None:
        return None
    subcommand, after = arguments[at], arguments[at + 1:]
    if subcommand == "worktree" and [a for a in after if not a.startswith("-")][:1] == ["add"]:
        return (
            Category.NOT_ALLOWLISTED,
            "git worktree add creates a second work tree, whose files are outside "
            "the diff the sweep reviews",
            "work in this repository's own working tree",
        )
    for index, argument in enumerate(after):
        if argument == "--":
            break
        name, equals, value = argument.partition("=")
        if subcommand == "init" and name.startswith("--") and _abbreviates(
                name, "--separate-git-dir", 5):
            return (
                Category.NOT_ALLOWLISTED,
                "git init --separate-git-dir puts the repository's git directory "
                "elsewhere, outside the diff the sweep reviews",
                "git init without it",
            )
        if name.startswith("--out"):
            if elsewhere:
                return _relative_elsewhere(f"git {subcommand} {name}")
            if not equals:
                value = after[index + 1] if index + 1 < len(after) else ""
            refused = _write_target_refusal(f"git {subcommand} {name}", value)
            if refused is not None:
                return refused
    if subcommand == "init":
        if elsewhere:
            return _relative_elsewhere("git init")
        for argument in after:
            for path in _path_candidates(argument):
                refused = _write_target_refusal("git init", path)
                if refused is not None:
                    return refused
    return None


def _relative_elsewhere(what):
    """The refusal for a path `what` writes while -C, --git-dir or
    --work-tree names another directory (round 7, S2).

    Refused rather than resolved: git resolves such a path against a
    directory that -C options compose (`-C a -C b` is `a/b`) and that
    --work-tree and --git-dir each move differently, per subcommand, and a
    second implementation of that resolution is one more thing to get
    wrong. Running git from the directory instead costs a retry."""
    return (
        Category.UNVERIFIABLE,
        f"{what} names a path relative to the directory -C, --git-dir or "
        "--work-tree gives, which the guard does not resolve",
        "run git without the directory option, naming the path from here",
    )
