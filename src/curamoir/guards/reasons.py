"""Machine-readable refusal reasons.

One vocabulary, three readers: the guard layer raises them, the ledger records
them (E0-R9), and the away-brief groups by them (E9-R2). Changing a string here
changes all three, so they are defined once and only here.

A reason names the CHECK, not the outcome. `Degraded` reuses the same reason as
a refusal — `Degraded(Reason.PAUSED, ...)` means "the PAUSE check could not
run", not "this repo is paused" — so a consumer that groups by reason alone
reports a repo as paused when it never was. The disambiguator is `severity`,
written alongside the reason by `Gateway._record_line`, and E9-R2 must read
both.
"""

from enum import Enum


class Reason(str, Enum):
    # Guardrail refusals (spec §7)
    PAUSED = "paused"                    # E0-R3: .curamoir/PAUSE or curamoir:paused label
    LOCKED = "locked"                    # E0-R2: a live supervisor lock exists
    BUDGET = "budget"                    # E0-R4: monthly or per-run ceiling would be breached
    PR_CAP = "pr_cap"                    # E0-R6: too many open curamoir/ PRs
    DEFAULT_BRANCH = "default_branch"    # E0-R1: attempted write to the default branch
    DENYLIST = "denylist"                # E0-R5: destructive command blocked

    # Sweep refusals that are not guardrails. Raised by the runner, not the
    # gateway, so they have no Degraded form.
    IDEMPOTENCE = "idempotence"          # E3-R7: open sweep PRs unknown, so a second PR
                                         # might duplicate one (curamoir.idempotence)

    # Guard-layer failures. An unrecorded guard failure is a silent loss of
    # enforcement, so both are ledgered (DEC-5).
    GUARD_ERROR = "guard_error"      # produced here: a guard raised (gateway fails closed);
                                     # and by job_guard: a curamoir.yaml that cannot be loaded
    GUARD_TIMEOUT = "guard_timeout"  # NOT YET PRODUCED: owned by E0-R5 / E4-R4b, where a
                                     # PreToolUse hook can time out. Claude Code hooks fail
                                     # OPEN on timeout, unlike this gateway, which fails
                                     # closed — that asymmetry is the point of recording it.

    def __str__(self):
        return self.value
