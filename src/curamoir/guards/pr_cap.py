"""E0-R6: the open-PR cap.

"A returning developer must never face a wall of PRs." Lives in the guard layer
so every PR-opening job inherits it rather than only the sweep.

`open_pr_branches` is injected — a callable returning the branch names of open
PRs — so the cap is testable without the network, and so a `gh` outage is
reported as an unenforced cap rather than mistaken for either a full queue or
an empty one.
"""

from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal

DEFAULT_PREFIX = "curamoir/"
DEFAULT_MAX_OPEN_PRS = 3


class OpenPrCap:
    """Refuses to open another PR once the cap is reached."""

    def __init__(
        self,
        open_pr_branches,
        max_open_prs=DEFAULT_MAX_OPEN_PRS,
        branch_prefix=DEFAULT_PREFIX,
    ):
        self._open_pr_branches = open_pr_branches
        self._max = max_open_prs
        self._prefix = branch_prefix

    def __call__(self, _context):
        try:
            branches = list(self._open_pr_branches() or [])
        except Exception as error:
            # Allow, but say the cap was not enforced. Refusing would let a gh
            # outage stall a month of scheduled work; staying silent would hide
            # the fact that the wall-of-PRs protection was absent.
            return Degraded(
                reason=Reason.PR_CAP,
                message=(
                    f"could not list open pull requests ({error}); "
                    "the open-PR cap was NOT enforced for this run."
                ),
            )

        # The developer's own open PRs are not cúramóir's to ration.
        ours = [name for name in branches if name.startswith(self._prefix)]
        if len(ours) < self._max:
            return None

        return Refusal(
            reason=Reason.PR_CAP,
            message=(
                f"{len(ours)} open {self._prefix}* pull request(s) already, at the "
                f"cap of {self._max}. Review or close one before cúramóir opens "
                "another: " + ", ".join(sorted(ours))
            ),
        )
