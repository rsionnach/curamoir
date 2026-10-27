"""E3-R3: the sweep's one PR, as text: its branch name, title, commit
message and body. Pure: no git, no `gh`, no files (cm-fkd.11; the runner,
cm-fkd.14, pushes and opens what this module writes).

E3-R3: "Output is exactly one PR per sweep, capped at `max_files_per_pr`,
on a `curamoir/sweep-<date>` branch. PR body MUST list each fix with its
principle id, and include a 'promotion candidates' section." The inputs are
the runner's own objects, as fix_scope's "What the runner does" names
them; final_body's docstring maps each argument to its object.

**What the body holds.** _compose is the source of truth for the layout;
in order:

1. The marker (below), alone on the first line.
2. `## curamoir sweep`, then one sentence: N fixes of M principles, the
   base, how many files, and that the ledger line records every fix.
3. `### Fixes`: only the fixes in the gate's `paths_for_pr` (pr_records;
   fix_scope step 9), each with its principle id. A rule fix is
   `principle` `rule` `path` line N: the line is the base lint's, joined by
   fingerprint, and the place is pr_markdown.location, never `path:N`,
   which redaction can erase when the file name holds a word like `token`.
   A fix that was the runner's remedy (a deletion), not the agent's edit,
   says so. A tier-1 fix is the line tier1.tier1_pr_lines gives.
4. `### Test gate`: GateResult.render() for a green gate; for a reduced
   one its summary without the dropped paths (render() lists them all on
   a line that is never cut), then each dropped file with its reason.
5. `### Files left for the next run`: those trimmed past
   `max_files_per_pr` and those the gate dropped (E3-R3's "rest", which the
   next sweep, at the next base, picks up).
6. `### Remedies skipped`: each remedy not applied, with its reason.
7. `### Promotion candidates`: PROMOTION_PENDING (E4-R1/E4-R2 have not
   landed).
8. `### Grade`: the E7-R5 hook, the caller's `grade_section` (escaped
   line by line, pr_markdown.plain_lines), else GRADE_PENDING.

A PR whose files carry no fix (paths_for_pr not empty, pr_records empty)
has no body or title: ValueError, and cm-fkd.14 records a failure with no
PR. So do contradictory inputs: a path both left out and in the PR, two
rule records of one fingerprint.

**The marker** (idempotence.sweep_marker, cm-fkd.8) is the body's FIRST
line, on its own. WHY first: redaction's private-key rule replaces a key
with no END line through to the end of the text, and truncation cuts lists
that come after it; nothing either touches can come before the first line.
Every other line, the caller's `grade_section` included, is neutralised:
idempotence.MARKER_WORD has its hyphen replaced by U+2011 (a non-breaking
hyphen, which reads the same), because parse_sweep_marker reads any other
line naming the word as a forged marker and gives up, which would make the
sweep's own PR one no later sweep could place; and every `<!--` is broken
up (pr_markdown.inert_comments), so repository text cannot open an HTML
comment that hides the rest of the body. Every path, rule id and
principle id is a pr_markdown.code_span, which a backtick in it cannot
close, and every piece of repository or agent text (those, the drop and
skip reasons, the tier-1 notes, the gate line) is pr_markdown.one_line:
redacted first, then a line break (git allows one in a file name), a
control or a format character (a bidi override) is shown as a visible
escape, so no text can start a line, a section or a forged marker of its
own. Text outside a code span (reasons, notes, the gate line) is
pr_markdown.plain_text besides, and the grade section pr_markdown.plain_lines
(the same, line by line): Markdown's live syntax is written as an HTML
entity, so it reads the same and does nothing. `&` is `&amp;` first (an
entity already in the text, `&#64;name`, would be decoded into a live
mention), `<` is `&lt;` (`<h3>` would render as a heading no `#` check
sees), `[` is `&#91;` (no link or image), and GitHub's autolinks, mentions
and references are broken: `://` is `&#58;//`, `www.` is `www&#46;`, `@`
is `@&#8203;`, `#` before a digit is `#&#8203;`, `GH-` before a digit is
`GH&#8203;-` (a bare URL or e-mail address would be a live link, `@name`
would notify that user, and `org/repo#123` would backlink this PR from
another repository: a tier-1 note is model text the repository steers; a
zero-width space, not `&#64;`, because GitHub finds mentions after
decoding entities). Behind that, final_body checks the body's headings are
exactly its own (_check_headings), and the grade's, ATX and setext, before
it is escaped (_check_grade_headings).

**The cap.** GitHub refuses a body over 65,536 characters. MAX_BODY_SIZE
is a little under it, measured by github_size: UTF-8 bytes, which are never
fewer than characters however GitHub counts them (code points or UTF-16
units), plus one per newline (and lone carriage return) in case GitHub
counts a line break as CRLF.
So the cap holds whatever the text. Over it, the lists are cut to their
first k lines each, with a `... and N more` line, k as large as a checked
search finds (_fitted). One k serves every list, so the fix list, the long
one, is the one that loses lines, and a huge list of files left out cannot
crowd it out alone. The marker, the gate's line and the fixed sections are
never cut; if they alone do not fit (a grade section of 60 KB),
SweepBodyError, naming the largest of them.

**Redaction** (E0-R7) happens piece by piece, before composing: every
piece of repository or agent text goes through pr_markdown.one_line (via
code_span, location or plain_text), which redacts it first, and the
caller's `grade_section`, which is multi-line, is redacted whole by
pr_markdown.plain_lines before its lines are escaped. The rest of the body is this module's own text. So the composed
body is the text GitHub receives, and the cap is checked on it (redaction
can lengthen text: `token=abc12345` becomes `token=[REDACTED]`).

final_body does NOT redact the composed body again: that is not
idempotent after plain_text, which writes a placeholder's `[` as `&#91;` so
it opens no link. Whole-body redaction would read the escaped placeholder
as a fresh value and rewrite it to `[REDACTED]`, undoing the escape, and an
escape can lengthen a sub-floor value past redaction's 8-character floor
(`token=ab\\x01cd"(https://...)` became the live link
`token=[REDACTED](https://...)`). Instead the backstop VERIFIES
(_check_redacted): with the body read as GitHub shows it (_rendered: every
entity plain_text writes read back as its character), redacting it must
change nothing; otherwise SweepBodyError (fail closed: a piece that
escaped redaction, or text an escape turned into something redaction would
match, is not sent). Then
final_body refuses (SweepBodyError) unless parse_sweep_marker of the body
is the base, and checks its headings. render_pr_body is the same body
without those checks, for tests and inspection only.

**Title and commit message** (decided here). The title is
`curamoir sweep YYYY-MM-DD: N fixes of M principles`: the date the branch
carries and the counts a reviewer triages by, nothing that needs
redacting. The commit message is the title, a blank line, `Base: <sha>`,
a blank line, and `principle: count` per principle (sorted): `git log`
then says what a sweep commit fixed without the PR. Both count only
pr_records. The ASCII spelling `curamoir` matches the branch and the
command, not the prose name. The date, for the title and the branch, is
exactly a datetime.date, the UTC day the caller decides; a datetime is
refused rather than read in some time zone.

**The branch**: `<guardrails.branch_prefix>sweep-YYYY-MM-DD-<base[:8]>`
(user decision, 2026-10-23), so idempotence.sweep_branch_prefix of the
configured prefix, which the open-PR cap and the duplicate check match,
always matches it. The prefix is checked by that function; the base must be
a full lowercase SHA, 40 hex digits or 64 (a SHA-256 repository, which
sweep_marker and pr_field also accept).

Syntax that imports on 3.9, like sweep.py.
"""

import re
from datetime import date as Date
from typing import NamedTuple

from curamoir.fix_counts import REMEDY, RULE, TIER1
from curamoir.fix_scope import nfc
from curamoir.green_gate import REDUCED
from curamoir.idempotence import (
    MARKER_WORD,
    parse_sweep_marker,
    sweep_branch_prefix,
    sweep_marker,
)
from curamoir.pr_markdown import code_span, inert_comments, location, plain_lines, plain_text
from curamoir.redaction import redact
from curamoir.tier1 import tier1_pr_lines

__all__ = ["GRADE_PENDING", "MAX_BODY_SIZE", "PROMOTION_PENDING", "SweepBodyError",
           "commit_message", "final_body", "github_size", "pr_records", "pr_title",
           "render_pr_body", "sweep_branch"]

#: The body's cap, as github_size measures it (module docstring, "The
#: cap"): under GitHub's 65,536-character limit, with room to spare.
MAX_BODY_SIZE = 65_000

#: The grade hook's text until Epic 7 passes a section (E7-R5).
GRADE_PENDING = "not graded yet (Epic 7)"

#: The promotion candidates section until E4-R1/E4-R2 fill it (E3-R3).
PROMOTION_PENDING = ("None yet: promotion candidates come from the promotion ladder "
                     "(E4-R1, E4-R2), which has not landed.")

#: MARKER_WORD's inert spelling, for every line but the marker (module
#: docstring, "The marker").
_INERT_WORD = MARKER_WORD.replace("-", "\u2011")

#: The name the title and the body's heading open with.
_NAME = "curamoir sweep"


class SweepBodyError(ValueError):
    """The body cannot be written: its marker would not be read back, or
    what may not be cut does not fit under the cap. A ValueError, so a
    caller catching ValueError catches it too."""


def github_size(text):
    """`text`'s size as the cap measures it: UTF-8 bytes plus one per
    newline and per lone carriage return (a line break GitHub may count as
    CRLF). Never less than the character count GitHub limits, however it
    counts (module docstring, "The cap")."""
    lone_returns = text.count("\r") - text.count("\r\n")
    return len(text.encode("utf-8")) + text.count("\n") + lone_returns


def sweep_branch(prefix, date, base):
    """The sweep PR's branch: `<prefix>sweep-YYYY-MM-DD-<base[:8]>`.
    ValueError for a prefix sweep_branch_prefix refuses, a `date` that is
    not a date, or a `base` that is not a full lowercase SHA."""
    _check_base(base)
    return f"{sweep_branch_prefix(prefix)}{_day(date)}-{base[:8]}"


def pr_records(records, gate):
    """The records whose path is in `gate.paths_for_pr` (compared in NFC,
    as tier1 compares them), in their order: the fixes the PR carries."""
    kept = {nfc(path) for path in gate.paths_for_pr}
    return tuple(record for record in records if nfc(record.path) in kept)


def pr_title(records, gate, date):
    """`curamoir sweep YYYY-MM-DD: N fixes of M principles`, counting
    pr_records. ValueError when none of them lands in the PR's files (no
    PR is opened for that: _carried)."""
    return f"{_NAME} {_day(date)}: {_counted(_carried(records, gate))}"


def commit_message(records, gate, date, base):
    """The sweep commit's message: the title, the base, and each
    principle's count of pr_records (module docstring)."""
    _check_base(base)
    counts = _per_principle(_carried(records, gate))
    lines = [pr_title(records, gate, date), "", f"Base: {base}", ""]
    lines += [f"{principle}: {count}" for principle, count in counts.items()]
    return "\n".join(lines) + "\n"


def render_pr_body(records, work_list, gate, base, skipped_remedies, left_out_paths,
                   grade_section=None, limit=MAX_BODY_SIZE):
    """final_body's body without its checks (the redaction verification,
    the marker, the headings): the same arguments, the same raises
    otherwise, fitted under `limit`. For inspection and tests, never for
    sending."""
    parts = _parts(records, work_list, gate, base, skipped_remedies, left_out_paths,
                   grade_section)
    return _body(parts, lambda text: github_size(text) <= limit)


def final_body(records, work_list, gate, base, skipped_remedies, left_out_paths,
               grade_section=None, limit=MAX_BODY_SIZE):
    """The PR body the runner sends: rendered from redacted pieces (E0-R7),
    fitted under `limit`, then checked: redaction would change nothing
    (_check_redacted), its marker reads back, its headings are its own.

    Args (fix_scope's "What the runner does"):
        records: every FixRecord of the sweep, rule (fix_counts.record_fixes)
            and tier-1 (tier1.record_tier1_fixes), unfiltered: pr_records
            keeps those in `gate.paths_for_pr`.
        work_list: `sandbox.work_list`, the WorkList the records were found
            in. Its `items` place each rule record by fingerprint; its
            `tier1` supplies the findings tier1_pr_lines reads.
        gate: the green_gate.GateResult (green or reduced).
        base: `sandbox.base`, a full lowercase SHA.
        skipped_remedies: remedies.RemedyResult.skipped.
        left_out_paths: the verdict's paths trim_pr_paths did not keep
            (past `max_files_per_pr`).
        grade_section: E7-R5's text, or None for GRADE_PENDING. Redacted
            whole, then each line escaped as plain_text escapes it
            (pr_markdown.plain_lines: no HTML, links, autolinks or
            mentions), then neutralised like every other line (module
            docstring, "The marker"). The E7-R5 contract: plain text, with
            no Markdown headings and no fenced block holding `#` lines; a
            grade with a heading of its own is refused
            (_check_grade_headings, _check_headings).
        limit: the cap, by github_size. A test seam; the runner leaves it.

    Raises:
        ValueError: the gate kept no file (abandoned: there is no PR); no
            fix lands in the PR's files (a PR of edits that fixed nothing
            is not opened; cm-fkd.14 records a failure with no PR); a
            record is of another base, or two rule records share a
            fingerprint; a rule record's fingerprint is not in
            `work_list.items`; a path in `left_out_paths` is also in
            `gate.paths_for_pr`; or `base` is not a full SHA.
        SweepBodyError (a ValueError): redacting the body would still
            change it (_check_redacted; module docstring, "Redaction");
            the marker does not read back as `base`, or the body's headings are not exactly
            its own (_check_headings: a redaction that ate sections), or
            `grade_section` has headings of its own (_check_grade_headings); or the parts that
            are never cut do not fit under `limit` (the message names the
            largest).
    """
    _check_grade_headings(grade_section)
    parts = _parts(records, work_list, gate, base, skipped_remedies, left_out_paths,
                   grade_section)
    body = _body(parts, lambda text: github_size(text) <= limit)
    _check_redacted(body)
    if parse_sweep_marker(body) != base:
        raise SweepBodyError(f"the body's sweep marker does not read back as {base}, so no "
                             "later sweep could tell this PR's base")
    _check_headings(body)
    return body


#: Every HTML entity pr_markdown's plain_text (and plain_lines) writes, and
#: the character it stands for (the zero-width space after `@`, `#` and
#: `GH` is read as nothing, as a reader reads it; `&amp;` is read as `&`, and
#: in one pass, so `&amp;#64;` reads back as `&#64;`, never as `@`). MUST
#: stay in step with pr_markdown._inline:
#: an entity missing here makes the check read text longer than a reader
#: sees (`token=ab&#58;//cd` is over redaction's 8-character floor where
#: `token=ab://cd` is not), which refuses an innocent PR.
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&#91;": "[", "&#58;": ":", "&#46;": ".",
             "&#8203;": ""}

_ENTITY = re.compile("|".join(map(re.escape, _ENTITIES)))


def _rendered(body):
    """`body` as GitHub shows it, as far as redaction can tell: each entity
    in _ENTITIES read back as its character, in one pass (so a decoded `&`
    never starts another entity)."""
    return _ENTITY.sub(lambda match: _ENTITIES[match.group()], body)


def _check_redacted(body):
    """SweepBodyError unless redacting `body` as rendered would change
    nothing (module docstring, "Redaction"). A check, never a rewrite: a
    rewrite would undo plain_text's escapes. WHY the rendered text and not
    the escaped one: `&lt;` is 3 characters longer than the `<` a reader
    sees, so a note like `logs session_token=<sid>` would otherwise read as
    a secret and refuse the whole PR."""
    view = _rendered(body)
    if redact(view) != view:
        raise SweepBodyError("redaction would still change the composed body (a piece that "
                             "was not redacted, or text an escape made look like a secret), "
                             "so the body is not sent")


#: Every heading the body has, in order: _compose's, and no other.
_HEADINGS = (f"## {_NAME}", "### Fixes", "### Test gate", "### Files left for the next run",
             "### Remedies skipped", "### Promotion candidates", "### Grade")

#: A CommonMark ATX heading line.
_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")

#: A CommonMark setext heading underline: under a paragraph line, it makes
#: that line an H1 (`===`) or an H2 (`---`), which no `#` check sees.
_SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)\s*$")


def _check_grade_headings(grade_section):
    """SweepBodyError when `grade_section` holds a Markdown heading (E7-R5's
    contract: plain text): an ATX line (`### x`), or a setext underline
    (`===`, `---`) under a non-blank line, which would make that line a
    heading. Checked on the text as given, BEFORE plain_lines escapes it:
    an escape can turn a heading into a line that is not one (`#` then a
    tab becomes `#\\t`), and such a grade broke the contract all the same.
    _check_headings then checks the composed body (ATX only). The error
    names line NUMBERS, never the lines: they are not redacted yet, and the
    runner records the error (cm-fkd.14)."""
    if grade_section is None:
        return
    lines = str(grade_section).splitlines()
    found = [number for number, line in enumerate(lines, 1)
             if _HEADING.match(line)
             or (number > 1 and lines[number - 2].strip() and _SETEXT_UNDERLINE.match(line))]
    if found:
        raise SweepBodyError(f"the grade section has headings of its own (line(s) {found}); "
                             "E7-R5's grade is plain text under the body's `### Grade`, so "
                             "the body is not sent")


def _check_headings(body):
    """SweepBodyError unless the body's heading lines are exactly _HEADINGS,
    in order. Defence in depth behind one_line: repository text cannot add
    a section, and a redaction that ate sections (a private key with no END
    line runs to the end of the text) cannot pass for a whole body."""
    found = tuple(line for line in body.splitlines() if _HEADING.match(line))
    if found != _HEADINGS:
        raise SweepBodyError(f"the body's headings are {list(found)!r}, not its own "
                             f"{list(_HEADINGS)!r}: a section was added or lost (redaction "
                             "can eat to the end of the text), so the body is not sent")


# --- the body's parts ------------------------------------------------------------------

class _Listed(NamedTuple):
    """One list of the body: its `lines`, the line shown when there are
    none (`empty`), and what the `... and N more` line adds after `more`
    when it is cut (`more_suffix`)."""

    lines: list[str]
    empty: str
    more_suffix: str = ""


def _parts(records, work_list, gate, base, skipped_remedies, left_out_paths, grade_section):
    """The body's pieces, a dict _compose assembles: "marker", "header",
    "gate" and "grade" (text never cut), and "lists", {name: _Listed} (the
    lists _fitted may cut). Raises as final_body's docstring says."""
    marker = sweep_marker(base)  # ValueError unless `base` is a full lowercase SHA
    if not gate.paths_for_pr:
        raise ValueError(f"the test gate kept no file ({gate.render()}), so there is no PR "
                         "and no body")
    strays = [record for record in records if record.base != base]
    if strays:
        raise ValueError(f"{len(strays)} fix record(s) are of another base than {base}")
    fingerprints = [r.fingerprint for r in records if r.source == RULE]
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("two rule fix records share a fingerprint: one violation is "
                         "fixed once")
    kept = {nfc(path) for path in gate.paths_for_pr}
    overlap = sorted(path for path in left_out_paths if nfc(path) in kept)
    if overlap:
        raise ValueError(f"{len(overlap)} path(s) are both in the PR and left out of it "
                         f"(first: {overlap[0]!r})")
    carried = _carried(records, gate)
    by_fingerprint = {item.fingerprint: item for item in work_list.items}
    unknown = [r for r in carried if r.source == RULE and r.fingerprint not in by_fingerprint]
    if unknown:
        raise ValueError(f"{len(unknown)} rule fix record(s) name a fingerprint the work list "
                         f"does not hold (first: {unknown[0].fingerprint}), so the fix "
                         "cannot be placed: pass the work list the records were found in")
    fixes = [_rule_line(record, by_fingerprint[record.fingerprint])
             for record in carried if record.source == RULE]
    fixes += tier1_pr_lines([r for r in carried if r.source == TIER1], work_list.tier1)
    dropped = [f"- {code_span(drop.path)}: {plain_text(drop.reason)}" for drop in gate.dropped]
    left = [f"- {code_span(path)} (past `max_files_per_pr`)" for path in left_out_paths]
    # A dropped path is listed twice on purpose: under "Test gate" with its
    # reason, and here, because the next sweep picks it up like the rest.
    left += [f"- {code_span(drop.path)} (dropped by the test gate)" for drop in gate.dropped]
    skipped = [f"- {code_span(s.path)} ({code_span(s.rule_id)}, remedy "
               f"{code_span(s.remedy)}): {plain_text(s.reason)}" for s in skipped_remedies]
    files = len(gate.paths_for_pr)
    header = (f"{_counted(carried)} at base `{base}`, in {files} "
              f"file{'s' if files != 1 else ''}. Each fix is listed with its principle id; "
              "the sweep's ledger line records every one.")
    return {
        "marker": marker,
        "header": header,
        "gate": _gate_line(gate),
        # Redacted whole (a key with no END line must stop at its end), then
        # escaped line by line (plain_lines); _compose neutralises it like
        # every line.
        "grade": GRADE_PENDING if grade_section is None else plain_lines(grade_section),
        "lists": {
            "fixes": _Listed(fixes, "None.",
                            " (the ledger line lists every fix)"),
            "dropped": _Listed(dropped, "Nothing was dropped."),
            "left": _Listed(left, "None."),
            "skipped": _Listed(skipped, "None."),
        },
    }


def _body(parts, fits):
    """_compose of `parts` with the lists cut as far as `fits` needs (_fitted)."""
    longest = max(len(listed.lines) for listed in parts["lists"].values())
    try:
        return _fitted(lambda keep: _compose(parts, keep), longest, fits)
    except SweepBodyError as error:
        sizes = {"header": parts["header"], "test gate line": parts["gate"],
                 "grade section": parts["grade"]}
        name = max(sizes, key=lambda key: github_size(sizes[key]))
        raise SweepBodyError(f"{error}; the largest part never cut is the {name} "
                             f"({github_size(sizes[name])})") from None


def _compose(parts, keep):
    """The body (the layout's source of truth; module docstring) with each
    list cut to its first `keep` lines. Every line but the marker has
    MARKER_WORD made inert and `<!--` broken up (module docstring, "The
    marker")."""
    def listed(name):
        lines, empty, more_suffix = parts["lists"][name]
        if not lines:
            return [empty]
        if len(lines) <= keep:
            return list(lines)
        return [*lines[:keep], f"- ... and {len(lines) - keep} more{more_suffix}"]

    content = [
        f"## {_NAME}", "", parts["header"], "",
        "### Fixes", *listed("fixes"), "",
        "### Test gate", parts["gate"], *listed("dropped"), "",
        "### Files left for the next run", *listed("left"), "",
        "### Remedies skipped", *listed("skipped"), "",
        "### Promotion candidates", PROMOTION_PENDING, "",
        "### Grade", parts["grade"],
    ]
    text = inert_comments("\n".join(content).replace(MARKER_WORD, _INERT_WORD))
    return f"{parts['marker']}\n{text}\n"


def _fitted(compose, longest, fits):
    """compose(keep) for `keep` as large as a checked search finds in
    0..`longest` whose text `fits`: all of it when that fits, else a binary
    search. The size is not monotone in `keep` (a `... and N more` line can
    be longer than the line it stands for), so the search only ever returns
    a `keep` it has checked fits, and when 1 does not fit tries 0 before
    giving up (SweepBodyError)."""
    whole = compose(longest)
    if fits(whole):
        return whole
    if longest < 1 or not fits(compose(1)):
        if fits(compose(0)):
            return compose(0)
        raise SweepBodyError("the body does not fit under the cap even with every list cut")
    low, high = 1, longest  # compose(low) fits; compose(high) does not
    while high - low > 1:
        middle = (low + high) // 2
        if fits(compose(middle)):
            low = middle
        else:
            high = middle
    return compose(low)


def _gate_line(gate):
    """The gate's line for the body: GateResult.render() for green, and for
    reduced its summary without the dropped paths, which the cut lists
    below carry (render() lists them all on one line, which is never cut,
    so a gate that dropped thousands would not fit)."""
    if gate.strategy == REDUCED:
        return (f"tests red with every fix; reduced to {len(gate.kept)} fixed file(s) that "
                f"are green, dropping the {len(gate.dropped)} listed below "
                f"({len(gate.runs)} runs)")
    return plain_text(gate.render())


def _carried(records, gate):
    """pr_records, or ValueError when it is empty: a PR whose files carry
    no fix is not opened."""
    carried = pr_records(records, gate)
    if not carried:
        raise ValueError("no fix lands in this PR's files: the edits fixed nothing the lint "
                         "or the scan reported, so no PR is opened")
    return carried


def _rule_line(record, item):
    line = (f"- {code_span(record.principle_id)} {code_span(record.rule_id)} "
            f"{location(record.path, item.line)}")
    if record.fixed_by == REMEDY:
        line += " (deleted by the runner's remedy, not edited)"
    return line


# --- small helpers ---------------------------------------------------------------------

def _per_principle(records):
    counts = {}
    for record in records:
        counts[record.principle_id] = counts.get(record.principle_id, 0) + 1
    return dict(sorted(counts.items()))


def _counted(records):
    fixes, principles = len(records), len(_per_principle(records))
    return (f"{fixes} fix{'es' if fixes != 1 else ''} of {principles} "
            f"principle{'s' if principles != 1 else ''}")


def _day(date):
    """YYYY-MM-DD of `date`, exactly a datetime.date: the caller passes the
    UTC day. A datetime is refused, so a time zone is never guessed here."""
    if type(date) is not Date:
        raise ValueError(f"a sweep's date must be a datetime.date (the UTC day), not "
                         f"{date!r}")
    return date.strftime("%Y-%m-%d")


def _check_base(base):
    """ValueError unless `base` is a full lowercase SHA. The check is
    sweep_marker's own, so the branch, the commit message and the marker
    can never disagree on what a base is; the marker line is discarded."""
    sweep_marker(base)
