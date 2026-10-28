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
commands in their own session are their business, except while the supervisor
lock exists: the lock is the job signal's fallback (hooks/_entry.in_curamoir_job),
so a running job, or a stale lock a killed one left, puts every session in that
repository under the denylist until the lock is gone. The denial says so.

If `dcg` is installed and not switched off, delegate to it rather than
maintaining a second copy of this list (E0-R5). The switch reaching the hook is
CURAMOIR_USE_DCG rather than the config key E0-R5 names, because this hook is
deliberately stdlib-only — see pretooluse.py.

Stdlib only, and no config read: this runs on every Bash call during a job.
"""

import re
from fnmatch import fnmatch

from curamoir.hooks._shell import (
    SHELLS,
    lex,
    program_name,
    redirection_targets,
    segments,
    strip_redirections,
)
from curamoir.hooks.verdict import Category, Decision, Rule, Unverifiable

# Programs whose misuse this list exists to stop.
_DANGEROUS = ("rm", "git", "find")

#: cúramóir's own state. A job may read it; rewriting or moving it destroys the
#: ledger, the journal and the handoff — and silences the four hooks that would
#: have recorded the loss, because `_entry.run` goes quiet when `.curamoir` is
#: not a directory. Judged as a path rule rather than per program, because
#: `mv`, `cp`, `tee` and a `>` redirection all reach it and all are allowlisted.
_OWN_STATE = ".curamoir"


# `$(...)` and `` `...` `` bodies: a destructive command inside a substitution is
# still executed, and tokenising the outer string leaves it invisible.
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

# How deep to follow nested payloads before giving up. Bounded so a pathological
# input cannot hang the hook that runs on every Bash call.
_MAX_DEPTH = 6

#: Most dangerous-program mentions the guard will inspect individually. Far
#: above anything a real command does; see the Rule above for the arithmetic.
MAX_DANGEROUS_POSITIONS = 50

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
    positional, however much it looks like a flag — `rm -- -rf` deletes a file
    NAMED `-rf` and is neither recursive nor forced. It is ALSO recorded in the
    flag set, because `git checkout --` is itself a destructive spelling the
    rules below need to see.
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
    # `--force` is normalised to "f" by _LONG_SYNONYMS before this is reached,
    # so the long spelling must NOT also be checked here: it never arrives.
    return "f" in flags


def _looks_like_a_path(token):
    """Whether a positional names a path rather than a branch.

    Deliberately generous: a branch name containing a slash is common
    (`curamoir/sweep`), so a slash alone cannot decide it. What distinguishes a
    discard is naming the tree — `.`, a glob, a path that exists — and when the
    answer is uncertain the safe reading is "this is a path", because the cost
    of being wrong is a retry on one side and the working tree on the other.
    """
    if token in (".", "..", "*") or token.startswith("./"):
        return True
    if any(character in token for character in "*?["):
        return True
    return "." in token.rsplit("/", 1)[-1]


#: Suffixes of files that hold code. A truncating `>` over one of these is a
#: destroy-without-backup in the same family as `git checkout -- <path>`.
#: Deliberately code only: a job legitimately writes `> report.json` and
#: `> out.log`, and refusing those is the false-refusal pressure that gets a
#: guard switched off. Writing code through shell redirection is not something a
#: job does — it edits through the Edit tool, which keeps the previous content.
_SOURCE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java",
    ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift", ".sh",
    ".bash", ".zsh", ".sql", ".scala", ".ex", ".exs",
)


def _is_source_file(target):
    return target.endswith(_SOURCE_SUFFIXES)


#: Allowlisted programs that OVERWRITE a file named in their arguments, and
#: which argument that is. The redirection rule caught `cat /dev/null > x.py`
#: and missed `echo '' | tee x.py`, `cp -f /dev/null x.py` and `mv /dev/null
#: x.py` — the same effect through three allowlisted programs. By this module's
#: own invariant they belong with the redirection case, not apart from it.
_WRITE_TARGETS = {"tee": "all", "cp": "last", "mv": "last"}


def _write_targets(tokens):
    """The paths `tokens` would overwrite by virtue of the program they run."""
    which = _WRITE_TARGETS.get(program_name(tokens[0]))
    if which is None:
        return []
    positionals = [t for t in tokens[1:] if not t.startswith("-")]
    if not positionals:
        return []
    return positionals if which == "all" else positionals[-1:]


def _targets_own_state(tokens):
    """Whether any argument points inside cúramóir's own state directory.

    Matched with `fnmatch`, not equality, because the shell expands the pattern
    before the command runs: `mv -f .curamoi? /tmp` moves exactly what
    `mv -f .curamoir /tmp` moves, and one character defeated a literal
    comparison.
    """
    return any(_targets_own_state_path(token) for token in tokens[1:])


def _targets_own_state_path(token):
    for part in token.strip("\"'").split("/"):
        if part == _OWN_STATE or fnmatch(_OWN_STATE, part):
            return True
    return False


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
        if recursive:
            # `-f` is not what makes this unrecoverable. On a non-tty — which is
            # what an agent's shell is — plain `rm -r` prompts for nothing
            # either, so the two spellings have the same effect and the rule
            # judges the effect.
            return Rule(
                "rm -r deletes recursively with no confirmation and no recovery",
                "move the path aside, or delete named files without -r",
            )
        return None

    if program == "find":
        # `find` is allowlisted as a read-only search, but it carries its own
        # deletion and execution primitives, and reaches them without naming
        # `rm` anywhere the position scan would see it.
        for flag in tokens[1:]:
            if flag == "-delete":
                return Rule(
                    "find -delete removes every matching path with no confirmation",
                    "find the paths first, then delete the ones you mean by name",
                )
            if flag in ("-exec", "-execdir", "-ok", "-okdir"):
                return Rule(
                    f"find {flag} runs a command the guard cannot verify against "
                    "every match",
                    "find the paths first, then act on them by name",
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

    if subcommand in ("checkout", "switch"):
        # `git checkout <anything>` denies, because path-versus-ref is not
        # decidable by reading the command. The previous heuristic guessed, and
        # guessed the unsafe way for an uncertain token: an extension-less name
        # — `src`, `tests`, `Makefile`, `Dockerfile` — answered "branch", so
        # `git checkout src` silently overwrote a whole subtree. Dotted names
        # went wrong in the other direction, refusing the tag `v1.2.3`.
        #
        # So stop guessing. `git switch` is the purpose-built branch tool, it is
        # allowlisted, and the refusal names it — which costs a retry on the
        # branch case and saves the working tree on the path case.
        # `-b`/`-B`/`--orphan`/`--track` CREATE a branch; the positional is a
        # new branch name and there is no tree to lose.
        creating = flags & {"b", "B", "--orphan", "--track", "-t", "t"}
        if subcommand == "checkout" and positionals and not creating:
            return Rule(
                "git checkout over a path overwrites the working tree with no "
                "backup, and a path cannot be told from a branch by reading it",
                "git switch <branch> to change branch, git stash push <path> to "
                "set work aside",
            )
        if "--" in flags or _is_forced(flags) or "--discard-changes" in flags:
            return Rule(
                f"git {subcommand} with -f/-- overwrites the working tree with no backup",
                "git stash push <path>",
            )
        return None

    if subcommand == "restore":
        # Decide on whether the WORKING TREE is targeted, not on a spelling.
        # `-S` is git's documented short form of `--staged`, and
        # `--staged --worktree` restores both.
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


def _inspect(tokens, depth):
    """The Rule `tokens` violate, following nested payloads. None if clean."""
    if depth > _MAX_DEPTH or not tokens:
        return None

    for segment in segments(tokens):
        # Judged before the operators are stripped, because the write target is
        # the effect: `cat /dev/null > src/foo.py` truncates a source file while
        # every program in it is allowlisted.
        for target in redirection_targets(segment):
            if _OWN_STATE in target.split("/"):
                return _own_state_rule("redirecting output over")
            if _is_source_file(target):
                return Rule(
                    f"redirecting output over {target} truncates a source file, "
                    "discarding whatever it held with no backup",
                    "edit the file with the Edit tool, which keeps the old content",
                )
        bare = strip_redirections(segment)
        if not bare:
            continue

        if _targets_own_state(bare):
            return _own_state_rule(f"{program_name(bare[0])!r} over")

        for target in _write_targets(bare):
            if _is_source_file(target):
                return Rule(
                    f"{program_name(bare[0])} would overwrite {target}, a source "
                    "file, discarding whatever it held with no backup",
                    "edit the file with the Edit tool, which keeps the old content",
                )

        # Command substitutions execute too, and a rule that reads token
        # program names cannot see inside one. Searched over the rejoined
        # segment because a substitution is not confined to a single token:
        # `echo `rm -rf /tmp/x`` lexes as "`rm", "-rf", "/tmp/x`".
        for match in _SUBSTITUTION.finditer(" ".join(segment)):
            inner = lex(match.group(1) or match.group(2) or "")
            nested = None if inner is None else _inspect(inner, depth + 1)
            if nested is not None:
                return nested

        for payload in _shell_payloads(bare):
            inner = lex(payload)
            nested = None if inner is None else _inspect(inner, depth + 1)
            if nested is not None:
                return nested

        positions = _dangerous_positions(bare)
        if len(positions) > MAX_DANGEROUS_POSITIONS:
            # Bounding the LENGTH did not bound the work: `_judge` slices and
            # then walks the remaining tokens, so N dangerous tokens cost
            # O(N²). `echo rm rm rm …` is 15,924 characters — inside
            # MAX_COMMAND_CHARS — and took 6.4s against a 5s hook budget that
            # fails open. Refusing unverified is the same trade the length
            # bound makes, and nothing real comes close to this many.
            # UNVERIFIABLE, not DESTRUCTIVE, though the rule lives in the
            # destructive pass: this is "the guard could not finish reading
            # it", which is the length bound's category and UNVERIFIABLE's own
            # definition. DEC-9 exists so callers assert the category rather
            # than the prose, so the two bounds must not report differently.
            return Unverifiable(
                f"this command names {len(positions)} potentially destructive "
                "programs, too many for the guard to check inside its time "
                "budget — and a hook that times out permits the action",
                "run one command at a time, or put the sequence in a committed script",
            )
        for index in positions:
            decision = _judge(bare[index:])
            if decision is not None:
                return decision
    return None


def _own_state_rule(what):
    return Rule(
        f"{what} .curamoir/ destroys cúramóir's own ledger, journal and handoff "
        "— and silences the hooks that would have recorded the loss",
        "leave .curamoir/ to cúramóir; a job writes through /curamoir: commands",
    )


def judge_destructive(command, in_job):
    """Whether `command` matches a destructive rule.

    Takes a token list from `policy`, so that the two passes can never disagree
    about where one command ends and the next begins. A raw string is accepted
    too and lexed here, which is how the rule tests and any direct caller use
    it; an unlexable string is unverifiable, and refusing is the safe reading.

    `in_job` is the caller's answer to "are we inside a cúramóir job?" —
    computed by `in_curamoir_job`, passed in so this stays pure and testable.
    """
    if not in_job:
        return Decision(deny=False)

    if isinstance(command, str):
        tokens = lex(command)
        if tokens is None:
            return Decision(
                deny=True,
                reason=(
                    "blocked by cúramóir: this command could not be parsed "
                    "(unbalanced quoting), so the guard cannot verify it."
                ),
                alternative="rewrite the command with balanced quoting",
                category=Category.UNPARSEABLE,
            )
    else:
        tokens = command

    rule = _inspect(tokens, depth=0)
    if rule is None:
        return Decision(deny=False)
    return Decision(
        deny=True,
        reason=f"blocked by cúramóir: {rule.reason}. Use `{rule.alternative}` instead.",
        alternative=rule.alternative,
        category=(
            Category.UNVERIFIABLE if isinstance(rule, Unverifiable)
            else Category.DESTRUCTIVE
        ),
    )


