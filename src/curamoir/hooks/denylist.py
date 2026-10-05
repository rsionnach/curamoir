"""E0-R5: the destructive-command denylist.

A `PreToolUse` hook blocks destructive git and filesystem commands during any
cúramóir job. Every refusal names the safe alternative, which is an acceptance
criterion: an agent told only "no" retries the same thing or invents worse.

Scope is the spec's: "during any cúramóir job". The developer's own destructive
commands in their own session are their business.

If `dcg` is installed and `use_dcg_if_present` is true, delegate to it rather
than maintaining a second copy of this list (E0-R5).
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    deny: bool
    reason: str = ""
    alternative: str = ""


# (pattern, what it destroys, what to do instead). Patterns are matched against
# each segment of a shell command, so a chained `&& git reset --hard` is caught.
_RULES = (
    (
        re.compile(r"\bgit\s+reset\s+(--hard|--merge)\b"),
        "git reset --hard discards uncommitted work irrecoverably",
        "git stash",
    ),
    (
        re.compile(r"\bgit\s+checkout\s+--\s"),
        "git checkout -- <file> overwrites the file from HEAD with no backup",
        "git stash push <file>",
    ),
    (
        # --force, but not --force-with-lease, which is the recommended fix.
        re.compile(r"\bgit\s+push\b(?=.*\s--force(?!-with-lease))"),
        "git push --force can overwrite a remote branch's history",
        "git push --force-with-lease",
    ),
    (
        re.compile(r"\bgit\s+clean\s+(-[a-eg-z]*f|--force)"),
        "git clean -f deletes untracked files with no recovery path",
        "git clean -n",
    ),
    (
        re.compile(r"\bgit\s+branch\s+(-D|--delete\s+--force|-[a-zA-Z]*D)\b"),
        "git branch -D deletes a branch even if its work is unmerged",
        "git branch -d",
    ),
    (
        re.compile(r"\bgit\s+stash\s+(drop|clear)\b"),
        "git stash drop/clear destroys stashed work permanently",
        "git stash list",
    ),
    (
        re.compile(r"\brm\s+(-[a-qs-zA-Z]*r[a-eg-zA-Z]*f|-[a-eg-zA-Z]*f[a-qs-zA-Z]*r|-rf|-fr)\b"),
        "rm -rf deletes recursively with no confirmation and no recovery",
        "move the path aside, or delete named files without -r",
    ),
)

# Shell separators a command may hide behind.
_SEGMENT = re.compile(r"&&|\|\||\||;|\n")


def decide(command, in_job):
    """Whether to block `command`.

    `in_job` is the caller's answer to "are we inside a cúramóir job?" —
    computed by `in_curamoir_job`, passed in so this stays pure and testable.
    """
    if not in_job:
        return Decision(deny=False)

    for segment in _SEGMENT.split(command or ""):
        for pattern, what, alternative in _RULES:
            if pattern.search(segment):
                return Decision(
                    deny=True,
                    reason=(
                        f"blocked by cúramóir: {what}. "
                        f"Use `{alternative}` instead."
                    ),
                    alternative=alternative,
                )
    return Decision(deny=False)


def in_curamoir_job(env, repo=None):
    """Whether we are running inside a cúramóir job.

    Two signals, per DEC-5. The env var is primary: hook processes inherit the
    parent environment, so the runner (E10-R1) exports CURAMOIR_JOB. The
    supervisor lock is the fallback, because CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1
    strips extra variables and we would otherwise stop enforcing silently.
    """
    if env.get("CURAMOIR_JOB"):
        return True
    if repo is not None:
        return (repo / ".curamoir" / "state" / "lock").exists()
    return False
