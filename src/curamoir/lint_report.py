"""The reader for `curamoir-lint --format json` (schema 1).

The report is a contract (docs/curamoir-lint.md, "JSON" and "Versioning"), and
a contract is only as strong as its consumers. Two consumers read it — the
sweep, which turns it into a work list (E3-R1), and the promotion ladder,
which tells a violation introduced from one merely found (E4-R4) — so the
policy's consumer half is enforced here, once, rather than re-derived in each:

- **Refuse a `schema_version` other than 1, or none.** A later version may
  mean something different by the same field names (how `col` counts, whether
  `end_col` is exclusive), and reading it as v1 would act on the wrong text.
- **Ignore what was added.** Unknown top-level fields, record fields and note
  kinds are additive under the policy and do not bump the version, so a reader
  that refused them would break on a compatible release. Unknown fields are
  not kept; unknown note kinds are dropped. `tools` is the exception: it is a
  name-to-version map, kept whole, because a new tool's version is still
  provenance.
- **Refuse an unknown value in a closed vocabulary** (`severity`,
  `fix_source`, and `fail_on`, which is a severity). Adding one bumps the
  version precisely because a consumer cannot place it, and guessing would
  loosen a gate.

**Its own copy of v1.** The vocabularies below are written out rather than
imported from `curamoir.rules`/`curamoir.lint`. If the runner grew a severity
without bumping the version, an imported tuple would absorb it silently; a
copy refuses it, and tests/test_lint_report.py fails, pointing at the bump
that was missed.

It also refuses a report that contradicts itself where the contract states
an invariant a consumer relies on: `complete` is false exactly when rules were
skipped, and every fingerprint is unique (the sweep's idempotence and the
ladder's introduced-vs-found both use it as an identity).

Stdlib only, and syntax that imports on 3.9: the sweep reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional

#: The only version this reader understands.
SCHEMA_VERSION = 1

#: v1's closed vocabularies. Ordered: SEVERITIES from least to most severe,
#: so a consumer can rank by index, as the runner does.
SEVERITIES = ("info", "warning", "error")
FIX_SOURCES = ("rule", "principle")

#: The note kinds this reader knows, with the string keys each must carry.
#: Any other kind is dropped (an open vocabulary).
NOTE_KEYS = {
    "no_rules": (),
    "ast_grep_version_unknown": ("reason",),
    "symlink_not_followed": ("path",),
    "not_regular_file": ("path",),
}

#: The top-level fields read. `schema_version` is checked first, on its own.
REQUIRED_TOP_LEVEL = (
    "schema_version", "tools", "fail_on", "complete", "rules_checked", "rules_skipped",
    "notes", "violations",
)

_POSITIONS = ("line", "col", "end_line", "end_col")
_STRINGS = ("path", "rule_id", "principle_id", "principle_rule", "message", "fix",
            "why_link", "match", "fingerprint")


class LintReportError(ValueError):
    """The report cannot be read as schema 1. The message names where."""


@dataclass(frozen=True)
class ReportedViolation:
    """One violation, as the report states it. Positions are 1-based and the
    end is exclusive (docs/curamoir-lint.md). `location` is not kept: it is
    `path:line:col`, derivable from the fields here."""

    path: str
    line: int
    col: int
    end_line: int
    end_col: int
    severity: str
    rule_id: str
    principle_id: str
    principle_rule: str
    message: str
    fix: str
    fix_source: str
    why_link: str
    match: str
    fingerprint: str


@dataclass(frozen=True)
class SkippedRule:
    rule_id: str
    reason: str


@dataclass(frozen=True)
class Note:
    """A known note. `path` and `reason` are set for the kinds that carry
    them (NOTE_KEYS) and None otherwise."""

    kind: str
    path: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class LintReport:
    schema_version: int
    tools: Mapping[str, Optional[str]]
    fail_on: str
    complete: bool
    rules_checked: tuple[str, ...]
    rules_skipped: tuple[SkippedRule, ...]
    notes: tuple[Note, ...]
    violations: tuple[ReportedViolation, ...]


def parse_report(text):
    """Read a report from its JSON text (str, or UTF-8 bytes)."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as error:
            raise LintReportError(f"the report is not UTF-8: {error}") from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise LintReportError(f"the report is not JSON: {error}") from error
    return read_report(document)


def read_report(document):
    """Read a report already decoded from JSON."""
    if not isinstance(document, dict):
        raise LintReportError(
            f"the report must be a JSON object, not {_json_type(document)}")
    if "schema_version" not in document:
        raise LintReportError(
            "the report has no schema_version, so it is not a schema-1 report "
            "(the pre-release shape had `version`)")
    version = document["schema_version"]
    if not _is_int(version) or version != SCHEMA_VERSION:
        raise LintReportError(
            f"schema_version is {version!r}; this reader understands only "
            f"{SCHEMA_VERSION}. A different version may mean something different "
            "by the same fields, so it is refused rather than guessed at")
    for field in REQUIRED_TOP_LEVEL:
        if field not in document:
            raise LintReportError(f"the report has no {field}")

    tools = _tools(document["tools"])
    fail_on = _vocabulary(document["fail_on"], SEVERITIES, "fail_on")
    complete = document["complete"]
    if not isinstance(complete, bool):
        raise LintReportError(f"complete must be true or false, not {complete!r}")
    rules_checked = tuple(
        _string(value, f"rules_checked[{index}]")
        for index, value in enumerate(_list(document["rules_checked"], "rules_checked"))
    )
    rules_skipped = tuple(
        _skipped(entry, f"rules_skipped[{index}]")
        for index, entry in enumerate(_list(document["rules_skipped"], "rules_skipped"))
    )
    if complete == bool(rules_skipped):
        raise LintReportError(
            f"complete is {str(complete).lower()} but {len(rules_skipped)} rule(s) "
            "were skipped; the report contradicts itself")
    notes = tuple(
        note
        for index, entry in enumerate(_list(document["notes"], "notes"))
        for note in _note(entry, f"notes[{index}]")
    )
    violations = tuple(
        _violation(record, f"violations[{index}]")
        for index, record in enumerate(_list(document["violations"], "violations"))
    )
    seen = {}
    for index, violation in enumerate(violations):
        if violation.fingerprint in seen:
            raise LintReportError(
                f"violations[{index}].fingerprint {violation.fingerprint!r} repeats "
                f"violations[{seen[violation.fingerprint]}]'s; within a report every "
                "fingerprint is unique")
        seen[violation.fingerprint] = index

    return LintReport(
        schema_version=version,
        tools=tools,
        fail_on=fail_on,
        complete=complete,
        rules_checked=rules_checked,
        rules_skipped=rules_skipped,
        notes=notes,
        violations=violations,
    )


def _violation(record, where):
    if not isinstance(record, dict):
        raise LintReportError(f"{where} must be an object, not {_json_type(record)}")
    for field in _POSITIONS + _STRINGS + ("severity", "fix_source"):
        if field not in record:
            raise LintReportError(f"{where} has no {field}")
    values = {}
    for field in _POSITIONS:
        value = record[field]
        if not _is_int(value) or value < 1:
            raise LintReportError(
                f"{where}.{field} must be a positive integer, not {value!r}")
        values[field] = value
    for field in _STRINGS:
        values[field] = _string(record[field], f"{where}.{field}")
    values["severity"] = _vocabulary(record["severity"], SEVERITIES, f"{where}.severity")
    values["fix_source"] = _vocabulary(record["fix_source"], FIX_SOURCES,
                                       f"{where}.fix_source")
    return ReportedViolation(**values)


def _skipped(entry, where):
    if not isinstance(entry, dict):
        raise LintReportError(f"{where} must be an object, not {_json_type(entry)}")
    for field in ("rule_id", "reason"):
        if field not in entry:
            raise LintReportError(f"{where} has no {field}")
    return SkippedRule(rule_id=_string(entry["rule_id"], f"{where}.rule_id"),
                       reason=_string(entry["reason"], f"{where}.reason"))


def _note(entry, where):
    """A one-tuple for a known kind, empty for an unknown one."""
    if not isinstance(entry, dict):
        raise LintReportError(f"{where} must be an object, not {_json_type(entry)}")
    if "kind" not in entry:
        raise LintReportError(f"{where} has no kind")
    kind = _string(entry["kind"], f"{where}.kind")
    if kind not in NOTE_KEYS:
        return ()
    values = {}
    for key in NOTE_KEYS[kind]:
        if key not in entry:
            raise LintReportError(f"{where} is a {kind} note with no {key}")
        values[key] = _string(entry[key], f"{where}.{key}")
    return (Note(kind=kind, **values),)


def _tools(value):
    if not isinstance(value, dict):
        raise LintReportError(f"tools must be an object, not {_json_type(value)}")
    for name, version in value.items():
        if version is not None and not isinstance(version, str):
            raise LintReportError(
                f"tools[{name!r}] must be a version string or null, not {version!r}")
    return MappingProxyType(dict(value))


def _vocabulary(value, allowed, where):
    if value not in allowed or not isinstance(value, str):
        raise LintReportError(
            f"{where} is {value!r}, not one of {', '.join(allowed)}. It is a closed "
            "vocabulary in schema 1, so a new value means a new schema")
    return value


def _list(value, where):
    if not isinstance(value, list):
        raise LintReportError(f"{where} must be an array, not {_json_type(value)}")
    return value


def _string(value, where):
    if not isinstance(value, str):
        raise LintReportError(f"{where} must be a string, not {value!r}")
    return value


def _is_int(value):
    # bool is an int subclass, and JSON `true` is not a version or a line.
    return isinstance(value, int) and not isinstance(value, bool)


def _json_type(value):
    if value is None:
        return "null"
    return {dict: "an object", list: "an array", str: "a string", bool: "a boolean",
            int: "a number", float: "a number"}.get(type(value), type(value).__name__)
