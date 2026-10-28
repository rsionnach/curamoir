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

from curamoir.hooks._policy_common import _abbreviates, _subcommand_at
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
