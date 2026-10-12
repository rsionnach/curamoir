"""E0-R6: the open-PR cap.

"A returning developer must never face a wall of PRs." Lives in the guard layer
so every PR-opening job inherits it rather than only the sweep.

`open_pr_branches` is injected — a callable listing the open PRs — so the cap
is testable without the network, and so a `gh` outage is reported as an
unenforced cap rather than mistaken for either a full queue or an empty one.

**The open-PR listing contract** (one contract, defined here, for every reader:
this cap, the sweep's idempotence decision and `/curamoir:status`; cm-fkd.4
supplies the `gh`-backed callable, `gh pr list --state open --json
headRefName,number,url,body,files,isCrossRepository`, mapped item by item
with idempotence.sweep_pr_listing; `isCrossRepository` MUST be requested,
see "The trust boundary"). The callable takes no arguments and returns:

- any non-string iterable (list, tuple, set, generator, dict_keys, ...) whose
  items are each either a non-empty branch name (str), or a record (a mapping)
  `{"branch": str, "number": int (optional), "url": str (optional), "base":
  full commit SHA or None (optional), "paths": [repo-relative str] or None
  (optional), "cross_repository": bool (optional)}`. A plain name is a
  record with only its branch; `base` and `paths` are what the sweep's
  decision falls back to when the ledger lacks the PR's line
  (curamoir.idempotence). Only `branch` and `cross_repository` are read
  here.
- or `None`, which means **could not list**, exactly like raising. WHY not
  "no open PRs", which this cap used to read it as: an empty listing is `[]`,
  and a callable that returns None has said nothing, so reading it as an empty
  queue would let a broken callable lift the cap silently. Treating it as
  "could not list" makes it Degraded here (the outage policy below, now
  visible instead of silent: no weaker) and a refusal for the sweep.

Anything else (a str, a mapping as the whole listing, an item that is neither
a name nor a record with a non-empty `branch`) is also "could not list": a
listing that is wrong in shape cannot be trusted in its count. A timeout is
the callable's own (cm-fkd.4 bounds its `gh` call and raises); nothing here
can interrupt it.

**The trust boundary** (one rule for every reader, applied here in
open_pr_records): a PR is cúramóir's to count, hold or be stopped by only
if its head branch is in this repository. `cross_repository` is gh's
`isCrossRepository`: `True` (a fork's PR, whose head branch name is the
fork owner's choice, so `curamoir/sweep-...` proves nothing) is DROPPED
from the listing before anyone reads it, so outsiders cannot fill E0-R6's
cap, shadow a sweep PR, or stall the sweep (cúramóir never opens a
cross-repository PR, so such a PR holds none of its fixes). `False` is
trusted. Missing or not a bool is unknown provenance: the PR still counts
here and still stands as open for the sweep, but the sweep never uses its
listed `base` or `paths` (curamoir.idempotence). WHY missing is not
"ignore": a cm-fkd.4 that forgot to request the field would then ignore
cúramóir's own PRs, lift the cap and duplicate them. WHY the repository
and not the author: there is no configured sweep identity, and the author
differs by mode (github-actions[bot], a PAT's user, the developer's own
`gh` login). A same-repository head branch needs push access, which can
already PAUSE cúramóir, so trusting it adds no power; the same holds for
editing a same-repository sweep PR's body marker.

**A deliberate change** (cm-fkd.8): a malformed listing item (e.g. `[1]`)
once failed closed here as a guard_error (`1.startswith` raised outside
the try). It is now "could not list", which this cap reports as Degraded
and allows, the documented outage policy below, applied to every listing
that cannot be read rather than only to a callable that raised.
"""

from collections.abc import Iterable, Mapping

from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal

DEFAULT_PREFIX = "curamoir/"
DEFAULT_MAX_OPEN_PRS = 3

#: How much of a bad listing an error message quotes.
_CLIP = 200


def clipped_repr(value, limit=_CLIP):
    """repr(value), cut to `limit` characters: a message quotes a bad listing
    to say what came back, not to copy a megabyte of it into the ledger."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...(clipped)"


def open_pr_records(listed):
    """The open-PR listing (the contract in the module docstring) as a list of
    records, each a dict with at least a non-empty `branch`; plain names become
    `{"branch": name}`. Records with `cross_repository` exactly True are
    dropped (module docstring, "The trust boundary"), after the whole
    listing is checked: a malformed item anywhere still means "could not
    list". ValueError when it is None or not of that shape: the open PRs
    could not be listed."""
    if listed is None:
        raise ValueError("the listing callable returned None")
    if isinstance(listed, (str, bytes, Mapping)) or not isinstance(listed, Iterable):
        raise ValueError(f"the open pull requests were listed as {clipped_repr(listed)}, "
                         "not an iterable of branch names or PR records")
    records = []
    for item in listed:  # a generator is consumed here, once
        record = {"branch": item} if isinstance(item, str) else item
        branch = record.get("branch") if isinstance(record, Mapping) else None
        if not (isinstance(branch, str) and branch):
            raise ValueError(f"an open pull request was listed as {clipped_repr(item)}, "
                             "not a branch name or a record with a branch")
        records.append(dict(record))
    # WHY `is True` and not truthiness: only gh's own boolean says "a fork";
    # anything else is unknown provenance, which stays (it may be ours).
    return [record for record in records if record.get("cross_repository") is not True]


def open_pr_branch_names(listed):
    """The branch names in an open-PR listing, cross-repository PRs dropped
    (open_pr_records' ValueError when it could not be listed)."""
    return [record["branch"] for record in open_pr_records(listed)]


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
            branches = open_pr_branch_names(self._open_pr_branches())
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
