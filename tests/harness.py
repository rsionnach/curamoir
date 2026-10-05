"""The guardrail harness.

Every Epic 0 acceptance criterion reads "a test harness demonstrates that...",
so this is a deliverable of E0-R0 and is shared by every guardrail's tests.

Its defining property: **it never invokes a model.** Guards are called as plain
callables against a real throwaway git repo and a real ledger file. A guardrail
that is only prompt text has no callable to register, and one that allows
everything fails `assert_refused`. That is how E0-R0's second acceptance
criterion — "a guardrail that is bypassed by a prompt rather than code fails the
harness" — is enforced mechanically rather than by reviewer vigilance.

Test-only code. It lives under tests/ and is never imported by production.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from curamoir.guards import Gateway, JobContext
from curamoir.ledger import Ledger


@dataclass
class GuardRun:
    """The observable result of running one guard in isolation."""

    refusal: object
    ledger: list
    performed: bool

    def assert_refused(self, reason):
        if self.refusal is None:
            raise AssertionError(
                f"guard did not refuse; expected reason {reason!r}. "
                "A guard that allows everything enforces nothing."
            )
        if self.refusal.reason is not reason:
            raise AssertionError(
                f"guard refused with {self.refusal.reason!r}, expected {reason!r}"
            )
        recorded = [entry["reason"] for entry in self.ledger]
        if str(reason) not in recorded:
            raise AssertionError(
                f"guard refused with {reason!r} but the ledger recorded {recorded!r}. "
                "An unrecorded refusal is invisible to the brief and the dogfood log."
            )

    def assert_allowed(self):
        if self.refusal is not None:
            raise AssertionError(f"guard unexpectedly refused: {self.refusal!r}")


class Harness:
    """A throwaway git repo plus a ledger, for exercising one guard at a time."""

    def __init__(self, root):
        self.repo = Path(root)
        self.repo.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self.repo / ".curamoir" / "state" / "ledger.jsonl"
        self._git("init", "--initial-branch=main")
        self._git("config", "user.email", "harness@example.invalid")
        self._git("config", "user.name", "curamoir harness")
        self.write("README.md", "throwaway fixture repo\n")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial commit")

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write(self, relative_path, content):
        """Create a file in the throwaway repo, making parents as needed."""
        target = self.repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def current_branch(self):
        return self._git("rev-parse", "--abbrev-ref", "HEAD")

    def checkout(self, branch):
        """Create and switch to a branch in the throwaway repo."""
        self._git("checkout", "-b", branch)
        return branch

    def rename_branch(self, new_name):
        """Rename the current branch, for repos whose default is not `main`."""
        self._git("branch", "-m", new_name)
        return new_name

    def local_branches(self):
        return self._git("for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def ledger(self):
        return Ledger(self._ledger_path)

    def run(self, guard, job="sweep"):
        """Run one guard against the throwaway repo. No model is involved."""
        performed = []
        ledger = self.ledger()
        gateway = Gateway(guards=[guard], ledger=ledger)
        context = JobContext(job=job, repo=self.repo)

        outcome = gateway.perform(context, lambda: performed.append(job))

        refused = outcome is not None and hasattr(outcome, "reason")
        return GuardRun(
            refusal=outcome if refused else None,
            ledger=ledger.entries(),
            performed=bool(performed),
        )
