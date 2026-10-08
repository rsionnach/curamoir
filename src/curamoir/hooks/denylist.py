"""E0-R5: the destructive-command denylist.

A `PreToolUse` hook blocks destructive git and filesystem commands during any
cúramóir job. Every refusal names the safe alternative — an acceptance criterion,
not a nicety: an agent told only "no" retries the same thing or invents worse.

**This decides on what a command DOES, not how it is spelled.** The first
implementation matched literal flag spellings and therefore allowed
`git push -f`, `rm -r -f`, `rm -Rf`, `rm --recursive --force`,
`git clean -d -f -x`, `git -C path reset --hard` and `git restore`, while
denying `--force-with-lease --force-if-includes` — the safest force-push git
offers. `-f` is what a model writes most of the time, so that was the common
case rather than an edge one.

So: tokenise, skip git's global options, expand clustered short flags, normalise
synonyms (`-R` is `-r`, `-f` is `--force`), then decide on the subcommand and the
resulting flag set.

**The program is SEARCHED FOR, not assumed to be argv[0].** A first attempt at
this dispatched on `tokens[0]`, which made any prefix word a bypass — it allowed
`sudo rm -rf /`, which the regex it replaced had caught. Scanning every position
also avoids enumerating wrapper programs (sudo, env, time, nice, timeout, xargs,
nohup, stdbuf, …), a list we would be permanently behind. Nested payloads are
followed too: `$(...)`, backticks, and a shell's `-c` script.

Scope is the spec's: "during any cúramóir job". The developer's own destructive
commands in their own session are their business.

If `dcg` is installed and not switched off, delegate to it rather than
maintaining a second copy of this list (E0-R5). The switch reaching the hook is
the CURAMOIR_USE_DCG environment variable rather than the config key E0-R5
names, because this hook is deliberately stdlib-only — see pretooluse.py.

Stdlib only, and no config read: this runs on every Bash call during a job.
"""

import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    deny: bool
    reason: str = ""
    alternative: str = ""


# Shell separators a command may hide behind. `&` is included: a single
# ampersand backgrounds the left side and starts a new command, so
# `echo hi & rm -rf /tmp/x` is two commands, not one.
_SEGMENT = re.compile(r"&&|\|\||\||;|&|\n")

# Programs whose misuse this list exists to stop.
_DANGEROUS = ("rm", "git")

# Shells whose `-c` argument is a script to be judged in its own right.
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish"}

# `$(...)` and `` `...` `` bodies: a destructive command inside a substitution is
# still executed, and tokenising the outer string leaves it invisible.
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

# How deep to follow nested payloads before giving up. Bounded so a pathological
# input cannot hang the hook that runs on every Bash call.
_MAX_DEPTH = 6

# git options that appear BEFORE the subcommand. Skipping these is what stops
# `git -C /repo reset --hard` walking past the guard.
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_GIT_GLOBAL_FLAGS = {"-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects"}

# Long-flag synonyms, normalised to one spelling per meaning.
_LONG_SYNONYMS = {
    "--recursive": "r",
    "--force": "f",
    "--delete": "d",
}


def _segments(command):
    return _SEGMENT.split(command or "")


def _tokenise(segment):
    """Tokens for one segment.

    `shlex` raises on unbalanced quotes; falling back to a naive split rather
    than to no inspection matters, or an unclosed quote would be a way round the
    guard.
    """
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _parse_flags(tokens):
    """(positional arguments, set of normalised flags) for a token list.

    Clustered short flags are expanded (`-xdf` → x, d, f) and `-R` is folded to
    `r`, because `-R` is a documented synonym for `-r` in `rm`.

    `--` ends option parsing, as POSIX requires: everything after it is a
    positional, however much it looks like a flag. Treating it as just another
    flag made `rm -- -rf` — deleting a file literally named `-rf`, neither
    recursive nor forced — look like `rm -rf`.
    """
    positionals, flags = [], set()
    end_of_options = False
    for token in tokens:
        if end_of_options:
            positionals.append(token)
            continue
        if token == "--":
            flags.add("--")
            end_of_options = True
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            flags.add(_LONG_SYNONYMS.get(name, name))
        elif token.startswith("-") and len(token) > 1:
            for character in token[1:]:
                flags.add("r" if character == "R" else character)
        else:
            positionals.append(token)
    return positionals, flags


def _git_subcommand(tokens):
    """(subcommand, remaining tokens), skipping git's global options."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        base = token.split("=", 1)[0]
        if base in _GIT_GLOBAL_WITH_VALUE:
            index += 1 if "=" in token else 2
            continue
        if token in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[index + 1:]
    return None, []


def _is_forced(flags):
    # `--force` is folded to "f" by _LONG_SYNONYMS before this sees it, so
    # checking the long spelling too would be a safety net that does not exist.
    return "f" in flags


def _program_name(token):
    """The bare program name a token refers to, ignoring path and quoting."""
    return token.strip("\"'").rsplit("/", 1)[-1]


def _dangerous_positions(tokens):
    """Indices where a dangerous program appears.

    Every position, not just index 0. Dispatching on argv[0] made any prefix
    word a bypass — `sudo rm -rf /`, `xargs rm -rf`, `nice -n 5 rm -rf`,
    `timeout 5 git reset --hard` — because the program sits further along. The
    regex this replaced searched the whole segment and caught all of them, so
    argv[0] dispatch was a regression, not a refinement.

    Scanning every position also removes the need to enumerate wrapper programs,
    which is an open-ended list we would be permanently behind.
    """
    return [
        index for index, token in enumerate(tokens)
        if _program_name(token) in _DANGEROUS
    ]


def _shell_payloads(tokens):
    """Scripts passed to a shell with `-c`, to be judged in their own right.

    Only for an actual shell: `echo 'rm -rf is dangerous'` is a quoted
    multi-word token too, and must stay allowed.
    """
    payloads = []
    for index, token in enumerate(tokens[:-1]):
        if _program_name(token) in _SHELLS and tokens[index + 1] == "-c" and index + 2 < len(tokens):
            payloads.append(tokens[index + 2])
    return payloads


def _judge(tokens):
    """A Decision for a token list starting at a dangerous program."""
    if not tokens:
        return None
    program = _program_name(tokens[0])

    if program == "rm":
        _, flags = _parse_flags(tokens[1:])
        recursive = "r" in flags
        if recursive and "--no-preserve-root" in flags:
            return Decision(
                True,
                "rm -r --no-preserve-root can delete the filesystem root",
                "delete named paths without --no-preserve-root",
            )
        if recursive and _is_forced(flags):
            return Decision(
                True,
                "rm -rf deletes recursively with no confirmation and no recovery",
                "move the path aside, or delete named files without -r",
            )
        return None

    if program != "git":
        return None

    subcommand, rest = _git_subcommand(tokens)
    if subcommand is None:
        return None
    positionals, flags = _parse_flags(rest)

    if subcommand == "reset" and ("--hard" in flags or "--merge" in flags):
        return Decision(
            True,
            "git reset --hard discards uncommitted work irrecoverably",
            "git stash",
        )

    if subcommand == "push":
        leased = "--force-with-lease" in flags or "--force-if-includes" in flags
        # A `+` source forces; an EMPTY source deletes the remote branch.
        plus_refspec = any(argument.startswith("+") for argument in positionals)
        delete_refspec = any(argument.startswith(":") for argument in positionals)
        # `--delete` is folded to "d" by _LONG_SYNONYMS, so check that spelling.
        if "d" in flags or delete_refspec:
            return Decision(
                True,
                "git push --delete (or an empty refspec) deletes a remote branch outright",
                "delete the branch locally and let the PR close it",
            )
        if (_is_forced(flags) and not leased) or plus_refspec:
            return Decision(
                True,
                "git push --force can overwrite a remote branch's history",
                "git push --force-with-lease",
            )
        return None

    if subcommand in ("checkout", "switch") and (
        "--" in flags or _is_forced(flags) or "--discard-changes" in flags
    ):
        # `-f` discards the whole working tree, exactly as `git reset --hard`
        # does — the rule immediately above. Firing only on the literal `--`
        # separator caught the rarer spelling and let the common one through.
        return Decision(
            True,
            f"git {subcommand} with -f/-- overwrites the working tree with no backup",
            "git stash push <path>",
        )

    if subcommand == "restore":
        # Decide on whether the WORKING TREE is targeted, not on a spelling.
        # `-S` is git's documented short form of `--staged`, and
        # `--staged --worktree` restores both — so checking for the long flag
        # alone was wrong in both directions at once.
        staged_only = (
            ("--staged" in flags or "S" in flags)
            and "--worktree" not in flags
            and "W" not in flags
        )
        if not staged_only:
            return Decision(
                True,
                "git restore overwrites the working tree from a commit with no backup",
                "git stash push <path>",
            )

    if subcommand == "clean" and _is_forced(flags):
        return Decision(
            True,
            "git clean -f deletes untracked files with no recovery path",
            "git clean -n",
        )

    if subcommand == "branch":
        # -D is exactly -d --force. Either spelling discards unmerged work.
        if "D" in flags or ("d" in flags and _is_forced(flags)):
            return Decision(
                True,
                "git branch -D deletes a branch even if its work is unmerged",
                "git branch -d",
            )
        return None

    # The commands that make `git reset --hard` actually unrecoverable. This
    # module's own refusal text calls that command irrecoverable, which is only
    # true while the reflog survives — and expiring the reflog then pruning is
    # the documented recipe for destroying it, so it is the natural next thing
    # an agent tries after being refused.
    if subcommand == "reflog" and positionals and positionals[0] == "expire":
        return Decision(
            True,
            "git reflog expire destroys the recovery log that makes other git operations undoable",
            "git reflog",
        )

    if subcommand == "gc" and any(flag.startswith("--prune") for flag in flags):
        return Decision(
            True,
            "git gc --prune discards unreachable objects, finalising earlier destruction",
            "git gc --auto",
        )

    if subcommand == "update-ref" and "d" in flags:
        return Decision(
            True,
            "git update-ref -d deletes a ref outright, with no reflog entry for the deletion",
            "git branch -d",
        )

    if subcommand == "worktree" and positionals and positionals[0] == "remove" and _is_forced(flags):
        return Decision(
            True,
            "git worktree remove --force discards a worktree including uncommitted work",
            "git worktree remove (without --force)",
        )

    if subcommand == "submodule" and positionals and positionals[0] == "deinit" and _is_forced(flags):
        return Decision(
            True,
            "git submodule deinit -f discards a submodule's local modifications",
            "git submodule deinit (without -f)",
        )

    if subcommand == "filter-branch":
        return Decision(
            True,
            "git filter-branch rewrites history across the whole repository",
            "git filter-repo on a clone, reviewed before pushing",
        )

    if subcommand == "stash" and positionals and positionals[0] in ("drop", "clear"):
        return Decision(
            True,
            "git stash drop/clear destroys stashed work permanently",
            "git stash list",
        )

    return None


def _inspect(command, depth):
    """A raw Decision for `command`, following nested payloads. None if clean."""
    if depth > _MAX_DEPTH or not command:
        return None

    for segment in _segments(command):
        # Command substitutions execute too, and tokenising the outer string
        # leaves their contents invisible.
        for match in _SUBSTITUTION.finditer(segment):
            inner = match.group(1) or match.group(2) or ""
            nested = _inspect(inner, depth + 1)
            if nested is not None:
                return nested

        tokens = _tokenise(segment)

        for payload in _shell_payloads(tokens):
            nested = _inspect(payload, depth + 1)
            if nested is not None:
                return nested

        for index in _dangerous_positions(tokens):
            decision = _judge(tokens[index:])
            if decision is not None:
                return decision
    return None


def decide(command, in_job):
    """Whether to block `command`.

    `in_job` is the caller's answer to "are we inside a cúramóir job?" —
    computed by `in_curamoir_job`, passed in so this stays pure and testable.
    """
    if not in_job:
        return Decision(deny=False)

    decision = _inspect(command, depth=0)
    if decision is None:
        return Decision(deny=False)
    return Decision(
        deny=True,
        reason=f"blocked by cúramóir: {decision.reason}. Use `{decision.alternative}` instead.",
        alternative=decision.alternative,
    )


def in_curamoir_job(env, repo=None):
    """Whether we are running inside a cúramóir job.

    Two signals, per DEC-5. The env var is primary: hook processes inherit the
    parent environment, so the runner (E10-R1) exports CURAMOIR_JOB. The
    supervisor lock is the fallback, because CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1
    strips extra variables and we would otherwise stop enforcing silently.

    NOTE: nothing writes the lock yet (E10-LOCK), so the fallback is currently
    inert and /curamoir:doctor reports that as a warning.
    """
    if env.get("CURAMOIR_JOB"):
        return True
    if repo is not None:
        return (repo / ".curamoir" / "state" / "lock").exists()
    return False
