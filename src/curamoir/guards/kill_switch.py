"""E0-R3: the kill switch.

Presence of `.curamoir/PAUSE`, or a `curamoir:paused` label/topic on the
repository, stops every scheduled job immediately with no side effects.

This is the first check in `curamoir run <job>` (E10-R1), before the lock is
acquired, so a paused repo never takes a lock it will not use.
"""

from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal

PAUSE_FILE = "PAUSE"
PAUSED_LABEL = "curamoir:paused"


class KillSwitch:
    """Refuses everything while the repo is paused.

    `repo_labels` is an optional callable returning the repository's labels or
    topics. It is injected rather than reached for directly so this guard stays
    testable without the network — and so a `gh` outage cannot be mistaken for
    a pause.
    """

    def __init__(self, repo_labels=None):
        self._repo_labels = repo_labels

    def __call__(self, context):
        if context.repo is not None:
            pause_file = context.repo / ".curamoir" / PAUSE_FILE
            if pause_file.exists():
                return Refusal(
                    reason=Reason.PAUSED,
                    message=(
                        f"paused: {pause_file.relative_to(context.repo)} is present. "
                        "Delete it to resume scheduled runs."
                    ),
                )

        if self._repo_labels is not None:
            try:
                labels = list(self._repo_labels() or [])
            except Exception as error:
                # Not a pause: a gh outage must not halt a month of scheduled
                # work. Not silence either — the check did not run, and that is
                # a gap the brief must be able to report.
                return Degraded(
                    reason=Reason.PAUSED,
                    message=(
                        f"could not read repository labels ({error}); "
                        f"the {PAUSED_LABEL!r} kill switch was NOT checked. "
                        "The local PAUSE file was checked and is absent."
                    ),
                )
        else:
            labels = []

        if PAUSED_LABEL in labels:
            return Refusal(
                reason=Reason.PAUSED,
                message=(
                    f"paused: the repository carries the {PAUSED_LABEL!r} label. "
                    "Remove the label to resume scheduled runs."
                ),
            )

        return None
