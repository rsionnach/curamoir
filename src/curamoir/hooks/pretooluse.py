"""The PreToolUse hook entry point.

Reads the hook payload on stdin, decides, and writes a deny decision to stdout.
Invoked by Claude Code; see hooks/hooks.json for the registration.

Two properties matter more than the logic:

- **It never raises.** A hook that crashes adds noise to every tool call and,
  because command hooks fail OPEN on error or timeout (DEC-5), protects nothing
  by dying. It exits 0 and lets the normal permission flow apply.
- **It is fast.** It runs on every Bash call during a job. No imports beyond the
  standard library, no network, no model.

Judges commands with the two-pass policy of DEC-9 (see policy.py): an allowlist
of what cúramóir's jobs legitimately do, then the destructive rules as a second
pass over the same string.

Delegates to `dcg` when that is installed and not switched off, rather than
keeping a second copy of the denylist (E0-R5).

NOTE ON THE CONTROL SURFACE: E0-R5 names the config key
`guardrails.use_dcg_if_present`, but this hook cannot read `curamoir.yaml` —
it is deliberately stdlib-only, because PyYAML is not guaranteed present for an
installed plugin, and it runs on every Bash call so it must not pay a config
parse. The switch is therefore the environment variable `CURAMOIR_USE_DCG`
(default on), which the runner sets from the config key. Documented here because
a docstring that quotes a key the code never reads is worse than no docstring.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from curamoir.hooks._entry import in_curamoir_job
from curamoir.hooks.policy import judge_command
from curamoir.hooks.verdict import Category, Decision


def main(argv=None, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.load(stdin)
    except (ValueError, OSError):
        # Unparseable input is not our problem to escalate.
        return 0

    try:
        if payload.get("tool_name") != "Bash":
            return 0

        command = (payload.get("tool_input") or {}).get("command") or ""
        repo = Path(payload.get("cwd") or ".")

        signal = in_curamoir_job(os.environ, repo)
        if not signal:
            return 0

        delegated = _delegate_to_dcg(command)
        decision = delegated if delegated is not None else judge_command(
            command, in_job=True, scripts=_package_scripts(repo))

        if decision.deny:
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": refusal_text(decision, signal),
                    }
                },
                stdout,
            )
        return 0
    except Exception:
        # Same reasoning as above: never let a guard failure become a crash on
        # the developer's tool call.
        return 0


# Exit statuses that mean `dcg` never reached a verdict on the command, rather
# than that it refused one.
#
# 126 and 127 are the shell's "could not execute". 2 and 64 (sysexits' EX_USAGE)
# are a tool that RAN and rejected its own arguments — a `dcg` a version out of
# step with the subcommand we pass, or an unrelated binary of that name on PATH.
# Read as a refusal, that denied `git status`, `git add -A` and `pytest -q`
# alike with the message "blocked by dcg": a guard that blocks all work, which
# gets switched off.
#
# Being liberal here is the safe direction, and that asymmetry is the reason to
# prefer it. Falling back never weakens the guard — cúramóir's own tables still
# judge the command, so a missed `dcg` verdict costs only dcg's extra coverage.
# A wrong refusal costs every command in the job.
_COULD_NOT_RUN = (2, 64, 126, 127)


#: The largest package.json read for its script names. A bigger one is not
#: read (its scripts are then unknown, and `yarn run <name>` is judged
#: strictly): the hook has 5s, and a command hook that times out fails open.
MAX_PACKAGE_JSON_BYTES = 1_000_000


def _package_scripts(repo):
    """The script names in `repo`'s package.json, or None when there is no
    readable one (policy.judge_command's `scripts`)."""
    path = repo / "package.json"
    try:
        if path.stat().st_size > MAX_PACKAGE_JSON_BYTES:
            return None
        scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(scripts, dict):
        return None
    return frozenset(name for name in scripts if isinstance(name, str))


def refusal_text(decision, signal=None):
    """What the agent is told: the reason, and what to do instead.

    E0-R5 makes naming the safe alternative an acceptance criterion, because an
    agent told only "no" retries the same thing or invents worse. The hook used
    to emit `reason` alone, and `Decision.alternative` had no reader anywhere —
    so the three refusals that keep their alternative OUT of the reason (the
    length cap, unparseable quoting, an unterminated heredoc) arrived with
    nothing to act on. A structured field with no consumer reads as "this gets
    shown", which is why it is joined here rather than dropped.

    The last sentence says which signal made this a job (`signal`, from
    in_curamoir_job): a denial in the developer's own session, from a lock a
    killed run left behind, is otherwise a mystery (cm-azr).
    """
    reason = decision.reason or "blocked by cúramóir"
    if decision.alternative and decision.alternative not in reason:
        reason = f"{reason} Try instead: {decision.alternative}."
    if signal is None:
        return reason
    if signal.job is not None:
        return f"{reason} Denied during cúramóir job {signal.job!r}."
    return (f"{reason} Denied because .curamoir/state/lock exists: a cúramóir job is "
            "running in this repository, or a killed one left its lock behind. See "
            "/curamoir:status.")


def _delegate_to_dcg(command):
    """`dcg`'s verdict, or None to fall back to our own list.

    E0-R5: "If `dcg` is installed and `use_dcg_if_present` is true, delegate to
    it instead of duplicating the list." The switch reaching this hook is
    CURAMOIR_USE_DCG, not the config key directly — see the module docstring.

    Delegation never WEAKENS the guard: a `dcg` that allows returns None, so
    cúramóir's own tables still judge the command. `dcg` exists so we do not
    maintain a second copy of the list, not so a permissive one can unlock
    commands cúramóir refuses on its own account.
    """
    if os.environ.get("CURAMOIR_USE_DCG", "1") != "1":
        return None
    if shutil.which("dcg") is None:
        return None
    try:
        result = subprocess.run(
            ["dcg", "check", command],
            capture_output=True,
            text=True,
            timeout=3,
            # A `dcg` that reads stdin would otherwise block the hook until the
            # timeout, and a command hook that times out fails OPEN.
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    verdict = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0 or result.returncode in _COULD_NOT_RUN or not verdict:
        # Allowed, or no verdict reached, or a refusal with nothing to quote —
        # and E0-R5 requires every refusal to name what to do instead, so a
        # silent one cannot be relayed. Fall back to our own tables.
        return None
    return Decision(
        deny=True,
        reason=verdict,
        alternative="see dcg output",
        category=Category.DESTRUCTIVE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
