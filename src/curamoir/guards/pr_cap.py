"""E0-R6: the open-PR cap.

"A returning developer must never face a wall of PRs." Lives in the guard layer
so every PR-opening job inherits it rather than only the sweep.

`open_pr_branches` is injected — a callable listing the open PRs — so the cap
is testable without the network, and so a `gh` outage is reported as an
unenforced cap rather than mistaken for either a full queue or an empty one.

**The open-PR listing contract** (one contract, defined here, for every reader:
this cap, the sweep's idempotence decision and `/curamoir:status`; cm-fkd.4
supplies the `gh`-backed callable, `gh pr list --state open --json
headRefName,number,url,body,files`, mapped item by item with
idempotence.sweep_pr_listing). The callable takes no arguments and returns:

- any non-string iterable (list, tuple, set, generator, dict_keys, ...) whose
  items are each either a non-empty branch name (str), or a record (a mapping)
  `{"branch": str, "number": int (optional), "url": str (optional), "base":
  full commit SHA or None (optional), "paths": [repo-relative str] or None
  (optional)}`. A plain name is a record with only its branch; `base` and
  `paths` are what the sweep's decision falls back to when the ledger lacks
  the PR's line (curamoir.idempotence). Only `branch` is read here.
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
    `{"branch": name}`. ValueError when it is None or not of that shape: the
    open PRs could not be listed."""
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
    return records


def open_pr_branch_names(listed):
    """The branch names in an open-PR listing (open_pr_records' ValueError
    when it could not be listed)."""
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
