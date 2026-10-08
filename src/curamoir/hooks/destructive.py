"""E0-R5: the destructive-command rules — the second pass of DEC-9's policy.

A `PreToolUse` hook blocks destructive git and filesystem commands during any
cúramóir job. Every refusal names the safe alternative — an acceptance
criterion, not a nicety: an agent told only "no" retries the same thing or
invents worse.

Two invariants:

- **Decide on what a command DOES, not how it is spelled.** Tokenise, skip git's
  global options (`git -C path reset --hard`), expand clustered short flags
  (`-Rf`), normalise synonyms (`-R`→`r`, `--force`→`f`), then decide on the
  subcommand and the resulting flag set. Note the asymmetry:
  `--force-with-lease` and `--force-if-includes` are the SAFE spellings and must
  stay allowed.
- **Search every position for the program; never assume argv[0].** A wrapper puts
  it further along (`sudo rm -rf /`, `timeout 5 git reset --hard`), and scanning
  removes the need to enumerate wrappers — sudo, env, nice, xargs, nohup, … — an
  open-ended list. Nested payloads are followed too: `$(...)`, backticks, and a
  shell's `-c` script.

Scope is the spec's: "during any cúramóir job". The developer's own destructive
commands in their own session are their business.

If `dcg` is installed and not switched off, delegate to it rather than
maintaining a second copy of this list (E0-R5). The switch reaching the hook is
CURAMOIR_USE_DCG rather than the config key E0-R5 names, because this hook is
deliberately stdlib-only — see pretooluse.py.

Stdlib only, and no config read: this runs on every Bash call during a job.
"""

import re

from curamoir.hooks._shell import SHELLS, program_name, segments, tokenise
from curamoir.hooks.verdict import Category, Decision, Rule


# Programs whose misuse this list exists to stop.
_DANGEROUS = ("rm", "git")


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


def _dangerous_positions(tokens):
    """Indices where a dangerous program appears — EVERY position, not just 0.

    A wrapper puts the program further along: `sudo rm -rf /`, `xargs rm -rf`,
    `nice -n 5 rm -rf`, `timeout 5 git reset --hard`. Scanning also removes the
    need to enumerate wrapper programs, an open-ended list.
    """
    return [
        index for index, token in enumerate(tokens)
        if program_name(token) in _DANGEROUS
    ]


def _shell_payloads(tokens):
    """Scripts passed to a shell with `-c`, to be judged in their own right.

    Only for an actual shell: `echo 'rm -rf is dangerous'` is a quoted
    multi-word token too, and must stay allowed.
    """
    payloads = []
    for index, token in enumerate(tokens[:-1]):
        if program_name(token) in SHELLS and tokens[index + 1] == "-c" and index + 2 < len(tokens):
            payloads.append(tokens[index + 2])
    return payloads


def _judge(tokens):
    """The Rule a token list violates, or None if it violates nothing."""
    if not tokens:
        return None
    program = program_name(tokens[0])

    if program == "rm":
        _, flags = _parse_flags(tokens[1:])
        recursive = "r" in flags
        if recursive and "--no-preserve-root" in flags:
            return Rule(
                                "rm -r --no-preserve-root can delete the filesystem root",
                "delete named paths without --no-preserve-root",
            )
        if recursive and _is_forced(flags):
            return Rule(
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
        return Rule(
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
            return Rule(
                                "git push --delete (or an empty refspec) deletes a remote branch outright",
                "delete the branch locally and let the PR close it",
            )
        if (_is_forced(flags) and not leased) or plus_refspec:
            return Rule(
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
        return Rule(
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
            return Rule(
                                "git restore overwrites the working tree from a commit with no backup",
                "git stash push <path>",
            )

    if subcommand == "clean" and _is_forced(flags):
        return Rule(
                        "git clean -f deletes untracked files with no recovery path",
            "git clean -n",
        )

    if subcommand == "branch":
        # -D is exactly -d --force. Either spelling discards unmerged work.
        if "D" in flags or ("d" in flags and _is_forced(flags)):
            return Rule(
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
        return Rule(
                        "git reflog expire destroys the recovery log that makes other git operations undoable",
            "git reflog",
        )

    if subcommand == "gc" and any(flag.startswith("--prune") for flag in flags):
        return Rule(
                        "git gc --prune discards unreachable objects, finalising earlier destruction",
            "git gc --auto",
        )

    if subcommand == "update-ref" and "d" in flags:
        return Rule(
                        "git update-ref -d deletes a ref outright, with no reflog entry for the deletion",
            "git branch -d",
        )

    if subcommand == "worktree" and positionals and positionals[0] == "remove" and _is_forced(flags):
        return Rule(
                        "git worktree remove --force discards a worktree including uncommitted work",
            "git worktree remove (without --force)",
        )

    if subcommand == "submodule" and positionals and positionals[0] == "deinit" and _is_forced(flags):
        return Rule(
                        "git submodule deinit -f discards a submodule's local modifications",
            "git submodule deinit (without -f)",
        )

    if subcommand == "filter-branch":
        return Rule(
                        "git filter-branch rewrites history across the whole repository",
            "git filter-repo on a clone, reviewed before pushing",
        )

    if subcommand == "stash" and positionals and positionals[0] in ("drop", "clear"):
        return Rule(
                        "git stash drop/clear destroys stashed work permanently",
            "git stash list",
        )

    return None


def _inspect(command, depth):
    """The Rule `command` violates, following nested payloads. None if clean."""
    if depth > _MAX_DEPTH or not command:
        return None

    for segment in segments(command):
        # Command substitutions execute too, and tokenising the outer string
        # leaves their contents invisible.
        for match in _SUBSTITUTION.finditer(segment):
            inner = match.group(1) or match.group(2) or ""
            nested = _inspect(inner, depth + 1)
            if nested is not None:
                return nested

        tokens = tokenise(segment)

        for payload in _shell_payloads(tokens):
            nested = _inspect(payload, depth + 1)
            if nested is not None:
                return nested

        for index in _dangerous_positions(tokens):
            decision = _judge(tokens[index:])
            if decision is not None:
                return decision
    return None


def judge_destructive(command, in_job):
    """Whether `command` matches a destructive rule.

    `in_job` is the caller's answer to "are we inside a cúramóir job?" —
    computed by `in_curamoir_job`, passed in so this stays pure and testable.
    """
    if not in_job:
        return Decision(deny=False)

    rule = _inspect(command, depth=0)
    if rule is None:
        return Decision(deny=False)
    return Decision(
        deny=True,
        reason=f"blocked by cúramóir: {rule.reason}. Use `{rule.alternative}` instead.",
        alternative=rule.alternative,
        category=Category.DESTRUCTIVE,
    )


