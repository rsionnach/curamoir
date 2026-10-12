"""E3-R5: the sweep records per-principle fix counts to the ledger for the
promotion window, and the promotion ladder reads them back.

E3-R5: "The sweep MUST record per-principle fix counts to the ledger for the
promotion window." This is the data the whole ladder rests on: E4-R1
(cm-kcq.1) counts each principle's fixes in `promotion.window_days` against
`promotion.fix_threshold`, and E4-R4 (cm-kcq.4) needs to tell a rule's
violations introduced from merely found. Three parts:

- **The fix record** (FixRecord), one per violation the sweep fixed: the
  shared shape for rule-driven fixes (E3-R1a) and tier-1 fixes (E3-R1b,
  cm-fkd.2), so the ladder counts both the same way. This module lands
  first, so it defines it (the cm-fkd.2 note asked for exactly this).
- **The writer**: record_fixes decides which of the base's violations the PR
  fixes, and ledger_fields / found_fields shape them for the sweep's one
  JobRun line (E0-R9's line, the same append-only JSONL).
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
it has not earned. The lint is deterministic and a sweep already runs it
once, so the second run is cheap.

The re-lint (relint) reads `.curamoir/` (rules, principles, `fail_on`) from
the work tree, so it refuses unless that directory is exactly the base's:
the base's rules decide what the base's violations were, and a rule edited
in the sandbox would decide what counts as fixed. It also holds the kept
paths to the bytes the green run tested, before and after
(GateResult.verify_kept), so the tree linted is the tree the PR carries.

"No longer reports it" is per rule and file, netted: for each (rule, path)
the number fixed is the fingerprints that vanished minus those that
appeared. WHY netted: rewriting `print(x)` into `print(x, flush=True)` makes
the old fingerprint vanish and a new one appear; that is the same
violation, still there, and counts as nothing.

A remedy deletion (remedies.apply_file_remedies) fixes every violation in
the deleted file, its file-level one and any other, each recorded with
`fixed_by: "remedy"`; everything else kept is the agent's (`"agent"`).

**Introduced vs found (E4-R4).** Decided: not computed here; recorded so it
is derivable. Each sweep line carries `found`, the fingerprints of EVERY
violation the base's lint reported, fixed or not, grouped by rule, with the
base SHA, `rules_checked` and the lint's `tools`. A violation is introduced
between two sweeps when its fingerprint is in the later `found` and not in
the earlier one, for a rule the earlier one checked. WHY all of `found` and
not only the fixes: a violation the cap or the gate left unfixed is still
"found" next week, and only the whole set tells it apart from a new one.
What it cannot see, stated on cm-kcq.4: a rename changes every fingerprint
in the file (cm-kcq.1's note); a broadened rule finds old violations as new
until cm-tmx's rule-set digest lets the ladder refuse to compare across it
(`tools` carries the lint's version once cm-tmx adds it; the digest is the
seam left open); identical matches in one file are numbered by occurrence,
so fixing the first renumbers the rest; and a sweep that did not lint
leaves a gap, which attributes what appeared to the next one that did.

**Limits, stated rather than hidden.**

- Fixing the first of two identical matches in a file renumbers the second
  into the first's fingerprint, so the record names the second's: the
  count is right, the identity is the vanished one.
- Tier-1 records have no fingerprint, so the reader cannot dedupe them
  within a line; what makes two tier-1 findings the same is cm-fkd.2's to
  define (a span), and until then each record counts.
- The ledger is not redacted (E0-R7 covers briefs, handoffs, issues and PR
  bodies): paths, rule ids and hex fingerprints are not secrets, and the
  ledger never leaves the repository (E11-R3).

Syntax that imports on 3.9, like sweep.py: the runner reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Optional

from curamoir.fix_scope import ScopeCheckError
from curamoir.git_guard import git, require_sandbox
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


@dataclass(frozen=True)
class FixRecord:
    """One fixed violation. `rule_id` and `fingerprint` are the lint's for a
    RULE fix and None for a TIER1 fix (it has neither); a TIER1 fix is
    always the agent's, since remedies apply only to rule violations. `base`
    is the commit the violation was found at, which with the fingerprint
    identifies it over time. ValueError for any other shape: a record the
    reader would skip must not be written."""

    principle_id: str
    rule_id: Optional[str]
    source: str
    path: str
    fingerprint: Optional[str]
    fixed_by: str
    base: str

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

    def as_dict(self):
        return {"principle_id": self.principle_id, "rule_id": self.rule_id,
                "source": self.source, "path": self.path, "fingerprint": self.fingerprint,
                "fixed_by": self.fixed_by, "base": self.base}


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
    """The FixRecords of everything the PR fixes (the module docstring, "What
    counts as a fix"), in work-list order: after the gate, before the patch.
    `gate` is green_gate's GateResult, `remedies` remedies' RemedyResult.
    Empty, and no lint run, when the gate kept nothing. Raises as relint."""
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


def found_fields(work, base):
    """`found`, for any sweep line whose base was linted (a success, a
    no_work, or a failure after the lint): every fingerprint the base's lint
    reported, by rule, with `base`, the lint's `rules_checked` and `tools`.
    What E4-R4 compares between sweeps (the module docstring)."""
    by_rule = {}
    for item in work.items:
        by_rule.setdefault(item.rule_id, []).append(item.fingerprint)
    return {"found": {
        "base": base,
        "fingerprints": {rule: sorted(prints) for rule, prints in sorted(by_rule.items())},
        "rules_checked": list(work.report.rules_checked),
        "tools": dict(work.report.tools),
    }}


def ledger_fields(work, records, base):
    """Keyword fields for the sweep's success line, beside
    GateResult.ledger_fields(): `fixes` (each record's as_dict, the source
    of truth the reader counts), `fix_counts` ({principle id: n}, the same
    numbers for a human reading the line; the reader never trusts them over
    the records), and found_fields. ValueError if a record is of another
    base: one line describes one sweep."""
    records = tuple(records)
    strays = [record for record in records if record.base != base]
    if strays:
        raise ValueError(f"{len(strays)} fix record(s) are of another base than {base}")
    counts = {}
    for record in records:
        counts[record.principle_id] = counts.get(record.principle_id, 0) + 1
    return {"fixes": [record.as_dict() for record in records],
            "fix_counts": dict(sorted(counts.items())),
            **found_fields(work, base)}


# --- the reader ------------------------------------------------------------------------

def fix_counts(entries, *, now, window_days, principle=None):
    """{principle id: fixes} over the `window_days` up to `now`, from ledger
    `entries` (Ledger.entries()), sorted by principle id; a principle with
    none is absent. With `principle`, only that one's count (or {}).

    Pure and deterministic. Counts the `fixes` records of successful sweep
    job lines dated (`at`) from `now - window_days` to `now` plus
    FUTURE_TOLERANCE, both included. A naive `now` is UTC, as a naive `at`
    is (ledger.parse_ledger_timestamp). Tolerant, like the ledger's own
    reader: an entry or record that is not the shape written here is
    skipped, never fatal. ValueError if `window_days` is not a whole number
    of 1 or more.

    Dedupe: a fingerprint repeated within one line counts once (one sweep
    fixes a violation once). Across lines it is NOT deduped: fixed,
    reintroduced and fixed again is two fixes, and that recurrence is the
    signal the ladder promotes on. A line seen twice (the same `run_id`,
    which a merge of the ledger can duplicate) counts once."""
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError(f"window_days must be a whole number of 1 or more, not {window_days!r}")
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    now = now.astimezone(timezone.utc)
    start, end = now - timedelta(days=window_days), now + FUTURE_TOLERANCE
    counts, runs = {}, set()
    for entry in entries:
        if not isinstance(entry, dict) or not _successful_sweep(entry):
            continue
        when = parse_ledger_timestamp(entry.get("at"))
        if when is None or not start <= when <= end:
            continue
        run_id = entry.get("run_id")
        if _text(run_id):
            if run_id in runs:
                continue
            runs.add(run_id)
        fixes = entry.get("fixes")
        if not isinstance(fixes, list):
            continue
        seen = set()
        for item in fixes:
            countable = _countable(item)
            if countable is None:
                continue
            principle_id, identity = countable
            if identity is not None:
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
    """(principle id, identity or None) for a record in the shape FixRecord
    writes, else None. The identity, a rule fix's (rule, path, fingerprint),
    is what dedupes within a line; a tier-1 fix has none."""
    if not isinstance(item, dict) or not _text(item.get("principle_id")):
        return None
    source = item.get("source")
    if source == RULE:
        if not all(_text(item.get(name)) for name in ("rule_id", "path", "fingerprint")):
            return None
        return item["principle_id"], (item["rule_id"], item["path"], item["fingerprint"])
    if source == TIER1:
        if item.get("fingerprint") is not None or not _text(item.get("path")):
            return None
        return item["principle_id"], None
    return None


def _text(value):
    return isinstance(value, str) and value != ""
