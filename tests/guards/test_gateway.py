"""The gateway is the single chokepoint every cúramóir side effect passes through.

These tests never invoke a model. That is deliberate, and it is what enforces
E0-R0's second acceptance criterion: a guardrail implemented as prompt text
rather than code cannot refuse anything here, so it fails the harness.
"""

from curamoir.guards import Gateway, JobContext, Reason, Refusal


def test_side_effect_runs_when_no_guard_objects():
    performed = []
    gateway = Gateway(guards=[])

    gateway.perform(JobContext(job="sweep", repo=None), lambda: performed.append("wrote to the repo"))

    assert performed == ["wrote to the repo"]


def refuse_with(reason, message):
    """A guard that always refuses. Guards are callables taking the job context."""
    return lambda _context: Refusal(reason=reason, message=message)


def test_refusing_guard_prevents_the_side_effect_from_running():
    performed = []
    gateway = Gateway(guards=[refuse_with(Reason.PAUSED, ".curamoir/PAUSE is present")])

    gateway.perform(JobContext(job="sweep", repo=None), lambda: performed.append("wrote to the repo"))

    assert performed == []


def test_refusal_reaches_the_caller_with_its_reason_and_message():
    gateway = Gateway(guards=[refuse_with(Reason.PR_CAP, "3 open curamoir/ PRs already")])

    outcome = gateway.perform(JobContext(job="sweep", repo=None), lambda: "should not happen")

    assert outcome.reason is Reason.PR_CAP
    assert outcome.message == "3 open curamoir/ PRs already"


def test_first_refusing_guard_wins_and_later_guards_are_not_consulted():
    consulted = []

    def record(name, refusal):
        def guard(_context):
            consulted.append(name)
            return refusal
        return guard

    gateway = Gateway(guards=[
        record("pause", Refusal(Reason.PAUSED, "paused")),
        record("budget", Refusal(Reason.BUDGET, "over ceiling")),
    ])

    outcome = gateway.perform(JobContext(job="sweep", repo=None), lambda: None)

    assert consulted == ["pause"]
    assert outcome.reason is Reason.PAUSED


def test_guard_that_raises_refuses_the_side_effect_rather_than_letting_it_through():
    """The gateway fails CLOSED.

    DEC-5 established that a timed-out Claude Code hook fails open — that is the
    harness's behaviour and outside our control. The gateway is ours, and here a
    guard that cannot reach a verdict means we do not know whether the side
    effect is safe. For an agent nobody is watching, that resolves to "no".
    """
    performed = []

    def broken_guard(_context):
        raise RuntimeError("could not read the ledger")

    gateway = Gateway(guards=[broken_guard])

    outcome = gateway.perform(JobContext(job="sweep", repo=None), lambda: performed.append("wrote"))

    assert performed == []
    assert outcome.reason is Reason.GUARD_ERROR
    assert "could not read the ledger" in outcome.message
