#!/usr/bin/env python3
"""Parse an R5 reviewer's report into the shared Finding schema (cm-70j).

The contract is section 10 of docs/r5-gate-v2-curamoir-spec.md, shared word
for word with NthLayer: a reviewer's LAST non-empty output line is
``R5-FINDINGS:`` followed by a JSON array of findings. r5-state.sh calls this
for `findings` and `advisory`; it never trusts counts typed by the
orchestrator.

    r5_findings.py parse <lens> <report-file> [--floor <level>]

prints one JSON object on stdout: ``findings`` (the normalised array),
``counts`` (critical, important, minor) and ``unproven`` (fingerprints of
blocking findings whose verification_level is below the floor). Exit 0; 2 if
the report does not meet the contract (the reason on stderr: the caller
records the reviewer UNAVAILABLE, R5-R9); 1 on a usage error.

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

_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_NUMBER = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")


class ReportError(Exception):
    """The report does not meet the section 10 contract."""


def normalise_claim(claim: str) -> str:
    """Lower-case, SHAs and line numbers removed, whitespace collapsed."""
    text = claim.lower()
    text = _SHA.sub(" ", text)
    text = _NUMBER.sub(" ", text)
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
    evidence = _text(raw, "evidence", n)
    fix = raw.get("fix")
    if fix is not None and not isinstance(fix, str):
        raise ReportError(f"finding {n}: fix must be a string or null")
    resolves = raw.get("resolves")
    if resolves is not None and not isinstance(resolves, str):
        raise ReportError(f"finding {n}: resolves must be a fingerprint or null")
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


def parse_report(text: str, lens: str, floor: str = DEFAULT_FLOOR) -> dict:
    """The report's findings, their counts and the unproven fingerprints."""
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
    findings = [_finding(item, lens, n) for n, item in enumerate(raw, 1)]
    counts = {s.lower(): sum(f["severity"] == s for f in findings) for s in SEVERITIES}
    floor_rank = LEVELS.index(floor)
    # A finding with no level is "asserted", the bottom of the ladder.
    unproven = [
        f["fingerprint"] for f in findings
        if lens not in ADVISORY_LENSES and f["severity"] in BLOCKING
        and LEVELS.index(f.get("verification_level", "asserted")) < floor_rank
    ]
    return {"findings": findings, "counts": counts, "unproven": unproven}


def main(argv: list) -> int:
    args = list(argv)
    floor = DEFAULT_FLOOR
    if "--floor" in args:
        i = args.index("--floor")
        if i + 1 >= len(args):
            print("r5_findings: --floor needs a level", file=sys.stderr)
            return 1
        floor = args[i + 1]
        del args[i:i + 2]
    if len(args) != 3 or args[0] != "parse":
        print("usage: r5_findings.py parse <lens> <report-file> [--floor <level>]", file=sys.stderr)
        return 1
    _, lens, path = args
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"r5_findings: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    try:
        result = parse_report(text, lens, floor)
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
