"""E0-R3: the kill switch.

Presence of `.curamoir/PAUSE`, or a `curamoir:paused` label on the
repository, stops every scheduled job immediately with no side effects.

This is the first check in `curamoir run <job>` (E10-R1), before the lock is
acquired, so a paused repo never takes a lock it will not use.
"""

from collections.abc import Mapping

from curamoir.guards.context import unusable_repo
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal

PAUSE_FILE = "PAUSE"
PAUSED_LABEL = "curamoir:paused"


class KillSwitch:
    """Refuses everything while the repo is paused.

    `repo_labels` is an optional callable returning the repository's labels
    as an iterable of label-name strings (not gh's `{name: ...}` objects,
    nor `gh repo view`'s `{labels: [...]}`). It is injected rather than
    reached for directly so this guard stays testable without the network —
    and so a `gh` outage cannot be mistaken for a pause. It must time out
    and raise rather than hang. (Labels only: a GitHub topic cannot contain
    ':', so `curamoir:paused` can never be one.)

    A callable that raises, or returns anything other than an iterable of
    str that is not itself a str, bytes or mapping, degrades the check: the
    job runs and a degraded line says the label was not checked. Names are
    compared stripped and casefolded, because GitHub treats label names
    case-insensitively.
    """

    def __init__(self, repo_labels=None):
        self._repo_labels = repo_labels

    def __call__(self, context):
        unusable = unusable_repo(context)
        if unusable is not None:
            return Degraded(
                reason=Reason.PAUSED,
                message=(
                    f"{unusable}, so the PAUSE check did NOT run. "
                    "Resolve the repository root before attempting the job."
                ),
            )
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
                labels = _label_names(self._repo_labels())
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

        if PAUSED_LABEL.casefold() in labels:
            return Refusal(
                reason=Reason.PAUSED,
                message=(
                    f"paused: the repository carries the {PAUSED_LABEL!r} label. "
                    "Remove the label to resume scheduled runs."
                ),
            )

        return None


def _label_names(returned):
    """The casefolded label names `returned` holds, or a raise if it is not
    the iterable of str that `repo_labels` promises.

    Strict on purpose. Each wrong shape fails OPEN: gh's `{name: ...}`
    objects never equal the label name, a bare string iterates as its
    characters, and either way a paused repository would run as if it were
    not, with nothing recorded. Raising instead puts the wrong shape
    through the same degraded path as an outage, so the brief can say the
    label was not checked. `None` is held to the same rule: a callable that
    found no labels returns an empty list, and one that forgot to return
    has not said so. A mapping is refused for the same reason as a string:
    a dict iterates as its str keys, so gh's `{"name": ...}` would pass as
    the one label "name", and a paused repository would run.
    """
    if returned is None:
        raise TypeError("repo_labels returned None, not an iterable of label names")
    if isinstance(returned, (str, bytes, Mapping)):
        raise TypeError(
            f"repo_labels returned {type(returned).__name__} {returned!r}, not "
            "an iterable of label names"
        )
    names = list(returned)
    for name in names:
        if not isinstance(name, str):
            raise TypeError(
                f"repo_labels returned the label {name!r} "
                f"({type(name).__name__}), not a label-name string"
            )
    # Stripped as well as casefolded: a label padded by whatever produced it
    # is still the label a maintainer added to pause the repository.
    return {name.strip().casefold() for name in names}
