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

import os
import re
from fnmatch import fnmatch

from curamoir.hooks._policy_common import _CWD, _subcommand_at
from curamoir.hooks._policy_writes import _uniq_positionals
from curamoir.hooks._shell import (
    SHELLS,
    is_separator,
    lex,
    program_name,
    redirection_targets,
    segments,
    strip_redirections,
)
from curamoir.hooks.verdict import Category, Decision, Rule, Unverifiable

# Programs whose misuse this list exists to stop.
_DANGEROUS = ("rm", "git", "find", "gh", "bd")

#: What a job does instead of deleting or truncating a file (round 10).
DELETION_ALTERNATIVE = (
    "a job does not delete or truncate files: put the deletion in the PR's "
    "description, or `git rm --cached` to untrack"
)

#: What a job does instead of moving an existing branch (round 10).
_BRANCH_MOVE_ALTERNATIVE = (
    "create a new branch (git switch -c <new>) instead of moving an existing one"
)

#: What a job does instead of deleting something on the remote (round 10).
_REMOTE_DELETION_ALTERNATIVE = "leave the branch; say in the PR what should be deleted"

#: Redirection operators that TRUNCATE their target (not `>>`, which appends).
_TRUNCATING = frozenset({">", ">|", "&>"})

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


def _abbreviated(flags, option, min_length=4):
    """Whether `flags` hold `option` or a prefix of it at least `min_length`
    characters long with the dashes, which git's option parser reads as
    `option` when no other option shares the prefix. The default, two letters
    after the dashes, is the policy's threshold for git
    (_policy_common._abbreviates); a caller passes more where a shorter prefix
    is shared (`--pr` is --progress's too)."""
    return any(flag.startswith("--") and len(flag) >= min_length and option.startswith(flag)
               for flag in flags)


#: Advice for a gh change, by what is changed (round 12).
_GH_ISSUE_ALTERNATIVE = "close it instead (gh issue close)"
_GH_SETTINGS_ALTERNATIVE = (
    "say in the PR what should change; a job does not change the repository's settings"
)
_GH_API_ALTERNATIVE = "a job reads GitHub with gh api; it does not change it"

#: gh api's options that take a value, short and long, so the endpoint and
#: the method are found past them.
_GH_API_VALUED_SHORT = frozenset("XHfFqtp")
_GH_API_VALUED_LONG = frozenset({
    "--method", "--header", "--field", "--raw-field", "--hostname", "--jq",
    "--template", "--cache", "--preview", "--input",
})


def _gh_rule(arguments):
    """The Rule for a gh command that changes GitHub state a job leaves alone
    (rounds 10 to 12), with advice that fits what it changes: `delete` among
    the first three words (an issue: close it; anything else: say so in the
    PR), `repo archive`/`rename`, `pr close -d`, `pr merge`, and any `gh api`
    that is not a read (_gh_api_rule); else None."""
    positionals, flags = _parse_flags(arguments)
    words = positionals[:3]
    if words[:1] == ["api"]:
        return _gh_api_rule(arguments[arguments.index("api") + 1:])
    if words[:2] == ["pr", "close"] and (
            "d" in flags or _abbreviated(flags, "--delete-branch", 5)):
        return Rule("gh pr close --delete-branch deletes the PR's branch on the remote",
                    _REMOTE_DELETION_ALTERNATIVE)
    if words[:2] == ["pr", "merge"]:
        return Rule("gh pr merge merges the PR, which is the reviewer's decision",
                    "leave the PR for review; a job never merges")
    if words[:2] == ["issue", "delete"]:
        return Rule("gh issue delete deletes the issue and its history",
                    _GH_ISSUE_ALTERNATIVE)
    if "delete" in words or words[:2] in (["repo", "archive"], ["repo", "rename"]):
        what = " ".join(words[:words.index("delete") + 1] if "delete" in words else words[:2])
        return Rule(f"gh {what} changes or deletes it on GitHub", _GH_SETTINGS_ALTERNATIVE)
    return None


def _gh_api_rule(arguments):
    """The Rule for a `gh api` call (`arguments` follow `api`) that is not a
    read (round 12): an explicit method other than GET or HEAD, in any
    spelling (`-X`, `-XPUT`, `-iX PUT`, `--method=post`), or a body (fields,
    `--input`) without an explicit GET, since gh then POSTs. `graphql` is
    always POSTed, so it is judged by its body instead: a mutation, a field
    read from a file (`-F query=@q.graphql`) or `--input` is refused. A
    DELETE of a git ref gets the branch advice."""
    method, fields, has_input, positionals = None, [], False, []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        following = arguments[index] if index < len(arguments) else ""
        if argument.startswith("--"):
            name, equals, value = argument.partition("=")
            if name not in _GH_API_VALUED_LONG:
                continue
            if not equals:
                value, index = following, index + 1
            if name == "--method":
                method = value
            elif name in ("--field", "--raw-field"):
                fields.append(value)
            elif name == "--input":
                has_input = True
            continue
        if not argument.startswith("-") or argument == "-":
            positionals.append(argument)
            continue
        for position, character in enumerate(argument[1:], start=2):
            if character in _GH_API_VALUED_SHORT:
                value = argument[position:]
                if not value:
                    value, index = following, index + 1
                if character == "X":
                    method = value
                elif character in "fF":
                    fields.append(value)
                break
    if "graphql" in positionals:
        if has_input or any("mutation" in f.lower() or f.partition("=")[2].startswith("@")
                            for f in fields):
            return Rule("gh api graphql with a mutation, or a body from a file, may "
                        "change GitHub state", _GH_API_ALTERNATIVE)
        return None
    verb = (method or "").upper()
    if verb in ("GET", "HEAD") or (not verb and not fields and not has_input):
        return None
    if verb == "DELETE" and any("git/refs" in p for p in positionals):
        return Rule("gh api -X DELETE on a ref deletes a branch or tag on the remote",
                    _REMOTE_DELETION_ALTERNATIVE)
    return Rule(f"gh api {'-X ' + verb if verb else 'with fields (a POST)'} changes "
                "GitHub state", _GH_API_ALTERNATIVE)


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


def _would_overwrite(target):
    """Whether writing `target` would destroy a source file's content: it is
    a source file, and it exists, or the working directory is unknown so that
    cannot be told (round 10: `echo x > new_dir/new.py` creates a file, which
    is no loss)."""
    if not _is_source_file(target):
        return False
    cwd = _CWD.get()
    return cwd is None or os.path.lexists(os.path.join(cwd, target))


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

    if program == "gh":
        return _gh_rule(tokens[1:])

    if program == "bd":
        at, refused = _subcommand_at("bd", tokens[1:])
        if refused is None and at is not None and tokens[1:][at] == "delete":
            return Rule(
                "bd delete removes an issue and its history from the tracker",
                "bd close <id>, which keeps the record",
            )
        return None

    if program != "git":
        return None

    subcommand, rest = _git_subcommand(tokens)
    if subcommand is None:
        return None
    positionals, flags = _parse_flags(rest)

    # An unambiguous prefix of a long option is that option to git (`--ha` is
    # `--hard`), so the destructive ones are matched by prefix too (R5
    # edge-case review). `--merge` likewise, from `--me` (`--m` is ambiguous
    # with --mixed).
    if subcommand == "reset" and (_abbreviated(flags, "--hard")
                                  or _abbreviated(flags, "--merge")):
        return Rule(
            "git reset --hard discards uncommitted work irrecoverably",
            "git stash",
        )

    if subcommand == "push":
        # A `+` source forces; an EMPTY source deletes the remote branch.
        plus_refspec = any(argument.startswith("+") for argument in positionals)
        delete_refspec = any(argument.startswith(":") for argument in positionals)
        # `--delete` is folded to "d" by _LONG_SYNONYMS, so check that
        # spelling, and its prefixes from `--de` (`--d` is ambiguous with
        # --dry-run).
        if "d" in flags or _abbreviated(flags, "--delete") or delete_refspec:
            return Rule(
                "git push --delete (or an empty refspec) deletes a remote branch outright",
                "delete the branch locally and let the PR close it",
            )
        # `--prune` from `--pru`: `--pr` is shared with --progress (round 6).
        if _abbreviated(flags, "--prune", 5):
            return Rule(
                "git push --prune deletes every remote branch that has no local "
                "counterpart",
                "git push <remote> <branch>",
            )
        if _abbreviated(flags, "--mirror"):
            return Rule(
                "git push --mirror overwrites every ref on the remote with the local "
                "one and deletes the remote's others",
                "git push <remote> <branch>",
            )
        # An explicit -f/--force forces whatever lease is also given: `git
        # help push` says -f "disables these checks", so `--force-with-lease`
        # beside it protects nothing (round 13). The lease alone is the safe
        # spelling, and passes.
        if _is_forced(flags) or plus_refspec:
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
        # `-B` (checkout) and `-C`/`--force-create` (switch) create OR RESET:
        # an existing branch is moved, and the commits only it had are dropped
        # (round 10).
        if (subcommand == "checkout" and "B" in flags) or (
                subcommand == "switch" and ("C" in flags or _abbreviated(
                    flags, "--force-create", 9))):
            return Rule(
                f"git {subcommand} {'-B' if subcommand == 'checkout' else '-C'} moves "
                "an existing branch, dropping the commits only it had",
                _BRANCH_MOVE_ALTERNATIVE,
            )
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
        # -f moves an existing branch, -M renames over one and -C copies over
        # one: each drops the commits only the overwritten branch had.
        if _is_forced(flags) or "M" in flags or "C" in flags:
            return Rule(
                "git branch -f/-M/-C moves or overwrites an existing branch, dropping "
                "the commits only it had",
                _BRANCH_MOVE_ALTERNATIVE,
            )
        return None

    if subcommand == "tag" and "d" in flags:
        return Rule(
            "git tag -d deletes a tag, which others may have fetched and built from",
            "leave it; say in the PR which tag should go",
        )

    if subcommand == "rm" and "--cached" in flags and "r" in flags:
        return Rule(
            "git rm -r --cached untracks a whole directory at once, more than a "
            "change usually means to",
            "git rm --cached <path> for each file",
        )

    if subcommand == "rm" and "--cached" not in flags:
        return Rule(
            "git rm deletes files from the working tree",
            DELETION_ALTERNATIVE,
        )

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


def _pipelines(tokens):
    """`tokens` split into pipelines, each a list of segments: `|` and `|&`
    join segments that run at the same time; every other separator (`;`,
    `&&`, `||`, `&`, a newline) ends a pipeline."""
    found, current, segment = [], [], []
    for token in tokens:
        if token in ("|", "|&"):
            current.append(segment)
            segment = []
        elif is_separator(token):
            current.append(segment)
            found.append([s for s in current if s])
            current, segment = [], []
        else:
            segment.append(token)
    current.append(segment)
    found.append([s for s in current if s])
    return [pipeline for pipeline in found if pipeline]


def _empties_its_input(tokens):
    """The Rule for a truncating write (`>`, `>|`, `&>`) whose target the same
    pipeline also reads: a word of any of its commands, or a `<` source, or
    uniq's output path when it is the input (rounds 10 and 11). The shell
    empties the target before anything reads it, so `cat a | sort > a` and
    `sort < a > a` leave `a` empty. Commands joined by `&&` or `;` run one
    after another, so `sort a > b && mv b a` is not one of these."""
    for pipeline in _pipelines(tokens):
        read = set()
        for segment in pipeline:
            bare = strip_redirections(segment)
            read |= {os.path.normpath(word) for word in bare[1:]}
            read |= {os.path.normpath(segment[i + 1])
                     for i, token in enumerate(segment[:-1]) if token == "<"}
            if bare and program_name(bare[0]) == "uniq":
                paths = _uniq_positionals(bare[1:])
                if len(paths) >= 2 and os.path.normpath(paths[0]) == os.path.normpath(
                        paths[1]):
                    return _emptied(paths[1])
        for segment in pipeline:
            for index, operator in enumerate(segment[:-1]):
                target = segment[index + 1]
                if operator in _TRUNCATING and os.path.normpath(target) in read:
                    return _emptied(target)
    return None


def _emptied(target):
    return Rule(
        f"writing over {target}, which the same command reads, empties it before "
        "it is read",
        "write to a new file, then mv it over, as two separate commands",
    )


def _inspect(tokens, depth):
    """The Rule `tokens` violate, following nested payloads. None if clean."""
    if depth > _MAX_DEPTH or not tokens:
        return None

    emptied = _empties_its_input(tokens)
    if emptied is not None:
        return emptied

    for segment in segments(tokens):
        # Judged before the operators are stripped, because the write target is
        # the effect: `cat /dev/null > src/foo.py` truncates a source file while
        # every program in it is allowlisted.
        bare = strip_redirections(segment)
        for target in redirection_targets(segment):
            if _OWN_STATE in target.split("/"):
                return _own_state_rule("redirecting output over")
        for index, operator in enumerate(segment[:-1]):
            if operator not in _TRUNCATING:
                continue
            target = segment[index + 1]
            if _would_overwrite(target):
                return Rule(
                    f"redirecting output over {target} truncates a source file, "
                    "discarding whatever it held with no backup",
                    "edit the file with the Edit tool, which keeps the old content",
                )
        if not bare:
            continue

        if _targets_own_state(bare):
            return _own_state_rule(f"{program_name(bare[0])!r} over")

        for target in _write_targets(bare):
            if _would_overwrite(target):
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


