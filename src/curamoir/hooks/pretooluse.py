"""The PreToolUse hook entry point.

Reads the hook payload on stdin, decides, and writes a deny decision to stdout.
Invoked by Claude Code; see hooks/hooks.json for the registration.

Two properties matter more than the logic:

- **It never raises.** A hook that crashes adds noise to every tool call and,
  because command hooks fail OPEN on error or timeout (DEC-5), protects nothing
  by dying. It exits 0 and lets the normal permission flow apply.
- **It is fast.** It runs on every Bash call during a job. No imports beyond the
  standard library, no network, no model.

Delegates to `dcg` when that is installed and configured, rather than keeping a
second copy of the denylist (E0-R5).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from curamoir.hooks.denylist import decide, in_curamoir_job


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

        if not in_curamoir_job(os.environ, repo):
            return 0

        delegated = _delegate_to_dcg(command)
        decision = delegated if delegated is not None else decide(command, in_job=True)

        if decision.deny:
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": decision.reason,
                    }
                },
                stdout,
            )
        return 0
    except Exception:
        # Same reasoning as above: never let a guard failure become a crash on
        # the developer's tool call.
        return 0


def _delegate_to_dcg(command):
    """`dcg`'s verdict, or None to fall back to our own list.

    E0-R5: "If `dcg` is installed and `use_dcg_if_present` is true, delegate to
    it instead of duplicating the list."
    """
    if os.environ.get("CURAMOIR_USE_DCG", "1") != "1":
        return None
    if shutil.which("dcg") is None:
        return None
    try:
        result = subprocess.run(
            ["dcg", "check", command], capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return None
    from curamoir.hooks.denylist import Decision

    return Decision(
        deny=True,
        reason=(result.stdout or result.stderr or "blocked by dcg").strip(),
        alternative="see dcg output",
    )


if __name__ == "__main__":
    raise SystemExit(main())
