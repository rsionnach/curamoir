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


# Shell separators a command may hide behind.
_SEGMENT = re.compile(r"&&|\|\||\||;|\n")

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
    """
    positionals, flags = [], set()
    for token in tokens:
        if token == "--":
            flags.add("--")
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
    return "f" in flags or "--force" in flags


def _judge(tokens):
    """A Decision for one tokenised segment, or None if nothing applies."""
    if not tokens:
        return None
    program = tokens[0].rsplit("/", 1)[-1]

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
        plus_refspec = any(argument.startswith("+") for argument in positionals)
        if (_is_forced(flags) and not leased) or plus_refspec:
            return Decision(
                True,
                "git push --force can overwrite a remote branch's history",
                "git push --force-with-lease",
            )
        return None

    if subcommand == "checkout" and "--" in flags:
        return Decision(
            True,
            "git checkout -- <path> overwrites the file from a commit with no backup",
            "git stash push <path>",
        )

    if subcommand == "restore" and "--staged" not in flags:
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

    if subcommand == "stash" and positionals and positionals[0] in ("drop", "clear"):
        return Decision(
            True,
            "git stash drop/clear destroys stashed work permanently",
            "git stash list",
        )

    return None


def decide(command, in_job):
    """Whether to block `command`.

    `in_job` is the caller's answer to "are we inside a cúramóir job?" —
    computed by `in_curamoir_job`, passed in so this stays pure and testable.
    """
    if not in_job:
        return Decision(deny=False)

    for segment in _segments(command):
        decision = _judge(_tokenise(segment))
        if decision is not None:
            return Decision(
                deny=True,
                reason=f"blocked by cúramóir: {decision.reason}. Use `{decision.alternative}` instead.",
                alternative=decision.alternative,
            )
    return Decision(deny=False)


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
