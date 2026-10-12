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
to do stopped nobody. The one refusal here is when the open PRs cannot be
listed (UNKNOWN, Reason.IDEMPOTENCE): then the check did not run, and that
is a refusal in the ordinary sense. The open-PR cap and the budget (E3-R6,
cm-fkd.7) are the guard layer's refusals (`pr_cap`, `budget`) and do not
touch `work`. Every field and which line carries it: fix_counts' module
docstring, "The ledger line".

**The decision**, in order:

1. The base's work list is empty: NO_WORK. The open PRs are not asked for;
   they cannot change the answer.
2. The open PRs cannot be listed (`open_pr_branches` raises, or returns
   something other than a list of branch names): UNKNOWN, refuse.
3. An open sweep PR (branch under SWEEP_BRANCH_PREFIX) has no `pr` record
   in the ledger: UNKNOWN, refuse. Its files are unknown, so no file is
   known to be free of it.
4. An open sweep PR was made from this very base: DUPLICATE_OPEN_PR,
   whatever it left out. Nothing has changed since it was opened, and E3-R7
   says that produces no second PR. WHY even for leftovers (a file past
   `max_files_per_pr`, a fix the test gate dropped): "two consecutive
   sweeps with no intervening changes" is exactly this, and the leftovers
   are E3-R3's "rest for the next run" once something has changed.
5. Otherwise every violation in a file that an open sweep PR changes is
   held back (`held`). Nothing left: DUPLICATE_OPEN_PR. Something left:
   WORK, with the work list narrowed to it (`Decision.work_list`), which
   the runner installs on the sandbox (Decision.apply_to) before the
   remedies.

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
subsumes "the same fingerprints": every fix
the PR records is in one of its paths. WHY a PR at an older base still
holds its files: a lint fingerprint does not depend on line numbers
(docs/curamoir-lint.md, "Fingerprint"), so an unchanged file's violations
are the same ones at the newer base, and the PR still fixes them.

**Merged or closed.** The open-PR list is the same source of truth as the
open-PR cap (guards.pr_cap.OpenPrCap's `open_pr_branches`: the branch names
of OPEN PRs), so a merged PR and a closed one look alike: not open, and
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
ledger (sweep_prs): the latest valid one per branch on any sweep job line,
whatever its outcome (a runner that opened the PR and then crashed still
opened it). Which of them are still open comes from `open_pr_branches`.

**Limits, stated rather than hidden.**

- A PR closed unmerged frees its files, so the next sweep proposes the same
  fixes again. Remembering a rejection is not E3-R7's (it is "no second PR
  with no intervening changes", and a close is a change); it is a separate
  question of whether a rejected fix should be retried.
- A branch name is the identity: a PR closed and another opened on the same
  branch name is matched to the latest `pr` record of that branch.
- A ledger without the open PR's line (lost, or a fresh checkout that
  never had it) refuses every sweep with work until that PR is merged or
  closed: safe, and the message says which branch.
- Tier-1 work items (cm-fkd.2) have a path, so they are held by file like
  the rest; nothing here depends on their fingerprint.
- Open PRs on other `curamoir/` branches (promote, review) are not sweep
  PRs and hold nothing here; the cap still counts them.

Syntax that imports on 3.9, like sweep.py: the runner reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import dataclasses
import re
from dataclasses import dataclass

from curamoir.guards.pr_cap import DEFAULT_PREFIX
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Refusal
from curamoir.sweep import NO_WORK, WORK

__all__ = ["WORK_FIELD", "PR_FIELD", "WORK", "NO_WORK", "DUPLICATE_OPEN_PR", "UNKNOWN",
           "SWEEP_BRANCH_PREFIX", "Decision", "pr_field", "sweep_prs", "sweep_decision"]

#: The ledger field that says what kind of sweep line this is (module docstring).
WORK_FIELD = "work"

#: The ledger field naming the PR a sweep opened: pr_field's dict.
PR_FIELD = "pr"

#: Everything there was to do is in an open sweep PR: a `work` value.
DUPLICATE_OPEN_PR = "duplicate_open_pr"

#: The open PRs could not be known: a decision kind, never a `work` value
#: (the line is a refusal, with Reason.IDEMPOTENCE).
UNKNOWN = "unknown"

#: The branches sweep PRs are opened on (E3-R3: `curamoir/sweep-<date>`).
SWEEP_BRANCH_PREFIX = DEFAULT_PREFIX + "sweep-"

#: A full commit SHA (SHA-1 or SHA-256).
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


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
    """{branch: pr record} from ledger `entries` (Ledger.entries()): the
    latest valid `pr` on a sweep job line per branch, any outcome, in the
    order the branches were first seen. Pure. Malformed entries and records
    are skipped, never fatal, like the ledger's own reader."""
    found = {}
    for entry in entries:
        if not (isinstance(entry, dict) and entry.get("kind") == "job"
                and entry.get("job") == "sweep"):
            continue
        record = _valid_pr(entry.get(PR_FIELD))
        if record is not None:
            found[record["branch"]] = record
    return found


@dataclass(frozen=True)
class Decision:
    """What this sweep does (module docstring, "The decision"). `kind` is
    WORK, NO_WORK, DUPLICATE_OPEN_PR or UNKNOWN; `message` the line's
    summary (or the refusal's message); `base` the commit decided for;
    `work_list` the narrowed sweep.WorkList for WORK, else None; `open_prs`
    the open sweep PRs' records considered, by PR number; `held` the
    violations left to them, in work-list order."""

    kind: str
    message: str
    base: str
    work_list: object = None
    open_prs: tuple = ()
    held: tuple = ()

    @property
    def terminal(self):
        """True when the sweep ends here (`record` writes its line); False
        for WORK, which goes on to the remedies."""
        return self.kind != WORK

    def ledger_fields(self):
        """Keyword fields for the sweep's line: `work` always;
        `open_sweep_prs` ([{number, branch, url}]) and `held_for_open_prs`
        (int) once the open PRs were listed. For WORK they go on the line
        the runner writes later, beside `pr`. ValueError for UNKNOWN, which
        is a refusal and carries none."""
        if self.kind == UNKNOWN:
            raise ValueError("an unknown decision is a refusal; it has no work fields")
        if self.kind == NO_WORK:
            return {WORK_FIELD: NO_WORK}
        return {WORK_FIELD: self.kind,
                "open_sweep_prs": [{name: pr[name] for name in ("number", "branch", "url")}
                                   for pr in self.open_prs],
                "held_for_open_prs": len(self.held)}

    def refusal(self):
        """The Refusal for UNKNOWN (Reason.IDEMPOTENCE); ValueError otherwise."""
        if self.kind != UNKNOWN:
            raise ValueError(f"a {self.kind} decision is not a refusal")
        return Refusal(reason=Reason.IDEMPOTENCE, message=self.message)

    def record(self, run, found_delta):
        """Write a terminal decision's line on `run` (a JobRun): a refusal
        for UNKNOWN (no `found_delta`, as every refusal, and the runner
        writes no snapshot), else `run.succeeded` with ledger_fields and
        `found_delta` (fix_scope's step 2; the runner then writes the
        snapshot, step 2a). ValueError for WORK, whose line is the runner's
        once it knows what came of the work."""
        if self.kind == WORK:
            raise ValueError("a work decision is not terminal; the runner records its line")
        if self.kind == UNKNOWN:
            run.refused(self.refusal())
        else:
            run.succeeded(self.message, **self.ledger_fields(), found_delta=found_delta)

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


def sweep_decision(work, base, entries, open_pr_branches,
                   branch_prefix=SWEEP_BRANCH_PREFIX):
    """The Decision for a sweep whose base `base` linted to `work` (a
    sweep.WorkList, the sandbox's, before any remedy). `entries` are the
    ledger's (Ledger.entries()); `open_pr_branches` is the open-PR cap's
    callable (the branch names of open PRs), called at most once and only
    when there is work. The rules: the module docstring."""
    if not work.items:
        return Decision(NO_WORK, "no work: the lint found nothing to fix at this base", base)
    try:
        listed = open_pr_branches()
    except Exception as error:
        # WHY so broad: the callable is injected (a `gh` query by default in
        # the runner), and whatever it raises, the open PRs are unknown.
        return Decision(UNKNOWN, _unknown(f"could not list open pull requests ({error})"),
                        base)
    if not isinstance(listed, (list, tuple)) or not all(_text(b) for b in listed):
        return Decision(UNKNOWN, _unknown(
            f"the open pull requests were listed as {listed!r}, not branch names"), base)
    open_sweeps = sorted({b for b in listed if b.startswith(branch_prefix)})
    records = sweep_prs(entries)
    unaccounted = [branch for branch in open_sweeps if branch not in records]
    if unaccounted:
        return Decision(UNKNOWN, _unknown(
            "the ledger has no record of which files the open sweep PR(s) on "
            + ", ".join(unaccounted) + " change; merge or close them, or restore the "
            "ledger line"), base)
    open_prs = tuple(sorted((records[branch] for branch in open_sweeps),
                            key=lambda pr: pr["number"]))
    same_base = [pr for pr in open_prs if pr["base"] == base]
    if same_base:
        return Decision(DUPLICATE_OPEN_PR, "no new PR: nothing has changed since "
                        + _names(same_base) + " was opened from this base, and it is still "
                        "open", base, open_prs=open_prs, held=work.items)
    held_paths = {path for pr in open_prs for path in pr["paths"]}
    held = tuple(item for item in work.items if item.path in held_paths)
    remaining = tuple(item for item in work.items if item.path not in held_paths)
    if not remaining:
        return Decision(DUPLICATE_OPEN_PR, "no new PR: every file with work is already in "
                        "open " + _names(open_prs), base, open_prs=open_prs, held=held)
    message = (f"{len(remaining)} violation(s) to fix"
               + (f"; {len(held)} held for open {_names(open_prs)}" if held else ""))
    return Decision(WORK, message, base, work_list=dataclasses.replace(work, items=remaining),
                    open_prs=open_prs, held=held)


def _unknown(why):
    return (f"{why}, so a new sweep PR might duplicate an open one (E3-R7); "
            "nothing was fixed, and the next sweep will try again")


def _names(prs):
    return ", ".join(f"#{pr['number']} ({pr['branch']})" for pr in prs)


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
