"""E3-R7: two consecutive sweeps with no intervening changes MUST produce
no second PR.

E3-R7's two cases: nothing to fix (the line says `no_work`), and the
previous sweep's PR still open and unmerged containing the same fixes (do
not duplicate it). This is what makes the weekly cadence safe to leave
running for a month of absence. The module is the decision, made once per
sweep after the base is linted and before anything edits the sandbox
(fix_scope's "What the runner (cm-fkd.4) must do", step 2b), and pure apart
from the one call it makes to list open PRs.

**The convention for the ledger** (decided here, read by the brief,
cm-fkd.9, and by whoever counts sweeps). A sweep that had nothing to do is
not refused (no guard stopped it) and did not fail: it is a `success` line
whose `work` field says what kind of success. `work` is on every sweep line
that got as far as this decision, with one of three values:

- WORK (`"work"`): there was work, and the line goes on to say what came
  of it (a `pr` if one was opened, `test_gate`, `fixes`).
- NO_WORK (`"no_work"`): the base's work list was empty.
- DUPLICATE_OPEN_PR (`"duplicate_open_pr"`): everything there was to do is
  already in an open sweep PR; `open_sweep_prs` names it.

WHY a field and not the summary text: the summary is for people and
changes wording; the brief should not parse prose. WHY not a refusal: the
brief groups refusals as "something stopped me", and a sweep with nothing
to do stopped nobody. The one refusal here is UNKNOWN (Reason.IDEMPOTENCE:
the open PRs cannot be listed, or an open sweep PR's files are known to
neither the ledger nor GitHub): then the check could not run, and that is
a refusal in the ordinary sense. The open-PR cap and the budget (E3-R6,
cm-fkd.7) are the guard layer's refusals (`pr_cap`, `budget`) and do not
touch `work`. Every field and which line carries it: fix_counts' module
docstring, "The ledger line".

**The decision**, in order:

1. The base's work list is empty and no tier-1 scan will run (`tier1`,
   Limits): NO_WORK. The open PRs are not asked for;
   they cannot change the answer.
2. The open PRs cannot be listed (the callable raises, returns None, or
   returns something outside the listing contract, guards.pr_cap's
   docstring): UNKNOWN, refuse; the message says the next sweep will try
   again, because a `gh` outage passes.
3. Each open sweep PR (branch under SWEEP_BRANCH_PREFIX, in this
   repository: a fork's PR was already dropped, "The trust boundary"
   below) is known by its ledger `pr` record when there is one (primary:
   it is what the sweep that opened it wrote), paired by PR NUMBER, else
   by the listing's `base` and `paths` when both are present and valid and
   the PR is known to be same-repository (GitHub's own account, "Open PRs
   on GitHub" below).
4. An open sweep PR was made from this very base, by whichever base is
   known (ledger, else listing; the base alone is enough here, no paths
   needed): DUPLICATE_OPEN_PR, whatever it left out. WHY even for leftovers
   (a file past `max_files_per_pr`, a fix the test gate dropped, or a rule
   changed at the same commit): E3-R7's "no intervening changes" is judged
   by the base commit, and the leftovers are E3-R3's "rest for the next
   run", which is the next base.
5. An open sweep PR neither source knows the files of: UNKNOWN, refuse. No
   file is known to be free of it. The message names the branch(es) and
   says this clears when the PR is merged or closed: it does not pass by
   itself, so "try again" would be false.
6. Otherwise every violation in a file that an open sweep PR changes is
   held back (`held`). Nothing left: DUPLICATE_OPEN_PR. Something left:
   WORK, with the work list narrowed to it (`Decision.work_list`), which
   the runner installs on the sandbox (Decision.apply_to) before the
   remedies.

With `tier1=True` (the tier-1 scan will run at this base), step 1 does not
apply and steps 4 and 6 never end the sweep on the lint's account alone:
the decision is WORK, and Decision.after_tier1 settles it once the scan
has run (TIER1_FAILED, DUPLICATE_OPEN_PR or NO_WORK, in that order).

**What "the same fixes" is.** By file, not by violation: a violation is
held when its path is in an open sweep PR's `pr.paths` (the gate's
`paths_for_pr`: every file the PR changes). WHY by file: two open PRs that
change one file conflict when the second is merged, so the second must
stay out of the first's files altogether, and holding a file back is
something the scope check enforces in code: a narrowed work list does not
list the file, so an edit to it is `file_not_in_work_list` (fix_scope),
whatever the prompt said. By violation would have needed the agent to
leave a fixed-elsewhere violation alone in a file it was editing, which
only the prompt could ask. It also keeps the fix records exact:
fix_counts.record_fixes nets vanished against appeared fingerprints per
(rule, file), and a file held whole never splits one of those groups. It
subsumes "the same fingerprints": every fix the PR records is in one of
its paths. WHY a PR at an older base still holds its files: a lint
fingerprint does not depend on line numbers (docs/curamoir-lint.md,
"Fingerprint"), so an unchanged file's violations are the same ones at
the newer base, and the PR still fixes them.

**Merged or closed.** The open-PR list is the same source of truth as the
open-PR cap (guards.pr_cap.OpenPrCap's callable, one contract for both:
OPEN PRs only), so a merged PR and a closed one look alike: not open, and
their files are free. Merged: the base holds the fixes, the lint no longer
reports them, and what it still reports is new. Closed unmerged: the fixes
were never applied, and are proposed again (see Limits).

**Why UNKNOWN refuses** although the open-PR cap degrades (allows) on the
same failure: the cap guards every job and must not stall a month of
unrelated work over a `gh` outage. This check guards one thing, a
duplicate PR, which is the harm E3-R7 names, and a sweep that cannot list
PRs cannot open one either (cm-fkd.4 opens it through the same `gh`), so
refusing before the fix step costs only the spend it saves.

**Where it reads from.** The previous sweeps' `pr` records come from the
ledger (sweep_prs): per branch, the latest `pr` on any sweep job line,
whatever its outcome (a runner that opened the PR and then crashed still
opened it). Latest means ledger order (the order lines were appended),
not the lines' `at`: an append-only file's order is the order things
happened in this repository, and a clock can be wrong. A malformed latest
record makes that branch's ledger record unknown (None), never the older
valid one, which may describe another PR. A ledger record whose number is
not the listed PR's number is an older PR on a reused branch name and is
not used. Which PRs are open, and GitHub's account of them, comes from
the open-PR callable.

**Open PRs on GitHub** (what cm-fkd.4 supplies). The ledger reaches the
repository only through the runner's state PR (spec section 6.1, E11-R3),
so in CI mode (a fresh checkout per run, E10-R2) sweep 1's `pr` line is
not visible to sweep 2 until that PR is merged. Without a second source
every sweep in between would refuse. So the sweep PR carries its base on
GitHub: its body has the line `sweep_marker(base)`, exactly
`<!-- curamoir-sweep base=<full lowercase commit SHA> -->` on a line of
its own (an HTML comment, invisible when rendered), which
parse_sweep_marker reads back strictly (anything else is None). Its paths
are GitHub's own list of the PR's files. cm-fkd.4's callable runs `gh pr
list --state open --json headRefName,number,url,body,files` (bounded by
its own timeout: nothing here can interrupt it) and returns
`[sweep_pr_listing(item) for item in ...]`, the listing contract's
records. `isCrossRepository` is part of that query and MUST stay in it
(next paragraph): without it every listed record is of unknown provenance,
and CI mode refuses until the state PR merges.

**The trust boundary** (guards.pr_cap's docstring holds the rule, for
every reader). A fork's PR has a head branch named by the fork's owner, so
a `curamoir/sweep-` name proves nothing; its record (`cross_repository`
True, gh's `isCrossRepository`) is dropped before grouping by branch and
holds nothing, refuses nothing, and cannot displace cúramóir's own PR
(cúramóir never opens a cross-repository PR, so it holds none of its
fixes). Same repository (`False`) is trusted: pushing that branch needs
write access, which can already PAUSE cúramóir, and the same is true of
editing the body's marker. Missing or not a bool (including a plain
branch name, the old contract) is unknown provenance: the PR still stands
as open and the ledger still knows its own PR by number, but its listed
`base` and `paths` are never the fallback, so with no ledger record it
refuses (step 5) rather than trust what anyone could have written.
Pairing with the ledger: the listed record on that branch with the ledger
record's number (else, for a listing without numbers, the one numberless
record) is the ledger's PR; any other record on the branch is a PR of its
own. Two or more records on one branch, none of them the ledger's, cannot
be told apart and refuse (step 5). `gh` returns at most GH_FILES_CAP files per PR, so a list that
long may be cut short; sweep_pr_listing then gives `paths` None, which
refuses unless the ledger knows (a cut list would free files the PR
changes).

**Limits, stated rather than hidden.**

- A PR closed unmerged frees its files, so the next sweep proposes the same
  fixes again. Remembering a rejection is not E3-R7's (it is "no second PR
  with no intervening changes", and a close is a change); it is a separate
  question of whether a rejected fix should be retried.
- A branch name and a PR number are the identity: a PR closed and
  another opened on the same branch name is matched to the latest `pr`
  record of that branch only if the numbers agree. Two open PRs from one
  branch name (to different base branches) are each judged on their own
  once one is the ledger's; with no ledger match they refuse.
- Only the repository is checked, not the author: anyone with push access
  can open a same-repository `curamoir/sweep-` PR, or edit a sweep PR's
  marker, and be believed. Acceptable: they can PAUSE cúramóir anyway.
- An open sweep PR that neither the ledger nor GitHub accounts for (no
  line, and no marker, more than GH_FILES_CAP files, or no
  `cross_repository: false`) refuses every sweep
  with work until it is merged or closed: safe, and the message says which
  branch.
- The `curamoir/sweep-` branch prefix is reserved for cúramóir: a
  developer's own branch under it is taken for a sweep PR, and, having no
  record and no marker, refuses sweeps until it is merged or closed.
- Two sweeps deciding at once could each see no open PR and both open
  one. Nothing here prevents that: it depends on the runner's lock
  (cm-x5a.6) admitting one sweep per repository at a time.
- Tier-1 findings (curamoir.tier1) are held by file too, before they
  exist: the scan may not choose a file in `held_paths`. With the scan to
  run (`tier1=True`), an empty or wholly held lint list is not yet
  NO_WORK or DUPLICATE_OPEN_PR; Decision.after_tier1 settles it once the
  scan has run, and a PR from this same base still ends the sweep before
  the scan is paid for.
- Tier 1 and E3-R7: see curamoir.tier1, "Once per base" and "Limits".
- Open PRs on other `curamoir/` branches (promote, review) are not sweep
  PRs and hold nothing here; the cap still counts them.

Syntax that imports on 3.9, like sweep.py: the runner reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass

from curamoir.guards.pr_cap import DEFAULT_PREFIX, clipped_repr, open_pr_records
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Refusal
from curamoir.sweep import NO_WORK, WORK
from curamoir.tier1 import FAILED as SCAN_FAILED

__all__ = ["WORK_FIELD", "PR_FIELD", "WORK", "NO_WORK", "DUPLICATE_OPEN_PR", "UNKNOWN",
           "TIER1_FAILED", "SWEEP_BRANCH_PREFIX", "GH_FILES_CAP", "Decision", "pr_field",
           "sweep_prs", "sweep_decision", "sweep_marker", "parse_sweep_marker",
           "sweep_pr_listing"]

#: The ledger field that says what kind of sweep line this is (module docstring).
WORK_FIELD = "work"

#: The ledger field naming the PR a sweep opened: pr_field's dict.
PR_FIELD = "pr"

#: Everything there was to do is in an open sweep PR: a `work` value.
DUPLICATE_OPEN_PR = "duplicate_open_pr"

#: The open PRs could not be known: a decision kind, never a `work` value
#: (the line is a refusal, with Reason.IDEMPOTENCE).
UNKNOWN = "unknown"

#: The tier-1 scan failed and nothing else is left to fix: a decision kind,
#: never a `work` value (the line is a failure, `work: "work"`). WHY not
#: NO_WORK: nothing was shown clean (Decision.after_tier1).
TIER1_FAILED = "tier1_failed"

#: The branches sweep PRs are opened on (E3-R3: `curamoir/sweep-<date>`).
#: Reserved for cúramóir (module docstring, Limits).
SWEEP_BRANCH_PREFIX = DEFAULT_PREFIX + "sweep-"

#: A full commit SHA (SHA-1 or SHA-256).
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

#: The most files `gh pr list --json files` returns for one PR: a list this
#: long may be cut short, so its paths are unknown (sweep_pr_listing).
GH_FILES_CAP = 100

#: The sweep PR body's base marker (module docstring, "Open PRs on GitHub").
#: WHY the same SHA pattern as pr_field (40 or 64): a SHA-256 repository's
#: sweep PRs must be readable too, and both are lowercase hex only.
_MARKER = re.compile(r"<!-- curamoir-sweep base=([0-9a-f]{40}|[0-9a-f]{64}) -->")
_MARKER_WORD = "curamoir-sweep"

#: How much of an error a message quotes.
_CLIP = 200


def pr_field(number, branch, url, base, paths):
    """The `pr` ledger field for the PR a sweep opened (cm-fkd.4 records it
    on the line, whatever the line's outcome, once the PR exists): its
    `number`, `branch`, `url`, the `base` commit it was made from, and the
    `paths` it changes (the gate's paths_for_pr), sorted. ValueError for
    any other shape: a record sweep_prs would skip must not be written."""
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"a PR number is a whole number of 1 or more, not {number!r}")
    if not _text(branch):
        raise ValueError("a PR's branch must be a non-empty string")
    if not isinstance(url, str):
        raise ValueError("a PR's url must be a string")
    if not (isinstance(base, str) and _COMMIT.fullmatch(base)):
        raise ValueError(f"a PR's base must be a full commit SHA, not {base!r}")
    paths = list(paths) if isinstance(paths, (list, tuple)) else None
    if not paths or not all(_text(path) for path in paths):
        raise ValueError("a PR's paths must be a non-empty list of non-empty strings")
    return {"number": number, "branch": branch, "url": url, "base": base,
            "paths": sorted(set(paths))}


def sweep_prs(entries):
    """{branch: pr record or None} from ledger `entries` (Ledger.entries()):
    per branch, the latest `pr` on a sweep job line in ledger order, any
    outcome, in the order the branches were first seen. None when that
    latest record is malformed: the branch's ledger record is unknown, and an
    older valid one is not resurrected. Pure; a `pr` with no recoverable
    branch is skipped, never fatal, like the ledger's own reader."""
    found = {}
    for entry in entries:
        if not (isinstance(entry, dict) and entry.get("kind") == "job"
                and entry.get("job") == "sweep"):
            continue
        raw = entry.get(PR_FIELD)
        if raw is None:
            continue
        record = _valid_pr(raw)
        if record is not None:
            found[record["branch"]] = record
        elif isinstance(raw, Mapping) and _text(raw.get("branch")):
            found[raw["branch"]] = None
    return found


def sweep_marker(base):
    """The line a sweep PR's body carries so GitHub records its base (module
    docstring, "Open PRs on GitHub"). ValueError unless `base` is a full
    lowercase commit SHA."""
    if not (isinstance(base, str) and _COMMIT.fullmatch(base)):
        raise ValueError(f"a sweep marker's base must be a full commit SHA, not "
                         f"{clipped_repr(base)}")
    return f"<!-- curamoir-sweep base={base} -->"


def parse_sweep_marker(body):
    """The base in a PR `body`'s sweep_marker line, else None. Strict: the
    marker is a whole line of exactly sweep_marker's form; any line naming
    `curamoir-sweep` in another form, or two markers with different bases,
    is None (a base read wrong would free files or hide work). Pure."""
    if not isinstance(body, str):
        return None
    bases = set()
    for line in body.splitlines():
        if _MARKER_WORD not in line:
            continue
        match = _MARKER.fullmatch(line)
        if match is None:
            return None
        bases.add(match.group(1))
    return bases.pop() if len(bases) == 1 else None


def sweep_pr_listing(item):
    """One `gh pr list --json headRefName,number,url,body,files,
    isCrossRepository` item as a record of the open-PR listing contract
    (guards.pr_cap): `base` from the body's marker, `paths` from `files`,
    None when there are GH_FILES_CAP or more (possibly cut short), none, or
    any is malformed; `cross_repository` is `isCrossRepository` as given
    (missing is None: unknown provenance, module docstring, "The trust
    boundary"). Pure: cm-fkd.4 runs `gh` and maps its items through this."""
    files = item.get("files")
    paths = None
    if isinstance(files, list) and 0 < len(files) < GH_FILES_CAP:
        names = [file.get("path") if isinstance(file, Mapping) else None for file in files]
        if all(_text(name) for name in names):
            paths = sorted(set(names))
    return {"branch": item.get("headRefName"), "number": item.get("number"),
            "url": item.get("url"), "base": parse_sweep_marker(item.get("body")),
            "paths": paths, "cross_repository": item.get("isCrossRepository")}


@dataclass(frozen=True)
class Decision:
    """What this sweep does (module docstring, "The decision"). `kind` is
    WORK, NO_WORK, DUPLICATE_OPEN_PR, UNKNOWN, or TIER1_FAILED (only from
    after_tier1); `message` the line's
    summary (or the refusal's message); `base` the commit decided for;
    `work_list` the narrowed sweep.WorkList for WORK, else None; `open_prs`
    the open sweep PRs' records considered, by PR number; `held` the
    violations left to them, in work-list order; `held_paths` every file an
    open sweep PR changes, sorted, which the tier-1 scan may not choose
    (curamoir.tier1.scan_candidates)."""

    kind: str
    message: str
    base: str
    work_list: object = None
    open_prs: tuple = ()
    held: tuple = ()
    held_paths: tuple = ()

    @property
    def terminal(self):
        """True when the sweep ends here (`record` writes its line); False
        for WORK, which goes on to the remedies."""
        return self.kind != WORK

    def ledger_fields(self):
        """Keyword fields for the sweep's line: `work` always;
        `open_sweep_prs` ([{number, branch, url}]) and `held_for_open_prs`
        (int) once the open PRs were listed. For WORK they go on the line
        the runner writes later, beside `pr`. TIER1_FAILED is written as
        `work: "work"` (it is a decision kind, not a `work` value).
        ValueError for UNKNOWN, which is a refusal and carries none."""
        if self.kind == UNKNOWN:
            raise ValueError("an unknown decision is a refusal; it has no work fields")
        if self.kind == NO_WORK:
            return {WORK_FIELD: NO_WORK}
        return {WORK_FIELD: WORK if self.kind == TIER1_FAILED else self.kind,
                "open_sweep_prs": [{name: pr[name] for name in ("number", "branch", "url")}
                                   for pr in self.open_prs],
                "held_for_open_prs": len(self.held)}

    def refusal(self):
        """The Refusal for UNKNOWN (Reason.IDEMPOTENCE); ValueError otherwise."""
        if self.kind != UNKNOWN:
            raise ValueError(f"a {self.kind} decision is not a refusal")
        return Refusal(reason=Reason.IDEMPOTENCE, message=self.message)

    def record(self, run, found_delta, **extra):
        """Write a terminal decision's line on `run` (a JobRun): a refusal
        for UNKNOWN (no `found_delta`, as every refusal, and the runner
        writes no snapshot); `run.failed` for TIER1_FAILED; else
        `run.succeeded`; the last two with ledger_fields, `found_delta` and
        `extra` (fix_scope's step 2; the runner then writes the snapshot,
        step 2a). `extra` carries the tier-1 field (fix_scope, "The tier1
        field"); a refusal carries no work fields, so not `extra` either.
        ValueError for WORK, whose line is the runner's once it knows what
        came of the work."""
        if self.kind == WORK:
            raise ValueError("a work decision is not terminal; the runner records its line")
        if self.kind == UNKNOWN:
            run.refused(self.refusal())
        elif self.kind == TIER1_FAILED:
            run.failed(self.message, **self.ledger_fields(), found_delta=found_delta,
                       **extra)
        else:
            run.succeeded(self.message, **self.ledger_fields(), found_delta=found_delta,
                          **extra)

    def after_tier1(self, work, scan):
        """The decision once the tier-1 scan has run, for a WORK decision
        made with `tier1=True`: `work` is the sandbox's work list with the
        accepted findings installed (curamoir.tier1.with_findings), `scan`
        the scan's Tier1Scan. Itself while there is anything to fix; else
        terminal, checked in this order:

        1. TIER1_FAILED when the scan failed: nothing was shown clean, so
           the line is a failure (`record` writes it), never `no_work`;
        2. DUPLICATE_OPEN_PR when the lint's work was all held for open
           sweep PRs;
        3. NO_WORK: neither the lint nor the scan found anything (E3-R7's
           `no_work`).

        ValueError for any other kind."""
        if self.kind != WORK:
            raise ValueError(f"a {self.kind} decision did not wait for the tier-1 scan")
        if work.items or work.tier1:
            return self
        if scan.status == SCAN_FAILED:
            return dataclasses.replace(
                self, kind=TIER1_FAILED, work_list=None,
                message=f"{scan.reason}; the lint left nothing else to fix, so nothing "
                        "was shown clean at this base")
        if self.held:
            return dataclasses.replace(
                self, kind=DUPLICATE_OPEN_PR, work_list=None,
                message="no new PR: every file with lint work is already in open "
                + _names(self.open_prs) + ", and the tier-1 scan found nothing else")
        return dataclasses.replace(
            self, kind=NO_WORK, work_list=None,
            message="no work: neither the lint nor the tier-1 scan found anything to fix "
                    "at this base")

    def apply_to(self, sandbox):
        """Install the narrowed work list on `sandbox` (fix_scope's slot,
        which the remedies, the prompt, the scope check and the fix records
        all read), so a held file is out of scope in code. ValueError unless
        this is a WORK decision made for this sandbox's base and from its
        own work list (the same lint report)."""
        if self.kind != WORK:
            raise ValueError(f"a {self.kind} decision has no work list to apply")
        current = getattr(sandbox, "work_list", None)
        if sandbox.base != self.base or current is None \
                or current.report is not self.work_list.report:
            raise ValueError("the decision was not made from this sandbox's work list at its "
                             "base; decide again after fix_scope.lint_sandbox")
        sandbox.work_list = self.work_list


def sweep_decision(work, base, entries, open_sweep_prs,
                   branch_prefix=SWEEP_BRANCH_PREFIX, tier1=False):
    """The Decision for a sweep whose base `base` linted to `work` (a
    sweep.WorkList, the sandbox's, before any remedy). `entries` are the
    ledger's (Ledger.entries()); `open_sweep_prs` is the open-PR callable
    the open-PR cap is given (guards.pr_cap's listing contract: branch names
    or records), called at most once and only when there is work. The rules:
    the module docstring.

    `tier1` is True when the tier-1 scan will run at this base
    (curamoir.tier1.plan_scan): the lint's work is then not all the work
    there may be, so an empty or wholly held lint list is WORK (with the
    narrowed list, possibly empty), not terminal, and the runner settles it
    after the scan with Decision.after_tier1. A PR already opened from this
    base is DUPLICATE_OPEN_PR either way, before any scan is paid for."""
    if not work.items and not tier1:
        return Decision(NO_WORK, "no work: the lint found nothing to fix at this base", base)
    try:
        records = open_pr_records(open_sweep_prs())
    except Exception as error:
        # WHY so broad: the callable is injected (cm-fkd.4's `gh` query), and
        # whatever it raises, or a listing of the wrong shape, the open PRs
        # are unknown.
        return Decision(UNKNOWN, _unknown(f"could not list open pull requests ("
                                          f"{_clip(str(error))})"), base)
    # Cross-repository records are already gone (open_pr_records): grouping
    # sees only this repository's PRs and those of unknown provenance.
    listed = {}
    for record in records:
        if record["branch"].startswith(branch_prefix):
            listed.setdefault(record["branch"], []).append(
                (_listed(record), record.get("cross_repository") is False))
    ledger = sweep_prs(entries)
    known, unaccounted = [], []
    for branch in sorted(listed):
        found, unknown = _account(listed[branch], ledger.get(branch))
        known += found
        unaccounted += unknown
    open_prs = tuple(sorted(known + unaccounted, key=_by_number))
    same_base = [pr for pr in open_prs if pr["base"] == base]
    if same_base:
        return Decision(DUPLICATE_OPEN_PR, "no new PR: an open sweep PR, " + _names(same_base)
                        + ", was opened from this same base commit; its leftovers wait for "
                        "the next base", base, open_prs=open_prs, held=work.items)
    if unaccounted:
        branches = [pr["branch"] for pr in unaccounted]
        return Decision(UNKNOWN, (
            "neither the ledger nor GitHub says which files the open sweep PR(s) on "
            + ", ".join(branches) + " change (GitHub's account is used only for a PR known "
            "to be from this repository and alone on its branch), so a new sweep PR might "
            "duplicate one (E3-R7); "
            "nothing was fixed, and this clears once "
            + ("it is" if len(branches) == 1 else "they are") + " merged or closed"), base)
    held_paths = {path for pr in open_prs for path in pr["paths"]}
    held = tuple(item for item in work.items if item.path in held_paths)
    remaining = tuple(item for item in work.items if item.path not in held_paths)
    if not remaining and not tier1:
        return Decision(DUPLICATE_OPEN_PR, "no new PR: every file with work is already in "
                        "open " + _names(open_prs), base, open_prs=open_prs, held=held,
                        held_paths=tuple(sorted(held_paths)))
    message = (f"{len(remaining)} violation(s) to fix"
               + (f"; {len(held)} held for open {_names(open_prs)}" if held else "")
               + ("; the tier-1 scan runs next" if tier1 else ""))
    return Decision(WORK, message, base, work_list=dataclasses.replace(work, items=remaining),
                    open_prs=open_prs, held=held, held_paths=tuple(sorted(held_paths)))


def _unknown(why):
    # Only for a listing that failed, which passes: "try again" is true here.
    return (f"{why}, so a new sweep PR might duplicate an open one (E3-R7); "
            "nothing was fixed, and the next sweep will try again")


def _account(group, record):
    """(known, unaccounted) for the open PRs listed on one branch, `group`
    a list of (listed record, same-repository?) in listing order, paired
    with the branch's ledger `record` (or None) by number (module
    docstring, "The trust boundary"). An unaccounted PR keeps its listed
    base only when it is trusted and the sole unpaired PR on its branch."""
    rest = list(group)
    known = []
    if record is not None:
        # WHY by number and not first listed: gh lists newest first, so a
        # later PR on the same branch name would otherwise displace ours.
        at = next((i for i, (pr, _) in enumerate(rest) if pr["number"] == record["number"]),
                  None)
        if at is None:  # a listing without numbers (plain names): as before
            at = next((i for i, (pr, _) in enumerate(rest) if pr["number"] is None), None)
        if at is not None:
            del rest[at]
            known.append(record)  # else: an older PR's line on a reused branch name
    ambiguous = not known and len(rest) > 1
    unaccounted = []
    for pr, trusted in rest:
        if trusted and not ambiguous and pr["base"] and pr["paths"]:
            known.append(pr)
        else:
            unaccounted.append(pr if trusted and not ambiguous else dict(pr, base=None))
    return known, unaccounted


def _listed(record):
    """A listing record's fields, each None unless valid (pr_field's rules)."""
    number, url = record.get("number"), record.get("url")
    base, paths = record.get("base"), record.get("paths")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        number = None
    if not (isinstance(base, str) and _COMMIT.fullmatch(base)):
        base = None
    if not (isinstance(paths, (list, tuple)) and paths and all(_text(p) for p in paths)):
        paths = None
    return {"number": number, "branch": record["branch"],
            "url": url if isinstance(url, str) else None, "base": base,
            "paths": None if paths is None else sorted(set(paths))}


def _by_number(pr):
    return (pr["number"] is None, pr["number"] or 0, pr["branch"])


def _clip(text):
    return text if len(text) <= _CLIP else text[:_CLIP] + "...(clipped)"


def _names(prs):
    return ", ".join(f"#{pr['number']} ({pr['branch']})" if pr["number"] is not None
                     else pr["branch"] for pr in prs)


def _valid_pr(record):
    if not isinstance(record, dict):
        return None
    try:
        return pr_field(record.get("number"), record.get("branch"), record.get("url"),
                        record.get("base"), record.get("paths"))
    except ValueError:
        return None


def _text(value):
    return isinstance(value, str) and value != ""
