#!/usr/bin/env python3
"""Parse an R5 reviewer's report into the shared Finding schema (cm-70j).

The contract is section 10 of docs/r5-gate-v2-curamoir-spec.md, shared word
for word with NthLayer: a reviewer's LAST non-empty output line is
``R5-FINDINGS:`` followed by a JSON array of findings. r5-state.sh calls this
for `findings` and `advisory`; it never trusts counts typed by the
orchestrator.

    r5_findings.py parse <lens> <report-file> [--floor <level>] [--prior <file>]

``--prior`` is a JSON file holding the previous round's open set for this lens
(a list of findings; none on a first review). It prints one JSON object:

- ``findings``: every record in the report, normalised;
- ``open_set``: the findings still open after this round, which the caller
  hands back as ``--prior`` next time;
- ``counts`` (critical, important, minor) and ``unproven`` (blocking findings
  whose verification_level is below the floor), both over the open set;
- ``closures`` (the prior fingerprints validly closed), ``closure_rejected``
  (each rejected closure with its reason) and ``unaccounted`` (prior open
  findings the report neither closed nor re-raised).

Closures, per section 10 (settled 2026-11-07). A record with ``resolves`` set
is a closure, not a finding: it is never counted, whatever its severity, and
its path, lines and evidence are the resolving lines. It is valid only if
``resolves`` names a fingerprint in the previous round's open set and its
evidence is non-empty; an invalid closure is ignored and rejected with the
reason, and a second closure of the same fingerprint is dropped. The
closure's own fingerprint is never compared to ``resolves``. A re-raised
finding is the prior record copied verbatim (path and claim, so the same
fingerprint) with no ``resolves``. The open set is the previous open set
minus valid closures, plus new and re-raised findings; a prior open finding
with neither a closure nor a re-raise is carried forward unchanged, tagged
``unaccounted``: silence never closes. A re-raise keeps the more severe of
its own and the prior finding's severity (``downgrades_refused`` lists those
it held up): severity goes down only through a valid closure. A report that
closes every prior finding with empty evidence is a contract failure
(R5-R25).

Exit 0; 2 if the report does not meet the contract (the reason on stderr:
the caller records the reviewer UNAVAILABLE, R5-R9); 1 on a usage error.

Standard library only, and Python 3.9 compatible: the hooks run under
whatever python3 the session finds (macOS ships 3.9).
"""

from __future__ import annotations

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
# Excellence is advisory: it never blocks, so it has nothing to prove.
ADVISORY_LENSES = ("excellence",)

# Removed from a claim before it is fingerprinted, so a finding keeps its key
# when the code it points at moves: line references (line 42, lines 4-9,
# :42, :4-9, L42) and SHA-like tokens (7 to 40 hex characters with at least
# one digit, so a word like "defaced" stays). Other numbers stay: "exits 2"
# and "exits 4" are different claims.
_LINE_REF = re.compile(r"\blines?\s+\d+(?:\s*[-\u2013]\s*\d+)?\b|:\d+(?:-\d+)?\b|\bl\d+\b")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_SPACE = re.compile(r"\s+")


class ReportError(Exception):
    """The report does not meet the section 10 contract."""


def normalise_claim(claim: str) -> str:
    """Lower-case, line references and SHAs removed, whitespace collapsed."""
    text = claim.lower()
    text = _LINE_REF.sub(" ", text)
    text = _SHA.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def fingerprint(lens: str, path: str, claim: str) -> str:
    """First 16 hex of sha256(lens|path|normalised claim)."""
    key = f"{lens}|{path}|{normalise_claim(claim)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    raise ReportError("the report is empty")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(finding: dict, key: str, n: int) -> str:
    value = finding.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"finding {n}: {key} must be a non-empty string")
    return value


def _finding(raw: Any, lens: str, n: int) -> dict:
    if not isinstance(raw, dict):
        raise ReportError(f"finding {n} is not an object")
    if raw.get("lens", lens) != lens:
        raise ReportError(f"finding {n}: lens {raw.get('lens')!r} is not this reviewer's ({lens})")
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        raise ReportError(f"finding {n}: severity must be one of {', '.join(SEVERITIES)}")
    path = _text(raw, "path", n)
    start, end = raw.get("start_line"), raw.get("end_line")
    if not (_is_int(start) and _is_int(end) and 1 <= start <= end):
        raise ReportError(f"finding {n}: start_line and end_line must be integers with 1 <= start <= end")
    claim = _text(raw, "claim", n)
    resolves = raw.get("resolves")
    if resolves is not None and not isinstance(resolves, str):
        raise ReportError(f"finding {n}: resolves must be a fingerprint or null")
    evidence = raw.get("evidence")
    if resolves is None:
        evidence = _text(raw, "evidence", n)
    elif evidence is None:
        # A closure without evidence is a contract matter for the round (it
        # is rejected, or the reviewer UNAVAILABLE), not a malformed record.
        evidence = ""
    elif not isinstance(evidence, str):
        raise ReportError(f"finding {n}: evidence must be a string")
    fix = raw.get("fix")
    if fix is not None and not isinstance(fix, str):
        raise ReportError(f"finding {n}: fix must be a string or null")
    regression = raw.get("regression", False)
    if not isinstance(regression, bool):
        raise ReportError(f"finding {n}: regression must be true or false")
    level = raw.get("verification_level")
    if level is not None and level not in LEVELS:
        raise ReportError(f"finding {n}: verification_level must be one of {', '.join(LEVELS)}")
    finding = {
        "lens": lens, "severity": severity, "path": path,
        "start_line": start, "end_line": end,
        "claim": claim, "evidence": evidence, "fix": fix,
        # Computed here, never taken from the reviewer: it is the merge key.
        "fingerprint": fingerprint(lens, path, claim),
        "resolves": resolves, "regression": regression,
        # Set by the fixer, never by a reviewer.
        "status": "open",
    }
    if level is not None:
        finding["verification_level"] = level
    return finding


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

    prior = {f["fingerprint"]: f for f in prior_open}
    closed, rejected = [], []
    for record in (r for r in records if r["resolves"] is not None):
        target = record["resolves"]
        if target not in prior:
            reason = "resolves names no fingerprint in the previous round's open set"
        elif not record["evidence"].strip():
            reason = "a closure needs evidence (the resolving lines)"
        elif target in closed:
            reason = "a second closure of the same fingerprint is dropped"
        else:
            closed.append(target)
            continue
        rejected.append({"resolves": target, "fingerprint": record["fingerprint"],
                         "reason": reason})
    # R5-R25: closing everything without citing the resolving lines.
    if prior and not closed and all(
            any(r["resolves"] == fp and not r["evidence"].strip() for r in records)
            for fp in prior):
        raise ReportError("every prior finding is closed with empty evidence")

    current, seen, held = [], set(), []
    for record in (r for r in records if r["resolves"] is None):
        fp = record["fingerprint"]
        if fp in seen:  # one entry per fingerprint
            continue
        seen.add(fp)
        before = prior.get(fp)
        if before and SEVERITIES.index(before["severity"]) < SEVERITIES.index(record["severity"]):
            # A re-raise may not lower a finding's severity: only a valid
            # closure, with evidence, takes a finding down.
            record = {**record, "severity": before["severity"]}
            held.append(fp)
        current.append(record)
    # A re-raise of a closed fingerprint keeps it open.
    closed = [fp for fp in closed if fp not in seen]
    carried = [{**f, "unaccounted": True} for fp, f in prior.items()
               if fp not in seen and fp not in closed]
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
            "unproven": unproven, "closures": closed, "closure_rejected": rejected,
            "unaccounted": [f["fingerprint"] for f in carried],
            "downgrades_refused": held}


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
        if not (isinstance(prior_open, list)
                and all(isinstance(f, dict) and isinstance(f.get("fingerprint"), str)
                        for f in prior_open)):
            print("r5_findings: the prior open set must be a list of findings", file=sys.stderr)
            return 1
    _, lens, path = args
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
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
