"""The chokepoint every cúramóir side effect passes through."""

from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal


def guard_name(guard):
    """A name for a guard, for refusal messages and the ledger."""
    return getattr(guard, "__name__", guard.__class__.__name__)


class Gateway:
    """Runs side effects, subject to the guards it was built with.

    The gateway owns execution: callers hand it the action rather than running
    the action themselves and checking a return value. That is what makes the
    guarded path the only path, and it is why E0-R1's "enforced in code, not
    prompt" is testable without a model in the loop.
    """

    def __init__(self, guards, ledger=None):
        self._guards = list(guards)
        self._ledger = ledger

    def perform(self, context, action):
        """Run `action` for `context`, unless a guard refuses."""
        for guard in self._guards:
            try:
                refusal = guard(context)
            except Exception as error:
                # Fail closed. A guard that cannot reach a verdict leaves us
                # unable to say the side effect is safe, and for an unattended
                # agent that resolves to "no". Recorded, because an unreported
                # guard failure is a silent loss of enforcement (DEC-5).
                refusal = Refusal(
                    reason=Reason.GUARD_ERROR,
                    message=f"{guard_name(guard)} failed: {error}",
                )
            if isinstance(refusal, Degraded):
                # Allow, but never silently: a guardrail that could not run is
                # a gap in protection the brief has to be able to report.
                self._record(context, refusal, severity="degraded")
                continue
            if refusal is not None:
                self._record(context, refusal, severity="refused")
                return refusal
        return action()

    def _record(self, context, outcome, severity):
        if self._ledger is None:
            return
        self._ledger.record(
            kind="guard",
            job=context.job,
            reason=outcome.reason,
            message=outcome.message,
            severity=severity,
        )
