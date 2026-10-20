"""E3-R1, second clause: the tier-1 scan for violations of principles that
have no rule yet.

E3-R1: "It MAY additionally scan for principle violations that have no rule
yet (tier 1), guided by `principles.md`, but MUST tag each such fix with the
principle id so the promotion counter can count it." This is the intake end
of the promotion ladder (E4-R1): a principle with no rule has no lint, so
its fixes can only come from here, and without them it never earns a rule.
The tag is FixRecord's `principle_id` with `source: "tier1"`
(curamoir.fix_counts), counted exactly as a rule fix is.

On by default (`sweep.tier1_scan`, DEC-5 in curamoir.config); off, the sweep
is the lint's work list alone, and nothing here runs.

**Who scans.** A separate `claude -p` call on `models.cheap`, before
anything edits the sandbox, with a READ-ONLY toolset (SCAN_TOOLS: Read, Grep,
Glob, and Read's allow rules alone, through fix_step.fix_step_argv, so every
other flag of the fix step's fence applies). WHY separate from the fix
step: the scope check (fix_scope.verify_fix) holds every edit to a violation
known BEFORE the edit; a finding the fixing agent declared after editing
would be a licence it drew for itself. The scan's findings are validated
here, installed on the sandbox's work list (`WorkList.tier1`), and the one
fix step then works through lint violations and tier-1 findings alike.
After the scan the sandbox must still be its base, file for file
(_call_scan): a scan that changed anything is a refusal (ScanRefused,
which carries the call's cost for the refusal line), never a scan.

**What the scan reports, and what Python checks** (check_findings). One
fenced JSON document in the reply's `result`: `{"findings": [{"principle_id",
"path", "line", "end_line", "quote", "note"}]}`. `--json-schema` is not
used: fix_step documents and relies on `--output-format json` and
`--max-turns` only, and nothing here leans on a flag it has not cited. Each
finding is refused, with a reason kept for the ledger, unless:

- it has exactly those keys, of those types;
- its principle exists at the base and is ELIGIBLE: `tier: principle` and
  no rule at the base cites it (eligible_principles). WHY both: a rule copied
  from examples/ leaves its principle's tier at `principle`, and the lint
  already counts that principle's fixes; a scan of it would count them twice;
- its path is one of the candidates Python gave the scan (scan_candidates:
  tracked at the base, a language the scope check can review, not held by an
  open sweep PR);
- its span, `line` to `end_line` (1-based, both included), lies inside the
  base file and is at most MAX_TIER1_SPAN lines (a whole-file span would
  license any edit in the file);
- `quote` is the base file's text at `line`, ignoring surrounding
  whitespace: a cheap check that the line was read, not imagined;
- it lies more than WINDOW + 1 lines from every lint violation in the
  file, and neither overlaps nor adjoins a finding accepted before it (in
  path, line order). So no insertion touches two of them (an insertion
  touches a span at its edges, the credit rule below), and a hunk that
  replaces lines of two of them changed lines of each.

At most MAX_FINDINGS findings are considered. A reply that is not that
document (one fenced block, tagged `json` or untagged), a scan that exited
non-zero or timed out, is a FAILED scan: no findings, and the sweep goes on
with the lint's work; with none, idempotence.Decision.after_tier1 makes it
TIER1_FAILED, a failure line, never `no_work`.

**What is taken on the agent's word.** That the code in the span really
violates the principle, and that the edit cures it: no rule exists to
re-lint with. A tier-1 fix is credited (tier1_fix_records) only when all
three hold: its path is in the gate's `paths_for_pr`; a hunk of the PR's
diff touches its span (not the window: the span itself, or an insertion at
either edge of it); and the fix agent's reply says `fixed` for its id.
Fixes count when the sweep's PR opens, as rule fixes do (fix_counts). An
over-count files a promotion issue (E4-R2); `/curamoir:promote` still asks
the developer and never auto-merges.

A tier-1 span never licenses a signature change (fix_scope._check_file),
whatever its principle permits: the agent chose the span.

**Scope (E3-R3).** The scan reads the lint's paths (no extra file in the
PR) and at most `max_files_per_pr` minus that many others, chosen here
deterministically (scan_candidates): files changed since the previous
scan's base first, then a rotation through the rest from the previous
scan's cursor, so successive sweeps cover the repository. The previous
scan is the latest at another base, whatever its outcome, so a sweep that
keeps failing does not pin the rotation; a retry at the same base starts
where that base's attempt started, and rereads its files
(rotation_origin). When the PR is
trimmed to `max_files_per_pr`, the lint's paths go first (trim_pr_paths).

**Once per base (E3-R7).** A scan is a model's reading, and two readings of
one tree can differ: a second sweep at an unchanged base that scanned again
could find something new and open a PR. So the scan runs at most once per
base commit, by the ledger (already_scanned): a sweep line whose `tier1`
says it scanned this base ends tier 1 for it, if that line is a success
(`no_work` included) or carries `pr`. A line that failed after the scan
without opening a PR (a red gate, a PR that would not open) does not: its
findings were neither fixed nor proposed, and taking it as "scanned" would
let the next sweep log `no_work` over them. That sweep scans again instead;
no PR exists, so E3-R7 is not at stake, and it costs one more scan.

Scans that do not end the matter are capped: FAILED and refused scans, and
scans whose sweep then failed or was refused without a PR, count against
MAX_SCAN_ATTEMPTS per base (scan_attempts). This needs every line after
the scan to carry its field, refusals included. Past it, plan_scan says
ATTEMPTS_EXHAUSTED: no model call, the lint alone, and a `no_work` line
then carries `tier1.status: attempts_exhausted`, so the ledger never claims
tier 1 found that base clean. Which lines carry the field: fix_scope, "The
tier1 field".

Limit: in CI mode the ledger reaches the repository through the runner's
state PR, so until that merges a fresh checkout does not see the earlier
scan and may scan again.

**Limits.** With tier 1 on and a principle eligible, E3's "second run on
the merged result opens nothing and logs `no_work`" depends on the model:
the merged result is a new base, so the scan runs again there, and its
rotation reads files no earlier scan read, where it may find something.
What E3-R7 requires, no second PR from an unchanged base, holds by the
once-per-base rule, provided every line after the decision carries the
scan's `tier1` field (fix_scope, "The tier1 field").

**Cost.** The scan and the fix step share the run's one budget estimate,
`budget.per_run_ceiling_usd` (job_guard); the scan is bounded by
SCAN_MAX_TURNS and SCAN_TIMEOUT. No principle eligible, no candidate file
(`max_files_per_pr: 0` among the ways), or attempts exhausted, means no
call and no spend. The call's cost is on every line that carries the
`tier1` field (`cost_usd`), and on the refusal of a scan that changed the
sandbox (ScanRefused).

Syntax that imports on 3.9, like sweep.py.
"""

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from curamoir.fix_counts import AGENT, TIER1, FixRecord
from curamoir.fix_scope import (
    DELETED,
    PYTHON_SUFFIXES,
    SCRIPT_SUFFIXES,
    WINDOW,
    ScopeCheckError,
    base_principles,
    changes_since,
    compute_hunks,
    is_dependency_file,
    nfc,
    overlaps,
    require_pristine,
    violation_span,
)
from curamoir.fix_step import run_fix_step
from curamoir.git_guard import git, require_sandbox, tree_changes, verify_git_state
from curamoir.repo_config import config_value

#: The scan's whole toolset: it reads; it never edits.
SCAN_TOOLS = ("Read", "Grep", "Glob")

#: The longest span a finding may have, in lines (the scope check allows
#: edits within it, so it bounds what one finding can license).
MAX_TIER1_SPAN = 10

#: The most findings one scan may report; the rest are refused.
MAX_FINDINGS = 50

#: Backstops for the scan call (the budget is the real bound, job_guard).
SCAN_MAX_TURNS = 30
SCAN_TIMEOUT = 900

#: The files a scan may choose: those whose changes the scope check can
#: review in full (fix_scope parses Python and reads JS/TS declarations).
SCANNABLE_SUFFIXES = PYTHON_SUFFIXES + SCRIPT_SUFFIXES

#: The longest `note` kept from a finding.
NOTE_CAP = 200

#: How many refused findings a ledger line lists (refused_count says all).
REFUSED_CAP = 20

#: The ledger field (Tier1Scan.ledger_fields).
TIER1_FIELD = "tier1"

#: How many scans one base gets that did not end the matter: FAILED and
#: SCAN_REFUSED scans, and scans whose sweep then failed without a PR
#: (scan_attempts). After that, ATTEMPTS_EXHAUSTED: no more model calls
#: for this base, and the sweep runs on the lint alone.
MAX_SCAN_ATTEMPTS = 2

# Status constants. A Tier1Plan's `status` is READY or one of OFF,
# NO_ELIGIBLE, ALREADY_SCANNED, ATTEMPTS_EXHAUSTED; a Tier1Scan's is one of
# those four (from Tier1Plan.skipped) or SCANNED, NO_CANDIDATES, FAILED
# (from run_tier1_scan), or SCAN_REFUSED (on ScanRefused). Never READY on a
# scan, never SCANNED on a plan.
SCANNED = "scanned"                 # the scan ran and its reply was read
OFF = "off"                         # sweep.tier1_scan is false
NO_ELIGIBLE = "no_eligible"         # every principle has a rule, or none exists
ALREADY_SCANNED = "already_scanned"  # a sweep line already scanned this base
NO_CANDIDATES = "no_candidates"     # no file to scan within max_files_per_pr
FAILED = "failed"                   # the call failed, or its reply was not the document
SCAN_REFUSED = "refused"            # the call changed the sandbox (ScanRefused)
ATTEMPTS_EXHAUSTED = "attempts_exhausted"  # MAX_SCAN_ATTEMPTS used up at this base
READY = "ready"                     # a plan's status only: the scan is to run

#: Why a finding was refused (check_findings).
MALFORMED = "malformed"
UNKNOWN_PRINCIPLE = "unknown_principle"
NOT_ELIGIBLE = "not_eligible"
NOT_A_CANDIDATE = "not_a_candidate"
UNREADABLE = "unreadable"
BAD_SPAN = "bad_span"
SPAN_TOO_LONG = "span_too_long"
QUOTE_MISMATCH = "quote_mismatch"
NEAR_LINT_VIOLATION = "near_lint_violation"
OVERLAPS_FINDING = "overlaps_finding"
OVER_CAP = "over_cap"

_KEYS = frozenset({"principle_id", "path", "line", "end_line", "quote", "note"})
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: A fence line: three backticks and an optional info string (its first
#: word is the tag). _fenced_blocks pairs them in order, opener then closer.
_FENCE_LINE = re.compile(r"^[ \t]*```[ \t]*(\S*)[^\n]*$")
#: One answer line of the fix agent's reply. WHY so strict: "t1-01 not
#: fixed" must not read as `fixed`. The number is at most 3 ASCII digits
#: (MAX_FINDINGS is 50): a longer one does not match, so a hostile reply
#: cannot make int() raise (it refuses strings past 4300 digits) after the
#: gate has run.
_REPLY = re.compile(r"""
    ^[ \t]* (?:\|[ \t]*)?                # an optional table cell's opening bar
    (?:[-*+][ \t]+ | \d+[.)][ \t]+)?     # an optional list marker
    [`*]* t1-([0-9]{1,3}) [`*]*        # the id, perhaps in backticks or bold
    [ \t:*`|\-\u2014\u2013\u2192]*        # separators only, | and em dash, en dash, arrow too
    (fixed|skipped)\b                  # the answer
    """, re.IGNORECASE | re.MULTILINE | re.VERBOSE)


class Tier1ReplyError(ValueError):
    """The scan's reply is not the findings document."""


@dataclass(frozen=True)
class Tier1Finding:
    """One accepted tier-1 finding: a violation of `principle_id` (which has
    no rule) in `path`, lines `line` to `end_line` of the base (1-based,
    both included). `id` is the runner's (`t1-01`, ...), what the fix agent
    replies with. `quote` and `note` are the scan agent's text.

    Violation-shaped for fix_scope.check_scope: `rule_id` is None (which is
    also why it licenses no signature change), `col` is 1 and `end_col`
    None, so the span is whole lines and never reads as file-level."""

    id: str
    principle_id: str
    path: str
    line: int
    end_line: int
    quote: str
    note: str

    source = TIER1
    rule_id = None
    col = 1
    end_col = None

    @property
    def span(self):
        return (self.line, self.end_line)

    def as_dict(self):
        return {"id": self.id, "principle_id": self.principle_id, "path": self.path,
                "line": self.line, "end_line": self.end_line, "quote": self.quote,
                "note": self.note}


@dataclass(frozen=True)
class Refused:
    """A finding the scan reported and check_findings refused."""

    reason: str
    principle_id: Optional[str]
    path: Optional[str]
    detail: str

    def as_dict(self):
        return {"reason": self.reason, "principle_id": self.principle_id,
                "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class Tier1Plan:
    """Whether tier 1 runs at `base` (plan_scan): `status` is READY or the
    reason it does not (OFF, NO_ELIGIBLE, ALREADY_SCANNED); `eligible` the
    eligible Principles, in file order."""

    status: str
    base: str
    eligible: tuple = ()

    @property
    def will_scan(self):
        """What idempotence.sweep_decision's `tier1` is given."""
        return self.status == READY

    def skipped(self):
        """The Tier1Scan of a plan that does not scan. ValueError for READY."""
        if self.will_scan:
            raise ValueError("a ready plan scans; call run_tier1_scan")
        return Tier1Scan(self.status, self.base, eligible=_ids(self.eligible))


@dataclass(frozen=True)
class Tier1Scan:
    """What tier 1 did at `base`. `findings` are the accepted ones
    (Tier1Finding), `refused` the rest (Refused); `candidates` the files the
    scan was given, in the order given; `cursor` the rotation's position
    after it (scan_candidates), and `cursor_from` and `changed_from` where
    it started (rotation_origin), so a retry at this base rereads the same
    files; `reason` why a FAILED scan failed; `cost_usd`
    the call's `total_cost_usd`, when it reported one; `model` the model it
    ran on and `usage` the payload's `usage` object, when there was one
    (kept for the runner's own cost line; the ledger field carries the
    cost)."""

    status: str
    base: str
    eligible: tuple = ()
    candidates: tuple = ()
    cursor: Optional[str] = None
    cursor_from: Optional[str] = None
    changed_from: Optional[str] = None
    findings: tuple = ()
    refused: tuple = ()
    reason: Optional[str] = None
    cost_usd: Optional[float] = None
    model: Optional[str] = None
    usage: Optional[dict] = None

    @property
    def scanned(self):
        """True when the scan ran and its reply was read. already_scanned
        counts it only on a line that also succeeded or opened a PR."""
        return self.status == SCANNED

    def ledger_fields(self):
        """`{"tier1": {...}}` for the sweep's line, whatever its outcome
        once the idempotence decision was made (fix_counts, "The ledger
        line"). The findings are counted, not listed: what was fixed is in
        `fixes`; what was refused is listed up to REFUSED_CAP. `cost_usd`
        is the scan call's spend, on every line that carries the field."""
        return {TIER1_FIELD: {
            "status": self.status,
            "scanned": self.scanned,
            "base": self.base,
            "eligible": list(self.eligible),
            "candidates": sorted(self.candidates),
            "cursor": self.cursor,
            "cursor_from": self.cursor_from,
            "changed_from": self.changed_from,
            "findings": len(self.findings),
            "refused": [r.as_dict() for r in self.refused[:REFUSED_CAP]],
            "refused_count": len(self.refused),
            "reason": self.reason,
            "cost_usd": self.cost_usd,
        }}


class ScanRefused(ScopeCheckError):
    """The scan call changed the sandbox: a refusal (fix_scope's step 6).
    `scan` is its Tier1Scan (status SCAN_REFUSED), so the refusal line
    records what the call cost and the attempt counts (scan_attempts)."""

    def __init__(self, message, scan):
        super().__init__(message)
        self.scan = scan

    @property
    def cost_usd(self):
        return self.scan.cost_usd

    def ledger_fields(self):
        return self.scan.ledger_fields()


# --- the plan --------------------------------------------------------------------------

def eligible_principles(principles, rules):
    """The principles of `principles` (a PrinciplesFile) a tier-1 scan may
    report: at `tier: principle`, and cited by none of `rules` (the base's,
    rules.Rule). In file order."""
    cited = {rule.principle for rule in rules}
    return tuple(p for p in principles.principles
                 if p.tier == "principle" and p.id not in cited)


def already_scanned(entries, base):
    """True when ledger `entries` hold a sweep line whose scan ran at
    `base` and which ended the matter: a success (`no_work` included) or a
    line carrying `pr`. A line that failed without a PR does not count: its
    findings were never fixed or proposed, so the next sweep scans again
    (module docstring, "Once per base")."""
    return any(record.get("base") == base for record in _done_scans(entries))


#: The statuses of a scan that made the model call (an attempt with an origin).
_CALLED = frozenset({SCANNED, FAILED, SCAN_REFUSED})


def rotation_origin(entries, base):
    """(changed_from, cursor_from): where a scan at `base` starts choosing
    files (scan_candidates). A retry at the same base restarts where the
    latest attempt at `base` started (its `changed_from` and `cursor_from`),
    so it rereads the same files; otherwise the latest line at another base
    whose scan ran, whatever its outcome, gives its `base` and the `cursor`
    it left, so the rotation moves on even when that sweep failed. WHY not
    only ended scans: a fix that keeps turning the gate red would then pin
    every later base to the same slice. (None, None) when there is none."""
    same = other = None
    for _entry, record in _tier1_records(entries):
        if record.get("base") == base:
            # Only a scan that made the call started somewhere: a skipped
            # plan's line (OFF, NO_ELIGIBLE, ATTEMPTS_EXHAUSTED, ...) has no
            # origin, and must not shadow the attempts before it.
            if record.get("status") in _CALLED:
                same = record
        elif record.get("scanned") is True:
            other = record
    if same is not None:
        return _text_or_none(same.get("changed_from")), _text_or_none(same.get("cursor_from"))
    if other is not None:
        return other["base"], _text_or_none(other.get("cursor"))
    return None, None


def _text_or_none(value):
    return value if isinstance(value, str) else None


def scan_attempts(entries, base):
    """How many scans at `base` did not end the matter (MAX_SCAN_ATTEMPTS):
    lines whose scan FAILED or was refused (SCAN_REFUSED), and lines whose
    scan ran but which failed without a PR."""
    count = 0
    for entry, record in _tier1_records(entries):
        if record.get("base") != base:
            continue
        if record.get("status") in (FAILED, SCAN_REFUSED) or (
                record.get("scanned") is True and not _ended(entry)):
            count += 1
    return count


def _done_scans(entries):
    for entry, record in _tier1_records(entries):
        if record.get("scanned") is True and _ended(entry):
            yield record


def _ended(entry):
    return entry.get("outcome") == "success" or entry.get("pr") is not None


def _tier1_records(entries):
    for entry in entries:
        if not (isinstance(entry, dict) and entry.get("kind") == "job"
                and entry.get("job") == "sweep"):
            continue
        record = entry.get(TIER1_FIELD)
        if isinstance(record, dict) and isinstance(record.get("base"), str):
            yield entry, record


def plan_scan(sandbox, entries):
    """Whether tier 1 runs in `sandbox` (linted at its base by
    fix_scope.lint_sandbox): OFF unless the base's `sweep.tier1_scan` is
    true, NO_ELIGIBLE when no principle at the base is eligible,
    ALREADY_SCANNED when ledger `entries` hold a scan of this base that
    ended the matter (already_scanned), ATTEMPTS_EXHAUSTED when
    MAX_SCAN_ATTEMPTS scans of it did not, else READY. Reads the config and principles at the base (the sandbox is
    still pristine) and the rules lint_sandbox recorded."""
    require_sandbox(sandbox, ScopeCheckError)
    if sandbox.rules is None:
        raise ScopeCheckError("no rules were loaded in this sandbox at its base "
                              "(fix_scope.lint_sandbox), so which principles have one is "
                              "unknown")
    require_pristine(sandbox)
    if config_value(str(sandbox.path), "sweep", "tier1_scan") is not True:
        return Tier1Plan(OFF, sandbox.base)
    eligible = eligible_principles(base_principles(sandbox), sandbox.rules)
    if not eligible:
        return Tier1Plan(NO_ELIGIBLE, sandbox.base)
    if already_scanned(entries, sandbox.base):
        return Tier1Plan(ALREADY_SCANNED, sandbox.base, eligible)
    if scan_attempts(entries, sandbox.base) >= MAX_SCAN_ATTEMPTS:
        return Tier1Plan(ATTEMPTS_EXHAUSTED, sandbox.base, eligible)
    return Tier1Plan(READY, sandbox.base, eligible)


# --- the candidates --------------------------------------------------------------------

def scannable(path):
    """Whether the scan may choose `path`: a language the scope check can
    review, not cúramóir's own state, not a dependency manifest."""
    return (path.endswith(SCANNABLE_SUFFIXES) and not path.startswith(".curamoir/")
            and not is_dependency_file(path))


def _tracked_scannable(sandbox):
    """Every regular file tracked at the sandbox's base that is scannable,
    in git's spelling and order."""
    listing = git(sandbox.path, "ls-tree", "-r", "-z", "--full-tree", sandbox.base)
    paths = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        path = name.decode("utf-8", "surrogateescape")
        if meta.split(b" ", 1)[0] in (b"100644", b"100755") and scannable(path):
            paths.append(path)
    return paths


def _changed_between(sandbox, before):
    """The paths that differ between commit `before` (the previous scan's
    base, read from the ledger) and the sandbox's base; none when `before`
    is not a full SHA, is this base, or is not a commit the clone has (a
    rewritten history)."""
    if not (isinstance(before, str) and _COMMIT.fullmatch(before)) or before == sandbox.base:
        return []
    known = git(sandbox.path, "rev-parse", "--verify", "--quiet", "--end-of-options",
                f"{before}^{{commit}}", ok=(0, 1, 128)).strip()
    if not known:
        return []
    diff = git(sandbox.path, "diff", "--name-only", "-z", "--no-renames",
               before, sandbox.base, ok=(0, 128))
    return [name.decode("utf-8", "surrogateescape") for name in diff.split(b"\0") if name]


def choose_candidates(lint_paths, universe, changed, cursor, cap, reserved=()):
    """The pure core of scan_candidates. Returns (candidates, cursor).

    The lint's scannable paths come first, at most `cap` of them; they
    cost no extra file in the PR. `reserved` is every lint path the PR may carry, scannable or not
    (default: `lint_paths`). The room for other files is `cap` minus the
    number of reserved paths. Those files come from `changed` first, then
    from a rotation through `universe` that starts after `cursor`.
    `universe` and `changed` hold no lint path and no held path. The new
    cursor is the last path the rotation took, or `cursor` if it took
    none."""
    lint_paths = sorted(set(lint_paths))[:max(0, cap)]
    room = max(0, cap - len(set(lint_paths) | set(reserved)))
    universe = sorted(set(universe))
    pool = set(universe)
    picked = [path for path in sorted(set(changed)) if path in pool][:room]
    taken = set(picked)
    after = [p for p in universe if cursor is None or p > cursor]
    before = [p for p in universe if cursor is not None and p <= cursor]
    for path in after + before:
        if len(picked) >= room:
            break
        if path not in taken:
            picked.append(path)
            taken.add(path)
            cursor = path
    return tuple(lint_paths + picked), cursor


def scan_candidates(sandbox, held_paths, entries, cap):
    """The files the scan is given, and the rotation's cursor after it:
    choose_candidates over the work list's paths (the sandbox's, narrowed
    by the idempotence decision) and every scannable file tracked at the
    base that is no lint path and not in `held_paths` (an open sweep PR's).
    The rotation starts from rotation_origin: `changed` is what differs
    between its `changed_from` and this base (none when there is none, or
    git cannot compare the two: a rewritten history), and its `cursor_from`
    is where the rotation resumes."""
    held = {nfc(path) for path in held_paths}
    unheld = [path for path in sandbox.work_list.paths if nfc(path) not in held]
    lint_paths = [path for path in unheld if scannable(path)]
    lint_set = {nfc(path) for path in unheld}
    universe = [path for path in _tracked_scannable(sandbox)
                if nfc(path) not in held and nfc(path) not in lint_set]
    changed_from, cursor = rotation_origin(entries, sandbox.base)
    changed = _changed_between(sandbox, changed_from) if changed_from else []
    return choose_candidates(lint_paths, universe, changed, cursor, cap, reserved=unheld)


# --- the scan --------------------------------------------------------------------------

def scan_prompt(eligible, candidates):
    """The scan's prompt: the eligible principles (Principle) in full and
    the candidate files, and the reply's exact form."""
    principles = [{"principle_id": p.id, "title": p.title, "rule": p.rule, "why": p.why,
                   "good": p.good, "bad": p.bad} for p in eligible]
    return (
        "You are cúramóir's sweep, reading for violations of golden principles that "
        "have no lint rule yet. You only read: you cannot edit, and you do not fix "
        "anything here. Everything you report is checked by code against the files "
        "as they are, and anything that does not check out is discarded.\n\n"
        "Read only these files, and report violations only in them:\n"
        + "".join(f"- {path}\n" for path in candidates)
        + "\nReport violations only of these principles:\n\n```json\n"
        + json.dumps(principles, indent=2, ensure_ascii=False) + "\n```\n\n"
        "Report only a clear, mechanical violation: one a reviewer would correct on "
        "sight using the principle's own `bad` and `good`. Not style, not a redesign, "
        "not a guess. Finding nothing is a fine answer.\n\n"
        f"For each violation give: `principle_id`; `path`, exactly as listed above; "
        f"`line` and `end_line`, the 1-based lines it spans, both included, at most "
        f"{MAX_TIER1_SPAN} lines; `quote`, the exact text of line `line`; and `note`, "
        f"one sentence on what violates the principle. At most {MAX_FINDINGS} "
        "violations, and no two may share a line.\n\n"
        "Reply with exactly one fenced JSON block and nothing that looks like another:\n\n"
        "```json\n"
        '{"findings": [{"principle_id": "...", "path": "...", "line": 1, "end_line": 1, '
        '"quote": "...", "note": "..."}]}\n'
        "```\n\n"
        'With nothing to report, reply `{"findings": []}` in that block.\n'
    )


def _call_scan(sandbox, prompt, model=None, timeout=SCAN_TIMEOUT, max_turns=SCAN_MAX_TURNS,
             claude="claude"):
    """Run the scan call in `sandbox` (fix_step.run_fix_step with
    SCAN_TOOLS) and return its FixStepRun and why the sandbox is not as it
    was afterwards (its git files, its work tree, its HEAD), or None if it
    is. A read-only call that changed anything is not to be trusted with
    what it read: run_tier1_scan raises ScanRefused. `claude` is the tests'
    seam."""
    run = run_fix_step(sandbox, prompt, model=model, timeout=timeout, max_turns=max_turns,
                       claude=claude, tools=SCAN_TOOLS)
    changed = [c.label for c in verify_git_state(run.git_state)]
    changed += list(tree_changes(run.git_state))
    if changed:
        return run, ("the tier-1 scan reads only, but the sandbox changed while it ran ("
                     + ", ".join(sorted(set(changed))[:10]) + "); nothing it reported is used")
    try:
        require_pristine(sandbox)
    except ScopeCheckError as error:
        return run, f"the tier-1 scan reads only, but afterwards {error}"
    return run, None


def _fenced_blocks(text):
    """The bodies of `text`'s fenced blocks tagged `json` (any case) or
    untagged. Fence lines are paired in order, line by line: an opener,
    then the next fence line closes it, so a ```python block's closing
    fence is never taken for an opener. An opener never closed is no
    block."""
    blocks, opened, body = [], None, []
    for line in text.split("\n"):
        line = line[:-1] if line.endswith("\r") else line
        match = _FENCE_LINE.match(line)
        if opened is None:
            if match:
                opened, body = match.group(1).lower(), []
            continue
        if match:
            if opened in ("json", ""):
                blocks.append("\n".join(body))
            opened = None
        else:
            body.append(line)
    return blocks


def parse_findings(text):
    """The `findings` list of the scan's reply `text`: one fenced block
    tagged `json` or untagged (_fenced_blocks; other blocks are ignored), or
    the whole text, if it is the document, a JSON object with
    exactly the key `findings`, a list. Tier1ReplyError otherwise. The
    entries are not checked here (check_findings)."""
    if not isinstance(text, str):
        raise Tier1ReplyError("the scan's reply has no text result")
    blocks = _fenced_blocks(text)
    if len(blocks) > 1:
        raise Tier1ReplyError(f"the scan's reply has {len(blocks)} fenced blocks, not one")
    raw = blocks[0] if blocks else text.strip()
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise Tier1ReplyError(f"the scan's reply is not JSON: {error}") from error
    if not (isinstance(document, dict) and set(document) == {"findings"}
            and isinstance(document["findings"], list)):
        raise Tier1ReplyError('the scan\'s reply is not {"findings": [...]}')
    return document["findings"]


# --- checking the findings --------------------------------------------------------

def check_findings(raw, *, known, eligible, candidates, base_text, lint_items,
                   window=WINDOW, max_span=MAX_TIER1_SPAN, cap=MAX_FINDINGS):
    """The pure core: (accepted Tier1Findings, Refused) from `raw` (the
    reply's findings list). `known` is every principle id at the base,
    `eligible` the eligible ids, `candidates` the paths the scan was given,
    `base_text(path)` the base file's text or None, `lint_items` the work
    list's violations. The rules: the module docstring. Accepted findings
    are in (path, line, end_line, principle) order and numbered t1-01, ...
    in that order."""
    lint_spans = {}
    for item in lint_items:
        lint_spans.setdefault(nfc(item.path), []).append(violation_span(item, window))
    context = _Context(
        known=known, eligible=eligible,
        # nfc -> the candidate's own spelling (git's), which the finding keeps.
        candidates={nfc(path): path for path in candidates},
        base_text=base_text, lint_spans=lint_spans, max_span=max_span)
    refused, valid = [], []
    for index, entry in enumerate(raw):
        if index >= cap:
            refused.append(Refused(OVER_CAP, _field(entry, "principle_id"),
                                   _field(entry, "path"),
                                   f"more than {cap} findings; the rest are not read"))
            continue
        checked = _check_one(entry, context)
        (refused if isinstance(checked, Refused) else valid).append(checked)
    valid.sort(key=lambda f: (f.path, f.line, f.end_line, f.principle_id))
    accepted, spans = [], {}
    for f in valid:
        # Adjoining counts as overlapping: one insertion between two
        # adjoining spans would touch both (touches), and be two credits.
        if any(overlaps((f.line - 1, f.end_line + 1), other)
               for other in spans.get(f.path, ())):
            refused.append(Refused(OVERLAPS_FINDING, f.principle_id, f.path,
                                   f"lines {f.line}-{f.end_line} overlap or adjoin a "
                                   "finding already accepted; one change is credited once"))
            continue
        spans.setdefault(f.path, []).append((f.line, f.end_line))
        accepted.append(f)
    width = max(2, len(str(len(accepted))))
    return (tuple(Tier1Finding(f"t1-{number:0{width}d}", f.principle_id, f.path, f.line,
                               f.end_line, f.quote, f.note)
                  for number, f in enumerate(accepted, start=1)),
            tuple(refused))


@dataclass
class _Context:
    """What check_findings holds every finding to (its parameters), and the
    base texts read so far (`texts`, path -> text or None)."""

    known: object
    eligible: object
    candidates: dict
    base_text: object
    lint_spans: dict
    max_span: int
    texts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Checked:
    """A finding that passed _check_one, before the overlap pass and its id."""

    principle_id: str
    path: str
    line: int
    end_line: int
    quote: str
    note: str


def _check_one(entry, context):
    """A _Checked for `entry` (one raw finding), or the Refused saying why
    not: every rule of the module docstring but the overlap between
    findings, which check_findings applies to the survivors."""
    pid, path = _field(entry, "principle_id"), _field(entry, "path")
    if not (isinstance(entry, dict) and set(entry) == _KEYS
            and all(isinstance(entry[k], str) for k in ("principle_id", "path", "quote", "note"))
            and all(isinstance(entry[k], int) and not isinstance(entry[k], bool)
                    for k in ("line", "end_line"))):
        return Refused(MALFORMED, pid, path,
                       "not an object with exactly principle_id, path, line, end_line, "
                       "quote and note, of the right types")
    if pid not in context.known:
        return Refused(UNKNOWN_PRINCIPLE, pid, path, "no such principle at the base")
    if pid not in context.eligible:
        return Refused(NOT_ELIGIBLE, pid, path,
                       "the principle has a rule (or is past tier `principle`), so it is "
                       "not tier 1")
    if nfc(path) not in context.candidates:
        return Refused(NOT_A_CANDIDATE, pid, path, "not one of the files the scan was given")
    path = context.candidates[nfc(path)]
    if path not in context.texts:
        context.texts[path] = context.base_text(path)
    text = context.texts[path]
    if text is None:
        return Refused(UNREADABLE, pid, path, "not a UTF-8 text file at the base")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    line, end_line = entry["line"], entry["end_line"]
    if not 1 <= line <= end_line <= len(lines):
        return Refused(BAD_SPAN, pid, path,
                       f"lines {line}-{end_line} are not a span of its {len(lines)} lines")
    if end_line - line + 1 > context.max_span:
        return Refused(SPAN_TOO_LONG, pid, path,
                       f"lines {line}-{end_line} are more than {context.max_span} lines")
    quote = entry["quote"].strip()
    at_base = lines[line - 1].rstrip("\r")
    if line == 1:
        at_base = at_base.lstrip("\ufeff")  # a UTF-8 byte order mark is not text
    if not quote or quote != at_base.strip():
        return Refused(QUOTE_MISMATCH, pid, path,
                       f"the quote is not the text of line {line} at the base")
    # WINDOW + 1: an insertion just past a lint window's edge is the lint
    # fix's (fix_scope's hunk range) and would touch a span starting there.
    if any(overlaps((line - 1, end_line + 1), span)
           for span in context.lint_spans.get(nfc(path), ())):
        return Refused(NEAR_LINT_VIOLATION, pid, path,
                       f"lines {line}-{end_line} are within {WINDOW + 1} lines of a lint "
                       "violation, so one change could count for both")
    return _Checked(pid, path, line, end_line, quote, " ".join(entry["note"].split())[:NOTE_CAP])


def _field(entry, name):
    value = entry.get(name) if isinstance(entry, dict) else None
    return value if isinstance(value, str) else None


def run_tier1_scan(sandbox, plan, held_paths, entries, *, cap=None, model=None,
                   timeout=SCAN_TIMEOUT, max_turns=SCAN_MAX_TURNS, claude="claude"):
    """Tier 1 for a READY `plan` (plan_scan) in `sandbox`, after the
    idempotence decision was applied to it (so its work list is the
    narrowed one) and before the remedies: choose the candidates, run the
    scan, check what it reports. Returns a Tier1Scan; install its findings
    with with_findings. `held_paths` is Decision.held_paths; `cap` defaults
    to the base's `sweep.max_files_per_pr`. ScanRefused (a ScopeCheckError,
    carrying the call's cost) if the scan changed the sandbox."""
    if not plan.will_scan:
        raise ValueError(f"a {plan.status} plan does not scan")
    require_sandbox(sandbox, ScopeCheckError)
    if sandbox.base != plan.base or sandbox.work_list is None:
        raise ScopeCheckError("the plan is not for this sandbox's base, or the sandbox "
                              "was not linted (fix_scope.lint_sandbox)")
    if cap is None:
        cap = config_value(str(sandbox.path), "sweep", "max_files_per_pr")
    eligible = _ids(plan.eligible)
    changed_from, cursor_from = rotation_origin(entries, sandbox.base)
    candidates, cursor = scan_candidates(sandbox, held_paths, entries, cap)
    origin = dict(cursor_from=cursor_from, changed_from=changed_from)
    if not candidates:
        return Tier1Scan(NO_CANDIDATES, sandbox.base, eligible=eligible, cursor=cursor, **origin)
    run, changed = _call_scan(sandbox, scan_prompt(plan.eligible, candidates), model=model,
                              timeout=timeout, max_turns=max_turns, claude=claude)
    payload = run.payload() or {}
    cost = payload.get("total_cost_usd")
    cost = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    spent = dict(base=sandbox.base, eligible=eligible, candidates=candidates, cursor=cursor,
                 **origin, cost_usd=cost, model=run.argv[run.argv.index("--model") + 1], usage=usage)
    if changed:
        raise ScanRefused(changed, Tier1Scan(SCAN_REFUSED, reason=changed, **spent))
    failed = dict(status=FAILED, **spent)
    if run.timed_out or run.exit_code != 0 or payload.get("is_error") is True:
        why = "timed out" if run.timed_out else f"exited {run.exit_code}"
        return Tier1Scan(**failed, reason=f"the tier-1 scan {why}")
    try:
        raw = parse_findings(payload.get("result"))
    except Tier1ReplyError as error:
        return Tier1Scan(**failed, reason=str(error))
    known = {p.id for p in base_principles(sandbox).principles}
    findings, refused = check_findings(
        raw, known=known, eligible=set(eligible), candidates=candidates,
        base_text=lambda path: _base_text(sandbox, path),
        lint_items=sandbox.work_list.items)
    return Tier1Scan(SCANNED, findings=findings, refused=refused, **spent)


def with_findings(work, scan):
    """`work` (a sweep.WorkList) with `scan`'s accepted findings as its
    `tier1`: what the runner installs as `sandbox.work_list`."""
    return dataclasses.replace(work, tier1=tuple(scan.findings))


def _base_text(sandbox, path):
    data = sandbox.show(path)
    if data is None or b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _ids(principles):
    return tuple(p.id for p in principles)


# --- the credit ------------------------------------------------------------------------

def parse_fix_replies(text):
    """{finding number: "fixed" or "skipped"} from the fix agent's reply,
    keyed by the id's number (so `t1-1` is t1-01). Only a line that is
    the id and then, with nothing but separators between, the answer
    (_REPLY): "t1-01 not fixed" and "t1-02: could not be fixed, skipped"
    are no answer at all. An id given both answers is left out (not fixed).
    Untrusted text, used only as one of the three conditions of a credit."""
    if not isinstance(text, str):
        return {}
    seen = {}
    for match in _REPLY.finditer(text):
        seen.setdefault(int(match.group(1)), set()).add(match.group(2).lower())
    return {key: answers.pop() for key, answers in seen.items() if len(answers) == 1}


def finding_number(finding_id):
    """The number of a finding id (`t1-07` -> 7), as parse_fix_replies keys it."""
    return int(finding_id.rsplit("-", 1)[1])


def touches(hunk, line, end_line):
    """Whether a `-U0` hunk (fix_scope.Hunk, base coordinates) changes lines
    `line` to `end_line`: replaces or deletes one of them, or inserts
    between two of them or at either edge."""
    if hunk.old_count == 0:
        return line - 1 <= hunk.old_start <= end_line
    return hunk.old_start <= end_line and line <= hunk.old_start + hunk.old_count - 1


def tier1_fix_records(findings, hunks, *, kept, replies, base):
    """The pure core of record_tier1_fixes: a TIER1 FixRecord for each of
    `findings` whose path is in `kept` (the gate's paths_for_pr), a hunk of
    whose file (`hunks`: path -> base-to-PR hunks) touches its span, and
    which `replies` (parse_fix_replies) says is `fixed`. In findings order."""
    kept = {nfc(path) for path in kept}
    hunks = {nfc(path): value for path, value in hunks.items()}
    return tuple(
        FixRecord(f.principle_id, None, TIER1, f.path, None, AGENT, base,
                  span=(f.line, f.end_line))
        for f in findings
        if nfc(f.path) in kept and replies.get(finding_number(f.id)) == "fixed"
        and any(touches(h, f.line, f.end_line) for h in hunks.get(nfc(f.path), ())))


def record_tier1_fixes(sandbox, gate, fix_run):
    """The TIER1 FixRecords of the PR the gate left in `sandbox`: after the
    gate and before the patch, beside fix_counts.record_fixes, whose
    records these join before fix_counts.ledger_fields. `gate` is the
    GateResult, `fix_run` the fix step's FixStepRun (its reply). Empty when
    the gate kept nothing or there were no findings. GateError if a kept
    path no longer holds what was tested; ScopeCheckError if the sandbox
    is not one or was not linted."""
    require_sandbox(sandbox, ScopeCheckError)
    if sandbox.work_list is None:
        raise ScopeCheckError("no work list was linted in this sandbox at its base")
    findings = tuple(sandbox.work_list.tier1)
    if not findings or not gate.paths_for_pr:
        return ()
    # verify_kept before and after reading the diff, as fix_counts.relint
    # does: the hunks read must be of the bytes the green run tested, and a
    # process the tests left running could rewrite a kept file meanwhile.
    gate.verify_kept(sandbox)
    kept = set(gate.paths_for_pr)
    hunks = {}
    for change in changes_since(sandbox.path, sandbox.base):
        if change.path not in kept or change.status == DELETED or change.unverifiable:
            continue
        hunks[change.path] = (change.hunks if change.hunks is not None
                              else compute_hunks(change.old_text or "", change.new_text))
    gate.verify_kept(sandbox)
    payload = fix_run.payload() or {}
    return tier1_fix_records(findings, hunks, kept=kept,
                             replies=parse_fix_replies(payload.get("result")),
                             base=sandbox.base)


# --- the PR ----------------------------------------------------------------------------

def trim_pr_paths(paths, work, cap):
    """At most `cap` of `paths` (the verdict's), the lint's paths first:
    rule fixes take precedence over tier-1 fixes when the PR is trimmed to
    `max_files_per_pr` (E3-R3). Sorted."""
    lint = {item.path for item in work.items}
    ordered = sorted(paths, key=lambda path: (path not in lint, path))
    return tuple(sorted(ordered[:max(0, cap)]))


def tier1_pr_lines(records, findings):
    """The PR body's line for each TIER1 record of `records` (E3-R3: each
    fix with its principle id): principle, that it is tier 1, where, and
    the scan's note. cm-fkd.4 puts them in the body, which it redacts
    (E0-R7). The note is the agent's text, kept on one line and without
    backticks."""
    by_place = {(f.path, f.span): f for f in findings}
    lines = []
    for record in records:
        if record.source != TIER1:
            continue
        finding = by_place.get((record.path, record.span))
        note = finding.note.replace("`", "'") if finding else ""
        lines.append(f"- `{record.principle_id}` (tier 1: no rule yet) "
                     f"`{record.path}:{record.span[0]}-{record.span[1]}`"
                     + (f": {note}" if note else ""))
    return lines
