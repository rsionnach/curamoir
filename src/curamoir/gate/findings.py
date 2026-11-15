#!/usr/bin/env python3
"""The shared R5 Finding contract, and the merge of one review round.

The contract is section 11 of docs/r5-gate-v2-curamoir-spec.md, shared word
for word with NthLayer; this module is its reference implementation (the
spec calls it normative: where the spec's text and this code disagree, the
code is right and the text is fixed). It was lifted from the bridge's
``.claude/hooks/lib/r5_findings.py`` (cm-8ap.13), which now imports it.

A reviewer's LAST non-empty output line is ``R5-FINDINGS:`` followed by a JSON
array of findings. Lines end at LF, with a CR before it tolerated; U+2028,
U+2029 and every other Unicode separator are content, so one inside a claim
does not split the line.

    findings.py parse <lens> <report-file> [--floor <level>] [--prior <file>]

``--prior`` is a JSON file holding the previous round's open set for this lens
(a list of findings; none on a first review). It prints one JSON object:

- ``findings``: every record in the report, normalised;
- ``open_set``: the findings still open after this round, which the caller
  hands back as ``--prior`` next time;
- ``counts`` (critical, important, minor) and ``unproven`` (blocking findings
  whose verification_level is below the floor), both over the open set;
- ``closures`` (the prior fingerprints validly closed), ``closure_rejected``
  (each rejected closure with its reason), ``unaccounted`` (prior open
  findings the report neither closed nor re-raised) and
  ``downgrades_refused`` (re-raises held at the prior, higher severity);
- ``prose_unfiled``: items the prose names under a severity heading with no
  matching entry, and ``Not filed`` lines with no reason (telemetry only).

Closures, per section 11. A record with ``resolves`` set is a closure, not a
finding: it is never counted, whatever its severity, and its path, lines and
evidence are the resolving lines. It is valid only if ``resolves`` names a
fingerprint in the previous round's open set and its evidence is non-empty;
an invalid closure is ignored and rejected with the reason, and a second
closure of the same fingerprint is dropped. The closure's own fingerprint is
never compared to ``resolves``. A re-raised finding is the prior record
copied verbatim (path and claim, so the same fingerprint) with no
``resolves``; a report that re-raises a finding and also closes it has that
closure rejected and the re-raise kept. The open set is the previous open set
minus valid closures, plus new and re-raised findings; a prior open finding
with neither a closure nor a re-raise is carried forward unchanged, tagged
``unaccounted``: silence never closes. A re-raise keeps the more severe of
its own and the prior finding's severity (``downgrades_refused`` lists those
it held up): severity goes down only through a valid closure. A report that
closes every prior finding with empty evidence and re-raises none of them is
a contract failure (R5-R25).

Fingerprints, per section 11: a stored fingerprint is a cache, never a
comparison key. Every comparison with the prior open set recomputes the key
from lens, path and claim with the current normaliser; a closure's
``resolves`` may name either that key or the prior finding's stored
fingerprint (what the brief published). The open set carries the recomputed
fingerprint (cm-5ao).

Corpus findings (a replay entry's ``recorded.json``) are read by a second
code path, ``corpus_finding`` and ``load_recorded``: a null path or line
range is allowed there, every key is kept as it was, and the stored
fingerprint is never rewritten, so the corpus round-trips losslessly.
Reviewer output still requires path, start_line and end_line (spec v1.2).

Exit 0; 2 if the report does not meet the contract (the reason on stderr:
the caller records the reviewer UNAVAILABLE, R5-R9); 1 on a usage error.

Standard library only, and Python 3.9 compatible: the bridge runs it under
whatever python3 the session finds (macOS ships 3.9).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from typing import Any

PREFIX = "R5-FINDINGS:"
LENSES = ("correctness", "clarity", "edge_cases", "provenance", "excellence")
SEVERITIES = ("CRITICAL", "IMPORTANT", "MINOR")
# The E5-R11 ladder, weakest first.
LEVELS = ("asserted", "cited", "walked", "script", "reproduced")
DEFAULT_FLOOR = "script"
BLOCKING = ("CRITICAL", "IMPORTANT")
# The fixer's per-finding statuses (section 11; set by the fixer, never by a
# reviewer).
STATUSES = ("open", "fixed", "superseded", "could_not_fix", "disputed", "fixed_in_passing")

# The severity policy per lens (R5-R12). Blocking is CRITICAL or IMPORTANT
# from these lenses only. Clarity and excellence are advisory: they never
# block, and they have nothing to prove.
BLOCKING_LENSES = ("correctness", "edge_cases", "provenance")
ADVISORY_LENSES = ("clarity", "excellence")
# The fix list's lens order (R5-R14), after severity. Excellence never joins
# the fix list (R5-R27); it is last so a merged entry sorts deterministically.
LENS_PRIORITY = ("correctness", "edge_cases", "provenance", "clarity", "excellence")

# Removed from a claim before it is fingerprinted, so a finding keeps its key
# when the code it points at moves: line references (line 42, line ~42,
# lines 4-9, :42, :4-9, L42) and SHA-like tokens (7 to 40 hex characters with
# at least one digit, so a word like "defaced" stays). Other numbers stay:
# "exits 2" and "exits 4" are different claims.
_LINE_REF = re.compile(r"\blines?\s+~?\d+(?:\s*[-\u2013]\s*~?\d+)?\b|:\d+(?:-\d+)?\b|\bl\d+\b")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_SPACE = re.compile(r"\s+")
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
# What counts as blank at the ends of an output line: ASCII whitespace only,
# since every Unicode separator is content (section 11).
_BLANK = " \t\r\f\v"


class ReportError(Exception):
    """The report does not meet the section 11 contract."""


class PriorError(Exception):
    """The previous round's open set is malformed (the caller's fault, not the reviewer's)."""


def schema_lens(name: str) -> str:
    """Pass names say ``edge-cases``; the schema says ``edge_cases``."""
    return name.replace("-", "_")


def normalise_claim(claim: str) -> str:
    """Lower-case, line references and SHAs removed, whitespace collapsed."""
    text = claim.lower()
    text = _LINE_REF.sub(" ", text)
    text = _SHA.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def fingerprint(lens: str, path: str, claim: str) -> str:
    """First 16 hex of sha256(lens|path|normalised claim)."""
    key = f"{lens}|{path}|{normalise_claim(claim)}"
    try:
        data = key.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, from a JSON \ud800 escape
        raise ReportError("a path or claim is not valid Unicode") from None
    return hashlib.sha256(data).hexdigest()[:16]


def key(finding: dict) -> str:
    """A finding's comparison key, recomputed (a stored fingerprint is a
    cache). A corpus finding with no path is keyed with an empty one."""
    return fingerprint(finding["lens"], finding.get("path") or "", finding["claim"])


def output_lines(text: str) -> list:
    """The report's lines: split at LF only, one CR before it dropped."""
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def _last_line(text: str) -> str:
    for line in reversed(output_lines(text)):
        if line.strip(_BLANK):
            return line.strip(_BLANK)
    raise ReportError("the report is empty")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unicode(value: str, key: str, n: int) -> str:
    """The value, if it can be written as UTF-8 (a JSON \\ud800 escape cannot)."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ReportError(f"finding {n}: {key} is not valid Unicode") from None
    return value


def _text(finding: dict, key: str, n: int) -> str:
    value = finding.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"finding {n}: {key} must be a non-empty string")
    return _unicode(value, key, n)


def _optional_text(finding: dict, key: str, n: int) -> None:
    value = finding.get(key)
    if value is not None and not isinstance(value, str):
        raise ReportError(f"finding {n}: {key} must be a string or null")
    if value is not None:
        _unicode(value, key, n)


def _resolves(raw: dict, n: int) -> Any:
    resolves = raw.get("resolves")
    # A copied fingerprint is forgiven surrounding space and upper case; a
    # value that cannot be a fingerprint at all is a malformed record, not a
    # closure to reject: it would otherwise turn a finding into an ignored one.
    if isinstance(resolves, str):
        resolves = resolves.strip().lower()
    if resolves is not None and not (isinstance(resolves, str) and _FINGERPRINT.fullmatch(resolves)):
        raise ReportError(f"finding {n}: resolves must be a 16-hex fingerprint or null")
    return resolves


def _common(raw: Any, n: int) -> None:
    """The checks shared by reviewer output and corpus findings."""
    if not isinstance(raw, dict):
        raise ReportError(f"finding {n} is not an object")
    if raw.get("severity") not in SEVERITIES:
        raise ReportError(f"finding {n}: severity must be one of {', '.join(SEVERITIES)}")
    _text(raw, "claim", n)
    _optional_text(raw, "fix", n)
    if not isinstance(raw.get("regression", False), bool):
        raise ReportError(f"finding {n}: regression must be true or false")
    level = raw.get("verification_level")
    if level is not None and level not in LEVELS:
        raise ReportError(f"finding {n}: verification_level must be one of {', '.join(LEVELS)}")


def _finding(raw: Any, lens: str, n: int) -> dict:
    """One reviewer record, normalised: path and line range required."""
    _common(raw, n)
    if raw.get("lens", lens) != lens:
        raise ReportError(f"finding {n}: lens {raw.get('lens')!r} is not this reviewer's ({lens})")
    path = _text(raw, "path", n)
    start, end = raw.get("start_line"), raw.get("end_line")
    if not (_is_int(start) and _is_int(end) and 1 <= start <= end):
        raise ReportError(f"finding {n}: start_line and end_line must be integers with 1 <= start <= end")
    resolves = _resolves(raw, n)
    evidence = raw.get("evidence")
    if resolves is None:
        evidence = _text(raw, "evidence", n)
    elif evidence is None:
        # A closure without evidence is a contract matter for the round (it
        # is rejected, or the reviewer UNAVAILABLE), not a malformed record.
        evidence = ""
    elif not isinstance(evidence, str):
        raise ReportError(f"finding {n}: evidence must be a string")
    else:
        _unicode(evidence, "evidence", n)
    finding = {
        "lens": lens, "severity": raw["severity"], "path": path,
        "start_line": start, "end_line": end,
        "claim": raw["claim"], "evidence": evidence, "fix": raw.get("fix"),
        # Computed here, never taken from the reviewer: it is the merge key.
        "fingerprint": fingerprint(lens, path, raw["claim"]),
        "resolves": resolves, "regression": raw.get("regression", False),
        # Set by the fixer, never by a reviewer.
        "status": "open",
    }
    level = raw.get("verification_level")
    if level is not None:
        finding["verification_level"] = level
    return finding


def corpus_finding(raw: Any, n: int = 1) -> dict:
    """One corpus finding, checked and returned unchanged (a copy).

    The corpus path of spec v1.2: a null path, or a null line range, is
    allowed, because a recorded finding carries a location only where the
    reviewer's report gave one. Every other key (``derived_from``,
    ``fix_commit`` and the rest) is kept, nulls stay null, and the stored
    fingerprint is not rewritten, so the corpus stays frozen when the
    normaliser changes; compare with ``key``."""
    _common(raw, n)
    if raw.get("lens") not in LENSES:
        raise ReportError(f"finding {n}: lens must be one of {', '.join(LENSES)}")
    path = raw.get("path")
    if path is not None:
        _text(raw, "path", n)
    start, end = raw.get("start_line"), raw.get("end_line")
    if not ((start is None and end is None)
            or (_is_int(start) and _is_int(end) and 1 <= start <= end)):
        raise ReportError(f"finding {n}: start_line and end_line must both be null, "
                          "or integers with 1 <= start <= end")
    _optional_text(raw, "evidence", n)
    stored = raw.get("fingerprint")
    if stored is not None and not (isinstance(stored, str) and _FINGERPRINT.fullmatch(stored)):
        raise ReportError(f"finding {n}: fingerprint must be 16 hex characters or null")
    _resolves(raw, n)
    if raw.get("status") is not None and raw.get("status") not in STATUSES:
        raise ReportError(f"finding {n}: status must be one of {', '.join(STATUSES)} or null")
    return copy.deepcopy(raw)


def load_recorded(recorded: Any) -> dict:
    """A replay entry's ``recorded.json``, every finding checked in corpus
    mode; returned as an equal copy (``load_recorded(r) == r``)."""
    if not isinstance(recorded, dict):
        raise ReportError("recorded.json must hold a JSON object")
    rounds = recorded.get("rounds")
    if not isinstance(rounds, list):
        raise ReportError("recorded.json: rounds must be a list")
    n = 0
    for review in rounds:
        if not isinstance(review, dict):
            raise ReportError("recorded.json: each round must be an object")
        found = review.get("findings")
        if found is None:  # counts_only, or a round whose findings were not recorded
            continue
        if not isinstance(found, list):
            raise ReportError("recorded.json: a round's findings must be a list or null")
        for raw in found:
            n += 1
            corpus_finding(raw, n)
    for raw in recorded.get("cross_repo_findings") or ():
        n += 1
        corpus_finding(raw, n)
    return copy.deepcopy(recorded)


def _rank(finding: dict) -> tuple:
    """Lower is kept: the more severe, then the better proven (no level is
    "asserted", the bottom of the ladder)."""
    return (SEVERITIES.index(finding["severity"]),
            -LEVELS.index(finding.get("verification_level", "asserted")))


def _keep_most_severe(by_key: dict, key: str, finding: dict) -> None:
    """Store finding under key unless one more severe, or as severe and at
    least as well proven, is there."""
    kept = by_key.get(key)
    if kept is None or _rank(finding) < _rank(kept):
        by_key[key] = finding


def check_prior(prior_open: Any) -> None:
    """Raise PriorError unless the previous round's open set is a list of
    findings that can be keyed and ranked."""
    if not isinstance(prior_open, list):
        raise PriorError("the prior open set must be a list of findings")
    for f in prior_open:
        if not (isinstance(f, dict)
                and all(isinstance(f.get(k), str) for k in ("fingerprint", "lens", "path", "claim"))
                and f.get("severity") in SEVERITIES
                and f.get("verification_level", "asserted") in LEVELS):
            raise PriorError("the prior open set must be a list of findings")
    try:  # each is recomputed: a claim or path that is not valid Unicode
        for f in prior_open:
            fingerprint(f["lens"], f["path"], f["claim"])
    except ReportError as exc:
        raise PriorError(f"the prior open set: {exc}") from None


# ---- prose_unfiled (section 11, "Prose and entries"), as NthLayer's parser has it

NOT_FILED = "Not filed"
# A Not-filed line ends in one of these.
NOT_FILED_REASONS = ("out_of_scope", "pre_existing", "not_an_issue")


def _heading(line: str) -> Any:
    """The section a markdown heading opens: a severity, Not filed, or None."""
    words = line.lstrip("#").strip().rstrip(":").split()
    if len(words) == 1 and words[0].upper() in SEVERITIES:
        return words[0].upper()
    if [w.lower() for w in words] == ["not", "filed"]:
        return NOT_FILED
    return None


def _prose_items(text: str) -> dict:
    """Items in the report's prose, per severity heading and under Not filed.

    The lens templates list each issue as a top-level item (``1. **...**``,
    ``- **...**``, or a plain ``- ...`` bullet) under a ``Critical`` /
    ``Important`` / ``Minor`` heading, and each remark that is not a finding
    as a line under ``Not filed``; an item's text is its bold lead if it has
    one. Indented sub-bullets, fenced blocks and the contract line are
    skipped."""
    items = {s: [] for s in (*SEVERITIES, NOT_FILED)}
    current, fenced = None, False
    for raw in output_lines(text):
        line = raw.strip()
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.startswith(PREFIX):
            continue
        if line.startswith("#"):
            current = _heading(line)
            continue
        # A sub-bullet (``   - File: ...``) is indented two or more spaces.
        if current is None or not line or raw[:2].isspace():
            continue
        head, _, rest = line.partition(" ")
        if head in ("-", "*") or (head[:-1].isdigit() and head.endswith(".")):
            title = rest.strip()
            if title.startswith("**") and current != NOT_FILED:
                title = title[2:].split("**")[0]
            items[current].append(title.strip())
        else:
            # "None." or a plain prose line ("Checks performed:") closes the
            # section: a list after it is notes, not this heading's items.
            current = None
    return items


def _words(text: str) -> set:
    return set(normalise_claim(text).replace("`", " ").replace("*", " ").split())


def _has_reason(line: str) -> bool:
    # Trailing punctuation first, so "(not_an_issue)." ends in its reason.
    words = (line.rstrip(" .;,").replace("`", " ").replace("(", " ").replace(")", " ")
             .split())
    return bool(words) and words[-1].rstrip(".;,") in NOT_FILED_REASONS


def prose_unfiled(text: str, records: list) -> list:
    """Prose items with no matching entry, and Not-filed lines with no reason.

    Each item under a severity heading with no matching entry, and each
    ``Not filed`` line that does not end in its reason, is logged as
    ``{"heading", "text"}``. It is telemetry, never a contract failure. Items
    are matched to that severity's entries (closures aside) greedily, by the
    share of the item's words found in the entry's claim, so a severity with
    as many entries as items has no unfiled item whatever the wording; with
    fewer entries, the least-matching items are the unfiled ones. It cannot
    see an issue mentioned only in running prose; the sampling audit reads
    for that."""
    items = _prose_items(text)
    out = []
    for severity in SEVERITIES:
        claims = [_words(r["claim"]) for r in records
                  if r["severity"] == severity and r["resolves"] is None]
        pairs = sorted(((len(_words(t) & c) / max(len(_words(t)), 1), i, j)
                        for i, t in enumerate(items[severity]) for j, c in enumerate(claims)),
                       reverse=True)
        matched, used = set(), set()
        for _, i, j in pairs:
            if i not in matched and j not in used:
                matched.add(i)
                used.add(j)
        out.extend({"heading": severity, "text": t}
                   for i, t in enumerate(items[severity]) if i not in matched)
    out.extend({"heading": NOT_FILED, "text": t} for t in items[NOT_FILED] if not _has_reason(t))
    return out


# ---- one reviewer's report

def parse_report(text: str, lens: str, floor: str = DEFAULT_FLOOR,
                 prior_open: list = ()) -> dict:
    """The report's records, the open set after it, and its accounting."""
    if lens not in LENSES:
        raise ValueError(f"unknown lens {lens!r}")
    if floor not in LEVELS:
        raise ValueError(f"unknown verification floor {floor!r}")
    line = _last_line(text)
    if not line.startswith(PREFIX):
        raise ReportError(f"the last line does not start with {PREFIX}")
    try:
        raw = json.loads(line[len(PREFIX):])
    except json.JSONDecodeError as exc:
        raise ReportError(f"the {PREFIX} line is not JSON ({exc.msg})") from None
    if not isinstance(raw, list):
        raise ReportError(f"the {PREFIX} line is not a JSON array")
    records = [_finding(item, lens, n) for n, item in enumerate(raw, 1)]

    # Section 11: a stored fingerprint is a cache, never a comparison key.
    # The prior open set is keyed by the fingerprint recomputed from lens,
    # path and claim with today's normaliser, so a finding stored under an
    # earlier normaliser still meets its re-raise. Two prior findings that
    # the current normaliser makes one are one entry, the most severe (then
    # the better proven) kept.
    # A closure's resolves names the fingerprint the brief published, which
    # may be the stored one, so key_for_resolves maps a stored fingerprint
    # and a recomputed key alike to the recomputed key. The recomputed keys
    # are written last, so where one finding's stale cache equals another's
    # recomputed key, the recomputed key wins.
    prior, stored_to_key = {}, {}
    for f in prior_open:
        recomputed = fingerprint(f["lens"], f["path"], f["claim"])
        stored_to_key.setdefault(f["fingerprint"], recomputed)
        _keep_most_severe(prior, recomputed, {**f, "fingerprint": recomputed})
    key_for_resolves = {**stored_to_key, **{k: k for k in prior}}

    # One entry per fingerprint: the same claim at two sites (or two claims
    # differing only in a line reference) share one; the most severe wins,
    # then the better proven.
    by_fp = {}
    for record in (r for r in records if r["resolves"] is None):
        _keep_most_severe(by_fp, record["fingerprint"], record)
    seen = set(by_fp)

    # Each closure with the prior key it names (None if it names none).
    closure_targets = [(r, key_for_resolves.get(r["resolves"])) for r in records
                       if r["resolves"] is not None]
    # named: the resolves values already used by a valid closure. A closure
    # naming another stored fingerprint of a finding already closed (two the
    # current normaliser merged) is part of that closure, not a second one.
    claimed, rejected, named = [], [], set()
    for record, target in closure_targets:
        if target is None:
            reason = "resolves names no fingerprint in the previous round's open set"
        elif not record["evidence"].strip():
            reason = "a closure needs evidence (the resolving lines)"
        elif target in seen:
            # Section 11: the re-raise is kept and the closure rejected.
            reason = "the same report re-raises the finding it closes"
        elif record["resolves"] in named:
            reason = "a second closure of the same fingerprint is dropped"
        else:
            named.add(record["resolves"])
            if target not in claimed:
                claimed.append(target)
            continue
        rejected.append({"resolves": record["resolves"], "fingerprint": record["fingerprint"],
                         "reason": reason})
    # R5-R25: closing every prior finding without citing the resolving lines,
    # and re-raising none of them. A re-raise beside an empty closure is a
    # finding kept, not a report to discard.
    if prior and not claimed and not seen.intersection(prior) and all(
            any(target == fp and not r["evidence"].strip() for r, target in closure_targets)
            for fp in prior):
        raise ReportError("every prior finding is closed with empty evidence")

    current, downgrades_refused = [], []
    for fp, record in by_fp.items():
        before = prior.get(fp)
        if before and SEVERITIES.index(before["severity"]) < SEVERITIES.index(record["severity"]):
            # A re-raise may not lower a finding's severity: only a valid
            # closure, with evidence, takes a finding down.
            record = {**record, "severity": before["severity"]}
            downgrades_refused.append(fp)
        current.append(record)
    carried = [{**f, "unaccounted": True} for fp, f in prior.items()
               if fp not in seen and fp not in claimed]
    open_set = current + carried

    counts = {s.lower(): sum(f["severity"] == s for f in open_set) for s in SEVERITIES}
    floor_rank = LEVELS.index(floor)
    # A finding with no level is "asserted", the bottom of the ladder.
    unproven = [
        f["fingerprint"] for f in open_set
        if lens not in ADVISORY_LENSES and f["severity"] in BLOCKING
        and LEVELS.index(f.get("verification_level", "asserted")) < floor_rank
    ]
    return {"findings": records, "open_set": open_set, "counts": counts,
            "unproven": unproven, "closures": claimed, "closure_rejected": rejected,
            "unaccounted": [f["fingerprint"] for f in carried],
            "downgrades_refused": downgrades_refused,
            "prose_unfiled": prose_unfiled(text, records)}


# ---- the merge of one review round (R5-R3, R5-R12 to R5-R16)

def _lens_rank(lens: str) -> int:
    return LENS_PRIORITY.index(lens)


def _sort_key(entry: dict) -> tuple:
    """R5-R14: severity, then lens priority; then location and key, so the
    order never depends on the order the lenses reported in."""
    return (SEVERITIES.index(entry["severity"]), _lens_rank(entry["lenses"][0]),
            entry["path"] or "", entry["start_line"] or 0, entry["fingerprint"])


def _overlap(a: dict, b: dict) -> bool:
    return (a["path"] is not None and a["path"] == b["path"]
            and a["start_line"] is not None and b["start_line"] is not None
            and a["start_line"] <= b["end_line"] and b["start_line"] <= a["end_line"])


def _clusters(items: list, joined) -> list:
    """Connected groups of items, two joined when ``joined(a, b)``; each group
    and the groups themselves in a fixed order."""
    parent = list(range(len(items)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if joined(items[i], items[j]):
                parent[root(j)] = root(i)
    groups = {}
    for i in range(len(items)):
        groups.setdefault(root(i), []).append(items[i])
    return list(groups.values())


def merge_round(open_sets: dict) -> dict:
    """Merge one review round's per-lens open sets into the fix list and the
    deferred list, deterministically (a model never merges findings, R5-R3).

    ``open_sets`` maps a lens (``edge-cases`` or ``edge_cases``) to its open
    set as ``parse_report`` returns it. Closures are not findings and never
    appear in an open set.

    - Dedupe (R5-R13): findings on one path with overlapping line ranges and
      the same normalised claim are one entry, attributed to every lens that
      raised it; the most severe record of the kind that sets its standing
      (below) represents it. ``deduped`` lists each
      such set of fingerprints, the representative first. Overlapping ranges
      with different claims stay separate entries, listed together under
      ``grouped``. A finding with no path or range (a corpus finding) is
      never deduped or grouped.
    - Severity policy (R5-R12): an entry is blocking if it is CRITICAL or
      IMPORTANT and a blocking lens raised it. Clarity's CRITICAL and
      IMPORTANT entries join the fix list only when a blocking entry has made
      a fix round necessary, sorted last; otherwise they are deferred.
    - Deferred (R5-R15): every MINOR, every excellence entry and every
      advisory entry not in the fix list.
    - Order (R5-R14): severity, then lens priority (correctness, edge cases,
      provenance, clarity); clarity's entries in the fix list come after
      every blocking one.

    Returns ``{"fix_list", "deferred", "deduped", "grouped", "entries"}``:
    the lists hold representatives' fingerprints, and ``entries`` maps each
    representative to its merged record (with ``lenses``, ``standing``:
    blocking, clarity or deferred, and ``blocking``). Standing is decided
    record by record: a blocking lens's MINOR merged with an advisory lens's
    CRITICAL does not block.
    The same open sets always give the same result (R5-R16)."""
    findings = []
    for lens_name, open_set in open_sets.items():
        lens = schema_lens(lens_name)
        if lens not in LENSES:
            raise ValueError(f"unknown lens {lens_name!r}")
        for f in open_set:
            if f.get("resolves") is not None:
                raise ValueError("an open set holds no closures")
            if schema_lens(f["lens"]) != lens:
                raise ValueError(f"a {f['lens']} finding in {lens_name}'s open set")
            findings.append({**f, "lens": lens, "fingerprint": key(f)})
    # One fixed input order: the result must not depend on dict or report order.
    findings.sort(key=lambda f: (_lens_rank(f["lens"]), f["fingerprint"],
                                 f.get("path") or "", f.get("start_line") or 0))

    def same_finding(a: dict, b: dict) -> bool:
        return _overlap(a, b) and normalise_claim(a["claim"]) == normalise_claim(b["claim"])

    # Each group's standing comes from its own records, one at a time: a
    # record blocks only if a blocking lens raised it CRITICAL or IMPORTANT,
    # and clarity's serious records are the only advisory ones that can join
    # a fix list (R5-R12); excellence's never do (R5-R27). The representative
    # is the most severe record of the kind that gave the group its standing,
    # so an entry's severity and lens always come from one record.
    entries, deduped = [], []
    for group in _clusters(findings, same_finding):
        group.sort(key=lambda f: (_rank(f), _lens_rank(f["lens"]), f["fingerprint"]))
        blockers = [f for f in group if f["severity"] in BLOCKING and f["lens"] in BLOCKING_LENSES]
        clarity = [f for f in group if f["severity"] in BLOCKING and f["lens"] == "clarity"]
        standing = "blocking" if blockers else "clarity" if clarity else "deferred"
        head = (blockers or clarity or group)[0]
        lenses = sorted({f["lens"] for f in group}, key=_lens_rank)
        entries.append({**head, "lenses": lenses, "standing": standing,
                        "blocking": standing == "blocking"})
        if len(group) > 1:
            deduped.append([head["fingerprint"]]
                           + [f["fingerprint"] for f in group if f is not head])
    entries.sort(key=_sort_key)
    grouped = [[e["fingerprint"] for e in g] for g in _clusters(entries, _overlap) if len(g) > 1]

    blocking = [e for e in entries if e["standing"] == "blocking"]
    clarity_serious = [e for e in entries if e["standing"] == "clarity"]
    fix_list = blocking + (clarity_serious if blocking else [])
    in_fix = {e["fingerprint"] for e in fix_list}
    deferred = [e for e in entries if e["fingerprint"] not in in_fix]
    return {"fix_list": [e["fingerprint"] for e in fix_list],
            "deferred": [e["fingerprint"] for e in deferred],
            "deduped": deduped, "grouped": grouped,
            "entries": {e["fingerprint"]: e for e in entries}}


def merge_from_raw(reports: dict, priors: Any = None, floor: str = DEFAULT_FLOOR) -> dict:
    """Parse each lens's raw report and merge the round (R5-R16).

    ``reports`` maps a lens to its reviewer's raw output; ``priors`` maps a
    lens to its previous open set (verify mode). Returns ``{"lenses": {lens:
    parsed report}, "merge": merge_round(...)}``. Everything comes from the
    raw output alone, so a recorded round can be merged again and compared."""
    priors = priors or {}
    parsed = {}
    for lens_name, text in reports.items():
        lens = schema_lens(lens_name)
        parsed[lens] = parse_report(text, lens, floor, priors.get(lens_name, priors.get(lens, ())))
    merge = merge_round({lens: p["open_set"] for lens, p in parsed.items()})
    return {"lenses": parsed, "merge": merge}


# ---- the command line

def main(argv: list) -> int:
    args = list(argv)
    options = {"--floor": DEFAULT_FLOOR, "--prior": ""}
    for flag in options:
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(f"r5_findings: {flag} needs a value", file=sys.stderr)
                return 1
            options[flag] = args[i + 1]
            del args[i:i + 2]
    floor = options["--floor"]
    if len(args) != 3 or args[0] != "parse":
        print("usage: r5_findings.py parse <lens> <report-file> [--floor <level>] "
              "[--prior <file>]", file=sys.stderr)
        return 1
    prior_open = []
    if options["--prior"]:
        try:
            with open(options["--prior"], encoding="utf-8") as handle:
                prior_open = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"r5_findings: cannot read the prior open set: {exc}", file=sys.stderr)
            return 1
        try:
            check_prior(prior_open)
        except PriorError as exc:  # the prior file's fault, not the report's
            print(f"r5_findings: {exc}", file=sys.stderr)
            return 1
    _, lens, path = args
    try:
        # newline="": no universal-newline translation, so only LF splits
        # the report (output_lines), as section 11 says.
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except UnicodeDecodeError:  # the report itself fails the contract
        print(f"r5_findings: the report {path} is not valid UTF-8", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"r5_findings: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    try:
        result = parse_report(text, lens, floor, prior_open)
    except ValueError as exc:  # an unknown lens or floor: the caller's mistake
        print(f"r5_findings: {exc}", file=sys.stderr)
        return 1
    except ReportError as exc:
        print(f"r5_findings: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
