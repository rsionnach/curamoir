"""E0-R5's external oracle, which 137 denylist cases never consulted.

The spec (docs/curamoir-spec.md:202) says: "If `dcg` is installed and
`use_dcg_if_present` is true, delegate to it instead of duplicating the list."

`_delegate_to_dcg` exists, but every case in test_denylist_forms.py calls
`judge_destructive()` directly and every case in test_command_policy.py calls
`judge_command()` directly — so the one source that could independently
adjudicate "what is a destructive git command", rather than this author's
opinion of it, was never asked. Fixture provenance review's finding 8.

These tests stub a `dcg` on PATH and assert the delegation actually happens and
its verdict is honoured.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_hook_with_dcg(command, dcg_script, extra_env=None):
    """Run the real hook with a stubbed `dcg` first on PATH."""
    import tempfile

    bindir = tempfile.mkdtemp(prefix="dcg-stub-")
    stub = Path(bindir) / "dcg"
    stub.write_text(dcg_script, encoding="utf-8")
    stub.chmod(0o755)

    environment = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "CURAMOIR_JOB": "sweep",
    }
    environment.update(extra_env or {})

    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": command}, "cwd": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-m", "curamoir.hooks.pretooluse"],
        input=json.dumps(payload), capture_output=True, text=True, env=environment,
    )
    decision = (
        json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None
    )
    import shutil
    shutil.rmtree(bindir, ignore_errors=True)
    return result, decision


DENIES = "#!/usr/bin/env bash\necho 'dcg says: this would destroy work' >&2\nexit 3\n"
ALLOWS = "#!/usr/bin/env bash\nexit 0\n"


def test_dcg_is_consulted_when_present():
    """A command cúramóir's own tables would ALLOW, which dcg refuses. If the
    refusal comes back, delegation happened."""
    result, decision = run_hook_with_dcg("git status", DENIES)

    assert result.returncode == 0, result.stderr
    assert decision is not None, "dcg was never consulted"
    assert decision["permissionDecision"] == "deny"


def test_dcgs_own_reason_reaches_the_agent():
    """Delegating is pointless if the verdict arrives without its explanation."""
    _, decision = run_hook_with_dcg("git status", DENIES)

    assert "dcg says" in decision["permissionDecisionReason"]


def test_a_dcg_that_allows_does_not_override_curamoirs_own_refusal():
    """Delegation must not WEAKEN the guard. `dcg` exists so we do not maintain
    a second copy of the list — not so a permissive dcg can unlock commands
    cúramóir refuses on its own account."""
    _, decision = run_hook_with_dcg("rm -rf /", ALLOWS)

    assert decision is not None, "a permissive dcg disabled cúramóir's own judgement"
    assert decision["permissionDecision"] == "deny"


def test_delegation_can_be_switched_off():
    """`guardrails.use_dcg_if_present` reaches the hook as CURAMOIR_USE_DCG."""
    _, decision = run_hook_with_dcg("git status", DENIES, extra_env={"CURAMOIR_USE_DCG": "0"})

    assert decision is None, "dcg was consulted despite being switched off"


def test_a_dcg_that_cannot_RUN_falls_back_instead_of_blocking_everything():
    """Exit 126/127 means the shell could not execute it — not that it reached a
    verdict. Treating any non-zero exit as a refusal meant a mis-installed dcg
    denied `git status`, `git add -A` and `pytest -q` alike, with the message
    "blocked by dcg". A guard that blocks all work gets switched off, which costs
    the same as a false negative.

    My own first version of this test asserted only that `git reset --hard` was
    denied under a crashing dcg — which passed for the wrong reason, because
    EVERYTHING was denied."""
    crashes = "#!/usr/bin/env bash\nexit 127\n"

    _, harmless = run_hook_with_dcg("git status", crashes)
    assert harmless is None, "a dcg that could not run blocked a harmless command"

    _, destructive = run_hook_with_dcg("git reset --hard", crashes)
    assert destructive is not None, "fell back but then failed to refuse"
    assert "stash" in destructive["permissionDecisionReason"], (
        "fell back to cúramóir's tables but lost the safe alternative"
    )


def test_a_dcg_that_is_not_executable_also_falls_back():
    """The other shape of the same failure: present on PATH but not runnable."""
    import os
    import shutil
    import tempfile

    bindir = tempfile.mkdtemp(prefix="dcg-stub-")
    stub = Path(bindir) / "dcg"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stub.chmod(0o000)
    try:
        environment = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "CURAMOIR_JOB": "sweep",
        }
        payload = {
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": "git status"}, "cwd": str(ROOT),
        }
        result = subprocess.run(
            [sys.executable, "-m", "curamoir.hooks.pretooluse"],
            input=json.dumps(payload), capture_output=True, text=True, env=environment,
        )
    finally:
        stub.chmod(0o700)
        shutil.rmtree(bindir, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "an unrunnable dcg blocked a harmless command"
