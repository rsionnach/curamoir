"""E3-R5: the sweep records per-principle fix counts to the ledger for the
promotion window, and the promotion ladder reads them back.

E3-R5: "The sweep MUST record per-principle fix counts to the ledger for the
promotion window." This is the data the whole ladder rests on: E4-R1
(cm-kcq.1) counts each principle's fixes in `promotion.window_days` against
`promotion.fix_threshold`, and E4-R4 (cm-kcq.4) needs to tell a rule's
violations introduced from merely found. Four parts:

- **The fix record** (FixRecord), one per violation the sweep fixed: the
  shared shape for rule-driven fixes (E3-R1a) and tier-1 fixes (E3-R1b,
  cm-fkd.2), so the ladder counts both the same way. This module lands
  first, so it defines it (the cm-fkd.2 note asked for exactly this).
- **The writer**: record_fixes decides which of the base's violations the PR
  fixes, and ledger_fields shapes them for the sweep's one JobRun line
  (E0-R9's line, the same append-only JSONL).
- **What was found**: found_set, found_delta and the rolling snapshot
  (read_snapshot, snapshot_for, write_snapshot / write_snapshot_or_warn),
  so each line says what changed in the base's violations since the last
  sweep without carrying all of them.
- **The reader**: fix_counts, pure and deterministic, turns ledger entries
  back into {principle id: count} for a window. It is the exact input
  cm-kcq.1 consumes.

**What counts as a fix.** A violation of the base's work list is fixed when
(1) its path is in the gate's `paths_for_pr` — a fix the gate dropped is
not in the PR, so it is not a fix — and (2) the lint, run again on the tree
the gate left (exactly the PR's: kept paths fixed, every other path at its
base), no longer reports it. WHY re-lint rather than "every violation in a
kept file": an agent that fixed one of a file's two violations would be
credited with both, and every over-count moves a principle toward promotion
it has not earned. The lint is deterministic, so a second run is safe; it
is not free: curamoir-lint takes no path arguments (docs/curamoir-lint.md),
so the re-lint checks the whole tree again, costing what the first lint
cost, and the caller passes it the same options (the same `timeout`).

The re-lint (relint) reads `.curamoir/` (rules, principles, `fail_on`) from
the work tree, so it refuses unless that directory is exactly the base's:
the base's rules decide what the base's violations were, and a rule edited
in the sandbox would decide what counts as fixed. It also holds the kept
paths to the bytes the green run tested, before and after
(GateResult.verify_kept), so the tree linted is the tree the PR carries.

"No longer reports it" is per rule and file, netted: for each (rule, path)
the number fixed is the fingerprints that vanished minus those that
appeared, never below zero, and the ones credited are the first that many
vanished fingerprints in the base's work-list order. WHY netted: rewriting
`print(x)` into `print(x, flush=True)` makes the old fingerprint vanish and
a new one appear; that is the same violation, still there, and counts as
nothing.

A remedy deletion (remedies.apply_file_remedies) fixes every violation in
the deleted file, its file-level one and any other, each recorded with
`fixed_by: "remedy"`; everything else kept is the agent's (`"agent"`).

**Introduced vs found (E4-R4).** Decided: not computed here; recorded so it
is derivable. The runner keeps ONE rolling snapshot of the latest full
found set (found_set: every fingerprint the base's lint reported, fixed or
not, by rule) at `.curamoir/state/found.json`, beside the ledger and under
the same policy: committed, never redacted, never leaving the repository.
Each sweep line whose base was linted carries `found_delta`: what was
`added` and `removed` relative to the snapshot it read.

The "introduced" recipe, for E4-R4, all in one place. On a line whose
`found_delta` is not a `baseline`:

1. Gap check first: its `previous_digest` must equal the `digest` of the
   prior line that carried a `found_delta`. If not, a snapshot write was
   lost or a line is missing, and this delta spans more than one sweep:
   still a true difference of found sets, but not one sweep's.
2. Untruncated: a violation was introduced when its fingerprint is in
   `added[rule]` for a rule in `previous_rules_checked` (a rule new since
   the snapshot finds old violations, not new ones).
3. Truncated: the number introduced per rule is `added_counts[rule]` for
   the rules in `previous_rules_checked`; which fingerprints they were
   cannot be named.

A line is a `baseline` (nothing on it is introduced) when there was no
usable snapshot: missing, corrupt, or of a base that is not an ancestor
of this line's base (snapshot_for: after a force-push or rebase the
delta would compare unrelated trees). WHY the
whole set in a snapshot and not on every line: at 10k violations the set is
~680 KB, and the ledger is read whole before every job (the budget guard,
/curamoir:status); a line with small churn is a few hundred bytes. WHY the
whole set at all and not only the fixes: a violation the cap or the gate
left unfixed is still "found" next week, and only the whole set tells it
apart from a new one. The limits of this are in the Limits below.

**The ledger line** (ledger_fields; JobRun adds `at`, `kind`, `job`,
`outcome`, `run_id` and the rest):

- `fixes` (list of FixRecord.as_dict(): principle_id, rule_id or None,
  source, path, fingerprint or None, fixed_by, base, span or None): the
  source of truth, and the only field the reader counts.
- `tier1` (curamoir.tier1's Tier1Scan.ledger_fields): whether the tier-1
  scan ran at this base and what came of it, on every line that got past
  the idempotence decision; the once-per-base rule reads it back.
- `fix_summary` ({principle id: int}): the same numbers, for humans only;
  the reader does not read this field.
- `found_delta` (found_delta's dict):
  - `base` (str): the commit this line's lint ran on.
  - `previous_base` (str or None): the snapshot's base.
  - `digest` (str): the current set's digest (the snapshot format below).
  - `previous_digest` (str or None): the snapshot's digest.
  - `baseline` (bool): no usable snapshot, so `added` is everything and
    none of it is "introduced".
  - `added`, `removed` ({rule id: [fingerprint]}, or None when
    `truncated`).
  - `added_count`, `removed_count` (int).
  - `added_counts`, `removed_counts` ({rule id: int}).
  - `truncated` (bool): a side exceeded DELTA_CAP, so only the counts
    are kept.
  - `rules_checked` (list), `previous_rules_checked` (list or None).
  - `tools` ({tool: version}).

**The snapshot file** (`.curamoir/state/found.json`, found_set's dict):
`version` (SNAPSHOT_VERSION), `base` (the commit linted), `digest`,
`fingerprints` ({rule id: [fingerprint], sorted}), `rules_checked` and
`tools`. The delta is per rule on fingerprints only, never paths (a
fingerprint already identifies its file). `digest` is the sha256 hex of
the canonical JSON of `fingerprints` alone (sorted keys, compact `,` `:`
separators), so it does not depend on how the file is laid out: the file
itself is indented with one fingerprint per line, because it is committed
and git diffs, merges and delta-compresses it by line.

E3-R7's fields (curamoir.idempotence, which decides them and holds the
constants; the convention's reasons are in its docstring):

- `work` (str): what kind of sweep line this is, on every line that got
  past the base's lint and the idempotence decision: `"work"` (there was
  work; the rest of the line says what came of it), `"no_work"` (the
  base's work list was empty) or `"duplicate_open_pr"` (everything to do
  is already in an open sweep PR). The last two are `success` lines.
- `open_sweep_prs` ([{number, branch, url}]) and `held_for_open_prs`
  (int): the open sweep PRs the decision found, and how many of the base's
  violations it left to them (all of them when one was opened from this
  base). On `work`
  and `duplicate_open_pr` lines.
- `pr` (idempotence.pr_field: number, branch, url, `base` commit, sorted
  `paths` the PR changes): the PR this sweep opened, on its line whatever
  the outcome once the PR exists, because the next sweep's decision reads
  `paths` from it to hold those files back.

Which keys appear on which outcome: a success that opened a PR carries
`fixes`, `fix_summary`, `found_delta`, `work` and `pr`; a `no_work` or
`duplicate_open_pr` success, and a failure after the base's lint, carry
`found_delta` and `work` but no `fixes` (so the reader counts nothing for
them); a refusal carries none of them, even one after the lint (a refused
sweep wrote no snapshot either, so the next line's delta still covers what
it saw).

**Limits, stated rather than hidden.**

- Fixing the first of two identical matches in a file renumbers the second
  into the first's fingerprint, so the record names the second's: the
  count is right, the identity is the vanished one. For the same reason
  "introduced" sees the renumbered one as unchanged and the last as removed.
- A rename changes every fingerprint in the file (cm-kcq.1's note): its
  violations read as removed and added, so as introduced.
- A broadened rule finds old violations as new until cm-tmx's rule-set
  digest lets the ladder refuse to compare across it (`tools` carries the
  lint's version once cm-tmx adds it; the digest is the seam left open).
- A sweep that did not lint, or was refused, leaves a gap: its changes
  land on the next line that did. A runner killed between the line and the
  snapshot write, or a write that failed (write_snapshot_or_warn warns and
  keeps the old snapshot: the job had already succeeded), leaves an older
  snapshot, so the next line's `previous_digest` will not match the line
  before it; the recipe's gap check sees it.
- A truncated delta has counts only, so its fingerprints cannot be named.
- A rewritten history (force-push, rebase) restarts "introduced" at a
  baseline: the old snapshot's base is off the new history.
- Tier-1 records have no fingerprint; their identity is their span at the
  base (FixRecord.span, curamoir.tier1), which dedupes within a line only.
  Across lines a tier-1 fix cannot be followed the way a fingerprint is: an
  edit above it moves its lines.
  A tier-1 record without a span is skipped by the reader; none was ever
  written, since `span` and the first tier-1 writer landed together.
- The ledger is not redacted (E0-R7 covers briefs, handoffs, issues and PR
  bodies): paths, rule ids and hex fingerprints are not secrets, and the
  ledger never leaves the repository (E11-R3).

Syntax that imports on 3.9, like sweep.py: the runner reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import contextlib
import hashlib
import json
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from curamoir.fix_scope import ScopeCheckError
from curamoir.git_guard import GitGuardError, git, require_sandbox
from curamoir.ledger import parse_ledger_timestamp
from curamoir.sweep import work_list

#: Where a fix came from: a lint rule's violation (E3-R1a), or a tier-1
#: principle violation that has no rule yet (E3-R1b).
RULE = "rule"
TIER1 = "tier1"
SOURCES = (RULE, TIER1)

#: Who made it: the fix agent's edit, or the runner's remedy (a deletion).
AGENT = "agent"
REMEDY = "remedy"
FIXERS = (AGENT, REMEDY)

#: The directory whose contents the lint reads besides the code: rules,
#: principles, and the config's `fail_on`.
LINT_INPUTS = ".curamoir"

#: How far ahead of `now` a ledger line may be dated and still count. WHY
#: not zero: the ladder runs right after a sweep, possibly on another
#: machine than the one that wrote the line (a ledger merged from CI), and
#: a clock a few hours ahead must not hide the sweep just recorded. WHY not
#: unbounded: a line dated next year would count for the whole of every
#: window until then.
FUTURE_TOLERANCE = timedelta(days=1)

#: JobRun's outcome for a job that succeeded. Only such sweep lines count:
#: a failed or refused sweep opened no PR, so whatever it records fixed
#: nothing.
SUCCESS = "success"

#: The rolling found-set snapshot, beside `ledger.jsonl`.
SNAPSHOT = Path(".curamoir") / "state" / "found.json"
SNAPSHOT_VERSION = 1

#: The most fingerprints one side of a found_delta lists. WHY a cap: one
#: pathological sweep (a rule that suddenly matches everything, a mass
#: rename) must not write a multi-MB line every later job reads. The
#: consumer is E4-R4 (introduced vs found), not the ladder's threshold,
#: which counts `fixes`. Past the cap the line keeps the per-rule counts,
#: which still say how many were introduced per rule, and the digests,
#: which still chain the lines; only the fingerprints' names are lost.
DELTA_CAP = 2000

#: FixRecord's fields, in as_dict's order: what the reader rebuilds a record from.
FIELDS = ("principle_id", "rule_id", "source", "path", "fingerprint", "fixed_by", "base",
          "span")


@dataclass(frozen=True)
class FixRecord:
    """One fixed violation. `rule_id` and `fingerprint` are the lint's for a
    RULE fix and None for a TIER1 fix (it has neither); a TIER1 fix is
    always the agent's, since remedies apply only to rule violations. `base`
    is the commit the violation was found at, which with the fingerprint
    identifies it over time. `span` is a TIER1 fix's (line, end_line) at
    `base`, both 1-based and inclusive (curamoir.tier1): its identity in
    place of a fingerprint. None for a RULE fix. A list is accepted (the
    ledger's JSON) and kept as a tuple. ValueError for any other shape: a
    record the reader would skip must not be written."""

    principle_id: str
    rule_id: Optional[str]
    source: str
    path: str
    fingerprint: Optional[str]
    fixed_by: str
    base: str
    span: Optional[tuple] = None

    def __post_init__(self):
        for name in ("principle_id", "path", "base"):
            if not _text(getattr(self, name)):
                raise ValueError(f"a fix record's {name} must be a non-empty string")
        if self.source not in SOURCES:
            raise ValueError(f"a fix record's source must be one of {', '.join(SOURCES)}")
        if self.fixed_by not in FIXERS:
            raise ValueError(f"a fix record's fixed_by must be one of {', '.join(FIXERS)}")
        if self.source == RULE and not (_text(self.rule_id) and _text(self.fingerprint)):
            raise ValueError("a rule fix names its rule_id and fingerprint")
        if self.source == TIER1 and (self.rule_id is not None or self.fingerprint is not None
                                     or self.fixed_by != AGENT):
            raise ValueError("a tier-1 fix has no rule_id or fingerprint, and is the agent's")
        if self.source == RULE and self.span is not None:
            raise ValueError("a rule fix is identified by its fingerprint, and has no span")
        if self.source == TIER1:
            span = self.span
            if not (isinstance(span, (list, tuple)) and len(span) == 2
                    and all(isinstance(n, int) and not isinstance(n, bool) for n in span)
                    and 1 <= span[0] <= span[1]):
                raise ValueError("a tier-1 fix names its span: [line, end_line], whole "
                                 "numbers with 1 <= line <= end_line")
            object.__setattr__(self, "span", (span[0], span[1]))

    def as_dict(self):
        return {"principle_id": self.principle_id, "rule_id": self.rule_id,
                "source": self.source, "path": self.path, "fingerprint": self.fingerprint,
                "fixed_by": self.fixed_by, "base": self.base,
                "span": None if self.span is None else list(self.span)}


# --- the writer ------------------------------------------------------------------------

def relint(sandbox, gate, **lint_options):
    """The lint (sweep.work_list, with `lint_options`) of `sandbox` as the
    test gate left it, which is the PR's tree. ScopeCheckError if the
    sandbox was not linted at its base (fix_scope.lint_sandbox), its HEAD
    moved, or anything under `.curamoir/` differs from the base; GateError
    (GateResult.verify_kept) if a kept path no longer holds what was tested;
    sweep.LintUntrusted if the report cannot be acted on."""
    require_sandbox(sandbox, ScopeCheckError)
    if sandbox.work_list is None:
        raise ScopeCheckError("no work list was linted in this sandbox at its base "
                              "(fix_scope.lint_sandbox), so there is nothing to compare")
    gate.verify_kept(sandbox)
    if sandbox.head() != sandbox.base:
        raise ScopeCheckError(f"the sandbox's HEAD is no longer the base {sandbox.base}")
    status = git(sandbox.path, "status", "--porcelain", "-z", "--ignored",
                 "--untracked-files=all", "--", LINT_INPUTS)
    if status:
        raise ScopeCheckError(
            f"{LINT_INPUTS}/ in the sandbox is not the base's, so the lint would not "
            "check the base's rules and what it reports fixed could not be trusted")
    after = work_list(sandbox.path, **lint_options)
    gate.verify_kept(sandbox)
    return after


def record_fixes(sandbox, gate, remedies, **lint_options):
    """The FixRecords of the rule fixes the PR carries (the module
    docstring, "What counts as a fix"), in work-list order: after the gate,
    before the patch. `gate` is green_gate's GateResult, `remedies`
    remedies' RemedyResult; `lint_options` go to the re-lint and should be
    the ones lint_sandbox was given (the same `timeout`: the re-lint is a
    full lint). Empty, and no lint run, when the gate kept nothing. Tier-1
    records (TIER1) are not found here: tier1.record_tier1_fixes builds
    them and the runner passes them to ledger_fields beside these. Raises
    as relint, so a re-lint that fails is a refusal, never zero fixes."""
    if not gate.paths_for_pr:
        return ()
    after = relint(sandbox, gate, **lint_options)
    return rule_fix_records(sandbox.work_list.items, after.items, kept=gate.paths_for_pr,
                            remedied=remedies.deleted, base=sandbox.base)


def rule_fix_records(before, after, *, kept, remedied, base):
    """The pure core of record_fixes: the violations of `before` (the base's
    work-list items) in a `kept` path that `after` (the re-lint's items) no
    longer reports, netted per rule and file, as RULE FixRecords in
    `before`'s order. A path in `remedied` was deleted by the runner."""
    kept, remedied = set(kept), set(remedied)
    before_by_group, after_by_group = {}, {}
    for item in before:
        before_by_group.setdefault((item.rule_id, item.path), []).append(item)
    for item in after:
        after_by_group.setdefault((item.rule_id, item.path), set()).add(item.fingerprint)
    fixed = set()
    for (rule_id, path), items in before_by_group.items():
        if path not in kept:
            continue
        remaining = after_by_group.get((rule_id, path), set())
        vanished = [item for item in items if item.fingerprint not in remaining]
        appeared = remaining - {item.fingerprint for item in items}
        fixed.update(item.fingerprint for item in vanished[:max(0, len(vanished) - len(appeared))])
    return tuple(
        FixRecord(item.principle_id, item.rule_id, RULE, item.path, item.fingerprint,
                  REMEDY if item.path in remedied else AGENT, base)
        for item in before if item.fingerprint in fixed)


def ledger_fields(records, delta):
    """Keyword fields for the sweep's success line, beside
    GateResult.ledger_fields(): `fixes` (each record's as_dict, the source
    of truth the reader counts), `fix_summary` ({principle id: n}, for
    humans only; the reader does not read this field) and `found_delta`
    (`delta`, found_delta's). Every key and which outcome carries which:
    the module docstring, "The ledger line". ValueError if a record is of
    another base than the delta's: one line describes one sweep."""
    records = tuple(records)
    base = delta["base"]
    strays = [record for record in records if record.base != base]
    if strays:
        raise ValueError(f"{len(strays)} fix record(s) are of another base than {base}")
    summary = {}
    for record in records:
        summary[record.principle_id] = summary.get(record.principle_id, 0) + 1
    return {"fixes": [record.as_dict() for record in records],
            "fix_summary": dict(sorted(summary.items())),
            "found_delta": delta}


# --- what was found --------------------------------------------------------------------

def found_set(work, base):
    """The full found set of `work` (a sweep.WorkList of the base `base`):
    every fingerprint the lint reported, by rule, sorted, with a `digest`
    of them, the lint's `rules_checked` and `tools`. Pure. What the
    snapshot holds and found_delta compares."""
    by_rule = {}
    for item in work.items:
        by_rule.setdefault(item.rule_id, set()).add(item.fingerprint)
    fingerprints = {rule: sorted(prints) for rule, prints in sorted(by_rule.items())}
    return {"version": SNAPSHOT_VERSION, "base": base, "digest": _digest(fingerprints),
            "fingerprints": fingerprints,
            "rules_checked": list(work.report.rules_checked),
            "tools": dict(work.report.tools)}


def found_delta(previous, current):
    """The `found_delta` ledger field: what `current` (found_set's) added
    and removed relative to `previous` (read_snapshot's, or None). Pure.

    `baseline` is True when `previous` is None or not a valid found set (a
    corrupt or foreign snapshot): `added` is then everything, and none of it
    is "introduced". Pass snapshot_for's result, not read_snapshot's, so a
    snapshot off the current base's history is None too. A rule in
    `rules_checked` but not in `previous_rules_checked` does not make the
    line a baseline; its fingerprints are all `added`, and a reader leaves
    them out of "introduced" by that list. Past
    DELTA_CAP fingerprints on either side, `added` and `removed` are None,
    `truncated` is True, and only the counts remain."""
    if not _valid_found(previous):
        previous = None
    before = previous["fingerprints"] if previous else {}
    now = current["fingerprints"]
    added, removed = {}, {}
    for rule in sorted(set(before) | set(now)):
        old, new = set(before.get(rule, ())), set(now.get(rule, ()))
        if new - old:
            added[rule] = sorted(new - old)
        if old - new:
            removed[rule] = sorted(old - new)
    added_counts = {rule: len(prints) for rule, prints in added.items()}
    removed_counts = {rule: len(prints) for rule, prints in removed.items()}
    added_count, removed_count = sum(added_counts.values()), sum(removed_counts.values())
    truncated = added_count > DELTA_CAP or removed_count > DELTA_CAP
    return {
        "base": current["base"],
        "previous_base": previous["base"] if previous else None,
        "digest": current["digest"],
        "previous_digest": previous["digest"] if previous else None,
        "baseline": previous is None,
        "added": None if truncated else added,
        "removed": None if truncated else removed,
        "added_count": added_count,
        "removed_count": removed_count,
        "added_counts": added_counts,
        "removed_counts": removed_counts,
        "truncated": truncated,
        "rules_checked": list(current["rules_checked"]),
        "previous_rules_checked": list(previous["rules_checked"]) if previous else None,
        "tools": dict(current["tools"]),
    }


def snapshot_path(repo):
    """Where `repo`'s found-set snapshot lives: `.curamoir/state/found.json`,
    beside the ledger. `repo` is the user's checkout, never the sandbox."""
    return Path(repo) / SNAPSHOT


def read_snapshot(path):
    """The found set saved at `path`, or None if it is missing, unreadable,
    not a found set of this version, or its digest does not match its
    fingerprints. Never raises for the file's content: a bad snapshot only
    makes the next line a baseline, which is the honest answer."""
    try:
        with open(path, encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return None
    return found if _valid_found(found) else None


def snapshot_for(repo, base, snapshot):
    """`snapshot` (read_snapshot's) if it can be compared with a lint of
    `base` in `repo` (the user's checkout), else None, which found_delta
    reads as a baseline. Comparable when its base is `base` or an ancestor
    of it (hardened `git merge-base --is-ancestor`). WHY: after a
    force-push, a rebase, or a checkout that rewrote found.json, the
    snapshot's base is off the current history, and a delta against it
    compares unrelated trees, so its "added" would not be "introduced".
    Every git failure (exit 1, not an ancestor; 128, a commit the repository
    does not have) is None as well: a baseline is the honest answer when
    the history cannot be checked."""
    if snapshot is None:
        return None
    previous = snapshot.get("base")
    if previous == base:
        return snapshot
    # WHY the shape check: the base is read from a committed file, and must
    # never reach git as an option or a revision expression.
    if not (isinstance(previous, str) and _COMMIT.fullmatch(previous)
            and isinstance(base, str) and _COMMIT.fullmatch(base)):
        return None
    try:
        git(repo, "merge-base", "--is-ancestor", previous, base)
    except GitGuardError:
        return None
    return snapshot


def write_snapshot_or_warn(path, found, *, sandbox=None):
    """write_snapshot, for the runner's call after a non-refusal line is
    recorded: True if written; on OSError, a RuntimeWarning (printed to
    stderr by default) and False, leaving the old snapshot whole and no temp
    file. WHY not raise: the job's line is already recorded and the job
    succeeded (or failed) on its own terms; a crash here would only lose
    the runner's remaining cleanup. The gap is visible: the next line's
    `previous_digest` will not match this line's `digest`. ValueError (not
    a found set, or a path inside the sandbox) still raises: that is the
    caller's bug, not the disk's."""
    try:
        write_snapshot(path, found, sandbox=sandbox)
    except OSError as error:
        warnings.warn(f"the found-set snapshot {path} was not written ({error}); the old "
                      "one is kept, and the next sweep line's previous_digest shows the gap",
                      RuntimeWarning, stacklevel=2)
        return False
    return True


def write_snapshot(path, found, *, sandbox=None):
    """Replace the snapshot at `path` with `found` (found_set's), atomically:
    a temp file in the same directory (`*.tmp`, which `.curamoir/.gitignore`
    ignores), fsynced, then os.replace, so a writer killed mid-write leaves
    the old snapshot whole and no temp file it knows of. The runner calls it
    after the sweep's line is recorded. ValueError if `found` is not a found
    set, or if `path` is inside `sandbox` (when given): the sandbox is
    thrown away, so a snapshot written there is lost and the next sweep
    would be a baseline."""
    if not _valid_found(found):
        raise ValueError("not a found set (found_set's)")
    path = Path(path)
    if sandbox is not None:
        root = Path(sandbox.path).resolve()
        target = path.resolve()
        if target == root or root in target.parents:
            raise ValueError(f"the snapshot {path} is inside the sandbox {sandbox.path}; "
                             "write it in the user's repository (snapshot_path(repo))")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.",
                                        suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            # WHY indented: one fingerprint per line, for git (the module
            # docstring, "The snapshot file"); the digest is over the
            # compact form, so the layout does not change it.
            json.dump(found, handle, sort_keys=True, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


#: A full commit SHA (SHA-1 or SHA-256), the only base snapshot_for hands git.
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _digest(fingerprints):
    canonical = json.dumps(fingerprints, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _valid_found(found):
    """True for a found set in found_set's shape whose digest is its own."""
    if not isinstance(found, dict) or found.get("version") != SNAPSHOT_VERSION:
        return False
    prints, rules, tools = found.get("fingerprints"), found.get("rules_checked"), found.get("tools")
    if not (_text(found.get("base")) and isinstance(prints, dict) and isinstance(rules, list)
            and isinstance(tools, dict) and all(_text(rule) for rule in rules)):
        return False
    for rule, values in prints.items():
        if not (_text(rule) and isinstance(values, list) and all(_text(v) for v in values)):
            return False
    return found.get("digest") == _digest(prints)


# --- the reader ------------------------------------------------------------------------

def fix_counts(entries, *, now, window_days, principle=None):
    """{principle id: fixes} over the `window_days` up to `now`, from ledger
    `entries` (Ledger.entries()), sorted by principle id; a principle with
    none is absent. With `principle`, only that one's count (or {}).

    Pure and deterministic. Counts the `fixes` records of successful sweep
    job lines dated (`at`) from `now - window_days` to `now` plus
    FUTURE_TOLERANCE, both included. A naive `now` is UTC, as a naive `at`
    is (ledger.parse_ledger_timestamp). Tolerant, like the ledger's own
    reader: an entry or record that is not the shape written here (the
    writer's own check, FixRecord) is skipped, never fatal. ValueError if
    `window_days` is not a whole number of 1 or more, or `now` is not a
    datetime the window can be computed around.

    Dedupe: the same (principle, rule, path, fingerprint) repeated within
    one line, or for a tier-1 fix the same (principle, path, span), counts
    once (one sweep fixes a violation once). Across lines it
    is NOT deduped: fixed, reintroduced and fixed again is two fixes, and
    that recurrence is the signal the ladder promotes on. A line seen twice
    (the same `run_id`, which a merge of the ledger can duplicate) counts
    once: the first copy with a record that counts (in the window) wins, so
    a malformed or empty first copy never hides a populated second. WHY an
    empty copy does not win: one sweep writes one line, so two copies that
    differ mean a merge mangled one, and emptying `fixes` is a likelier
    mangling than inventing valid records. Of two populated copies the
    first wins. A `run_id` that is not a
    non-empty string is treated as absent: the line counts, but copies of
    it cannot be told apart, so each counts."""
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError(f"window_days must be a whole number of 1 or more, not {window_days!r}")
    if not isinstance(now, datetime):
        raise ValueError(f"now must be a datetime, not {now!r}")
    try:
        now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
        now = now.astimezone(timezone.utc)
        start, end = now - timedelta(days=window_days), now + FUTURE_TOLERANCE
    except OverflowError as error:
        raise ValueError(f"now {now!r} is too close to datetime's limits for a "
                         f"{window_days}-day window") from error
    counts, runs = {}, set()
    for entry in entries:
        if not isinstance(entry, dict) or not _successful_sweep(entry):
            continue
        when = parse_ledger_timestamp(entry.get("at"))
        if when is None or not start <= when <= end:
            continue
        fixes = entry.get("fixes")
        if not isinstance(fixes, list):
            continue
        countable = [c for c in map(_countable, fixes) if c is not None]
        if not countable:
            continue
        run_id = entry.get("run_id")
        if _text(run_id):
            if run_id in runs:
                continue
            runs.add(run_id)
        seen = set()
        for principle_id, identity in countable:
            if identity in seen:
                continue
            seen.add(identity)
            if principle is None or principle_id == principle:
                counts[principle_id] = counts.get(principle_id, 0) + 1
    return dict(sorted(counts.items()))


def _successful_sweep(entry):
    return (entry.get("kind") == "job" and entry.get("job") == "sweep"
            and entry.get("outcome") == SUCCESS)


def _countable(item):
    """(principle id, identity) for a record FixRecord would accept, else
    None. The identity is what dedupes within a line: a rule fix's
    (principle, rule, path, fingerprint), a tier-1 fix's (principle,
    "tier1", path, line, end_line). WHY the span suffices for tier 1: the
    dedupe is within one line, whose records are all of one base, so base
    line numbers name one place; curamoir.tier1 also refuses overlapping
    findings at intake, so this is the backstop, not the rule."""
    if not isinstance(item, dict):
        return None
    try:
        record = FixRecord(**{name: item.get(name) for name in FIELDS})
    except (TypeError, ValueError):
        return None
    if record.source == RULE:
        return record.principle_id, (record.principle_id, record.rule_id, record.path,
                                     record.fingerprint)
    return record.principle_id, (record.principle_id, TIER1, record.path, *record.span)


def _text(value):
    return isinstance(value, str) and value != ""
