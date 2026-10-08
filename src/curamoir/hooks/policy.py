"""DEC-9: the command policy for a cúramóir job — allowlist first, denylist second.

Three denylist rewrites each closed holes and opened others, because a denylist
must understand every way a command can be written while an allowlist only has
to recognise the few things cúramóir's own jobs legitimately do.

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
import shlex

from curamoir.hooks.categories import Category
from curamoir.hooks.denylist import Decision, _segments, decide

# Construction whose effect this guard cannot determine by reading it. Refused
# rather than parsed — the distinction matters, and the message says which.
_UNVERIFIABLE = (
    (re.compile(r"\$\("), "command substitution `$(...)`"),
    (re.compile(r"`"), "command substitution with backticks"),
    (re.compile(r"<\(|>\("), "process substitution `<(...)`"),
    (re.compile(r"\$\{?[A-Za-z_]"), "a shell variable, whose value is not knowable here"),
)

# `NAME=value` before the program is a shell ASSIGNMENT PREFIX, not a command.
# Refusing it meant this repo's own test command — PLANTED.json's
# `PYTHONPATH=src python3 -m pytest -q`, which E3-R4 requires the sweep to run —
# was blocked. A variable REFERENCE (`$R`) is still unverifiable; an assignment
# is not.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Programs that can run anything, so allowlisting them would allowlist everything.
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"}
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

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(command):
    """Remove heredoc bodies, which are data rather than commands.

    The denylist judged each body line as its own command, so writing this
    project's `docs/security.md` — which names `rm -rf` and `git reset --hard` —
    was refused. A guard that blocks writing its own documentation is one the
    developer switches off.
    """
    lines = command.splitlines()
    kept, index = [], 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        match = _HEREDOC.search(line)
        index += 1
        if match:
            terminator = match.group(2)
            while index < len(lines) and lines[index].strip() != terminator:
                index += 1
            index += 1  # skip the terminator itself
    return "\n".join(kept)


def _unverifiable(segment):
    for pattern, description in _UNVERIFIABLE:
        if pattern.search(segment):
            return description
    return None


def _allowed(tokens):
    """None if allowed, else (Category, reason).

    The category is returned, never inferred from the message. Deriving it from a
    substring of the prose would be the same mistake the provenance review found
    in the tests: a check that reads text instead of structure.

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

    if program in _SHELLS:
        # A committed SCRIPT may run; inline code may not.
        #
        # The line is reviewed-artefact versus generated-at-call-time. cúramóir
        # already allows `pytest`, `make` and `npm`, every one of which executes
        # arbitrary repo-controlled code — so refusing `bash scripts/quickstart.sh`
        # (the default `smoke.command`) while allowing `pytest` was incoherent.
        # Inline `-c` is genuinely different: it is composed at call time and is
        # not an artefact anyone reviewed.
        flags = [a for a in arguments if a.startswith("-")]
        if any("c" in flag.lstrip("-") for flag in flags):
            return (
                Category.UNVERIFIABLE,
                f"{program!r} with -c runs code composed at call time, which the "
                "guard cannot verify. Put the script in a file and run that instead",
            )
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

    stripped = _strip_heredocs(command or "")

    for segment in _segments(stripped):
        if not segment.strip():
            continue

        reason = _unverifiable(segment)
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

        try:
            tokens = shlex.split(segment)
        except ValueError:
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

        verdict = _allowed(tokens)
        if verdict is not None:
            category, reason = verdict
            return Decision(
                deny=True,
                reason=(
                    f"blocked by cúramóir: {reason}. A job may run only recognised "
                    "commands, because a guard that is behind on an allowlist "
                    "costs a retry while one behind on a denylist costs the "
                    "repository. If this command is legitimate, add it to the "
                    "allowlist in curamoir.yaml and say why in the bead."
                ),
                alternative="add it to the allowlist in curamoir.yaml",
                category=category,
            )

    # Second pass: an allowlisted program can still be used destructively.
    return decide(stripped, in_job=True)
