"""The consumer side of the `curamoir-lint` JSON contract (cm-fkd.1).

docs/curamoir-lint.md, "Versioning", makes two promises that only hold if every
consumer keeps them: a reader refuses a `schema_version` it does not know, and
it ignores fields and note kinds it does not know. `curamoir.lint_report` is
where both the sweep (E3-R1) and the promotion ladder (E4-R4) keep them, so
they are tested once, here.

**Fixture provenance.** `RECORD` is the example record from
docs/curamoir-lint.md, field for field. The round-trip tests use a real run of
`scripts/curamoir-lint` over a copy of `fixtures/sample-repo` with the regex
test rules from tests/test_lint.py, whose output is pinned there against
PLANTED.json.
"""

import copy
import dataclasses
import json
import subprocess

import pytest

from curamoir import lint, lint_report, rules
from curamoir.lint_report import LintReportError, parse_report
from tests.test_lint import ALL_REGEX_RULES, WRAPPER, sample_repo

RECORD = {
    "location": "src/sample/core/billing.py:15:5",
    "path": "src/sample/core/billing.py",
    "line": 15,
    "col": 5,
    "end_line": 15,
    "end_col": 11,
    "severity": "error",
    "rule_id": "no-bare-print",
    "principle_id": "structured-logging",
    "principle_rule": "Emit diagnostics through the module logger, never through `print`.",
    "message": "print( writes to stdout with no level or logger name",
    "fix": "logger.info(...), or logger.debug(...) while debugging",
    "fix_source": "rule",
    "why_link": ".curamoir/principles.md#structured-logging",
    "match": "print(",
    "fingerprint": "66dc3a54eb3d313c",
}

DOCUMENT = {
    "schema_version": 1,
    "tools": {"ast-grep": None},
    "fail_on": "error",
    "complete": True,
    "rules_checked": ["no-bare-print"],
    "rules_skipped": [],
    "notes": [],
    "violations": [RECORD],
}


def document(**changes):
    result = copy.deepcopy(DOCUMENT)
    result.update(changes)
    return result


def parse(value):
    return parse_report(json.dumps(value))


def with_record(**changes):
    record = dict(RECORD, **changes)
    return document(violations=[record])


# --- the version: refuse what we cannot read ---------------------------------

@pytest.mark.parametrize("version", [0, 2, "1", 1.0, True, None])
def test_a_schema_version_other_than_1_is_refused(version):
    """A v2 report may mean something different by the same field names (how
    `col` counts, whether `end_col` is exclusive); reading it as v1 would act
    on the wrong lines. `True` and `1.0` are refused too: JSON has no integer
    type of its own, and `True == 1` in Python, which is not a reason to
    accept it."""
    with pytest.raises(LintReportError, match="schema_version"):
        parse(document(schema_version=version))


def test_a_report_with_no_schema_version_is_refused():
    """The pre-cm-9jv shape had `version`, not `schema_version`; nothing should
    read it as a v1."""
    legacy = document()
    del legacy["schema_version"]
    legacy["version"] = 1

    with pytest.raises(LintReportError, match="schema_version"):
        parse(legacy)


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '"report"'])
def test_text_that_is_not_a_json_object_is_refused(text):
    with pytest.raises(LintReportError):
        parse_report(text)


def test_bytes_are_accepted_as_utf8():
    report = parse_report(json.dumps(DOCUMENT, ensure_ascii=False).encode("utf-8"))

    assert report.violations[0].rule_id == "no-bare-print"


def test_non_utf8_bytes_are_refused():
    with pytest.raises(LintReportError):
        parse_report(b'{"schema_version": 1, "x": "\xff"}')


# --- additive changes: ignore what we do not know ----------------------------

def test_unknown_top_level_fields_are_ignored():
    """Adding a top-level field does not bump the version, so a reader that
    refused one would break on a compatible release."""
    report = parse(document(generated_by="a later curamoir-lint", extra={"x": [1]}))

    assert len(report.violations) == 1


def test_unknown_record_fields_are_ignored():
    report = parse(with_record(column_bytes=4, suggestion={"text": "logger.info"}))

    assert report.violations[0].path == "src/sample/core/billing.py"
    assert not hasattr(report.violations[0], "column_bytes")


def test_unknown_tools_entries_are_kept_as_given():
    report = parse(document(tools={"ast-grep": "0.50.0", "semgrep": "1.2.3"}))

    assert report.tools["ast-grep"] == "0.50.0"
    assert report.tools["semgrep"] == "1.2.3"


def test_unknown_note_kinds_are_dropped_and_known_ones_kept():
    notes = [
        {"kind": "no_rules"},
        {"kind": "a_kind_from_the_future", "detail": "whatever it says"},
        {"kind": "symlink_not_followed", "path": "leak.py"},
        {"kind": "ast_grep_version_unknown", "reason": "stdout was empty"},
        {"kind": "not_regular_file", "path": "pipe", "extra": 1},
    ]
    report = parse(document(notes=notes))

    assert [n.kind for n in report.notes] == [
        "no_rules", "symlink_not_followed", "ast_grep_version_unknown", "not_regular_file",
    ]
    assert report.notes[1].path == "leak.py"
    assert report.notes[2].reason == "stdout was empty"


def test_a_known_note_kind_missing_its_key_is_refused():
    """A known kind is a typed shape; one without its `path` is a retyping
    (a version bump), not an addition."""
    with pytest.raises(LintReportError, match="notes"):
        parse(document(notes=[{"kind": "symlink_not_followed"}]))


@pytest.mark.parametrize("note", ["no_rules", {"path": "x"}, {"kind": 3}])
def test_a_note_that_is_not_a_kinded_object_is_refused(note):
    with pytest.raises(LintReportError, match="notes"):
        parse(document(notes=[note]))


# --- closed vocabularies: refuse a value we cannot place ---------------------

@pytest.mark.parametrize("severity", ["critical", "ERROR", "", None])
def test_an_unknown_severity_is_refused(severity):
    """A new severity bumps the version (it is a closed vocabulary): a
    consumer that ranks severities cannot place one it has never seen, and
    guessing would loosen a gate."""
    with pytest.raises(LintReportError, match="severity"):
        parse(with_record(severity=severity))


@pytest.mark.parametrize("fix_source", ["model", "", None])
def test_an_unknown_fix_source_is_refused(fix_source):
    with pytest.raises(LintReportError, match="fix_source"):
        parse(with_record(fix_source=fix_source))


def test_an_unknown_fail_on_is_refused():
    with pytest.raises(LintReportError, match="fail_on"):
        parse(document(fail_on="fatal"))


def test_the_readers_v1_vocabularies_match_the_runners():
    """The reader keeps its OWN copy of v1's vocabularies rather than
    importing the runner's: a severity added to the runner without a version
    bump must be refused by the reader, not silently absorbed by it. So when
    this test fails, the fix is a `schema_version` bump in the runner, not an
    edit here."""
    assert lint_report.SEVERITIES == rules.SEVERITIES
    assert set(lint_report.FIX_SOURCES) == {"rule", "principle"}
    assert lint_report.SCHEMA_VERSION == lint.SCHEMA_VERSION
    assert set(lint_report.NOTE_KEYS) <= set(lint.NOTE_KINDS)


def test_every_field_the_reader_requires_is_one_the_runner_writes():
    """Requiring a field the runner never writes would refuse every report."""
    typed = {f.name for f in dataclasses.fields(lint_report.ReportedViolation)}
    assert typed <= set(lint.RECORD_FIELDS)
    assert set(lint_report.REQUIRED_TOP_LEVEL) <= set(lint.TOP_LEVEL_FIELDS)


# --- types: refuse what is not the contract -----------------------------------

@pytest.mark.parametrize("field", [
    "path", "line", "col", "end_line", "end_col", "rule_id", "principle_id",
    "principle_rule", "message", "fix", "why_link", "match", "fingerprint",
])
def test_a_record_missing_a_field_is_refused(field):
    record = dict(RECORD)
    del record[field]

    with pytest.raises(LintReportError, match=field):
        parse(document(violations=[record]))


@pytest.mark.parametrize("value", ["15", 0, -1, 15.0, True, None])
def test_a_position_that_is_not_a_positive_integer_is_refused(value):
    with pytest.raises(LintReportError, match=r"violations\[0\]\.line"):
        parse(with_record(line=value))


@pytest.mark.parametrize("end", [{"end_line": 14}, {"end_line": 15, "end_col": 4}])
def test_an_end_before_the_start_is_refused(end):
    """RECORD starts at 15:5. The end is exclusive, so an end equal to the
    start is an empty span and allowed; one before it locates no text."""
    with pytest.raises(LintReportError, match="before"):
        parse(with_record(**end))


def test_an_end_equal_to_the_start_is_accepted():
    violation = parse(with_record(end_line=15, end_col=5)).violations[0]

    assert (violation.end_line, violation.end_col) == (15, 5)


@pytest.mark.parametrize("field", ["path", "match", "fingerprint", "message"])
def test_a_string_field_that_is_not_a_string_is_refused(field):
    with pytest.raises(LintReportError, match=field):
        parse(with_record(**{field: 15}))


@pytest.mark.parametrize("changes", [
    {"complete": "true"},
    {"rules_checked": "no-bare-print"},
    {"rules_checked": [1]},
    {"rules_skipped": [{"rule_id": "x"}]},
    {"rules_skipped": ["x"]},
    {"tools": ["ast-grep"]},
    {"tools": {"ast-grep": 50}},
    {"notes": {}},
    {"violations": {}},
    {"violations": ["src/a.py:1:1"]},
])
def test_a_top_level_field_of_the_wrong_type_is_refused(changes):
    with pytest.raises(LintReportError):
        parse(document(**changes))


@pytest.mark.parametrize("field", ["tools", "fail_on", "complete", "rules_checked",
                                   "rules_skipped", "notes", "violations"])
def test_a_report_missing_a_top_level_field_is_refused(field):
    broken = document()
    del broken[field]

    with pytest.raises(LintReportError, match=field):
        parse(broken)


def test_complete_must_agree_with_rules_skipped():
    """`complete` is false exactly when rules were skipped. A report that says
    otherwise contradicts itself, and the sweep cannot tell which half to
    believe."""
    skipped = [{"rule_id": "no-print", "reason": "ast-grep is not installed"}]
    with pytest.raises(LintReportError, match="complete"):
        parse(document(complete=True, rules_skipped=skipped))
    with pytest.raises(LintReportError, match="complete"):
        parse(document(complete=False, rules_skipped=[]))

    report = parse(document(complete=False, rules_skipped=skipped))
    assert report.rules_skipped[0].rule_id == "no-print"


def test_a_repeated_fingerprint_is_refused():
    """Within one report every fingerprint is unique; the idempotence and
    introduced-vs-found checks rely on it as an identity."""
    with pytest.raises(LintReportError, match="fingerprint"):
        parse(document(violations=[RECORD, dict(RECORD, line=16, end_line=16, location="x")]))


# --- typed, immutable records ------------------------------------------------

def test_records_are_typed_and_immutable():
    report = parse(document())
    violation = report.violations[0]

    assert (violation.path, violation.line, violation.col) == ("src/sample/core/billing.py", 15, 5)
    assert (violation.end_line, violation.end_col) == (15, 11)
    assert violation.severity == "error"
    assert violation.fix_source == "rule"
    assert violation.fingerprint == "66dc3a54eb3d313c"
    assert isinstance(report.violations, tuple)
    assert isinstance(report.rules_checked, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        violation.line = 16
    with pytest.raises(TypeError):
        report.tools["ast-grep"] = "0.50.0"


# --- round trip: a real run ----------------------------------------------------

def test_a_real_lint_run_round_trips_field_for_field(tmp_path):
    """The reader against the runner, not against a document written to
    agree with it: every typed field equals the JSON's, in report order."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    result = subprocess.run(
        [str(WRAPPER), "--repo", str(repo), "--format", "json"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == lint.EXIT_VIOLATIONS, result.stderr

    raw = json.loads(result.stdout)
    report = parse_report(result.stdout)

    assert report.schema_version == 1
    assert report.fail_on == raw["fail_on"]
    assert report.complete is True
    assert list(report.rules_checked) == raw["rules_checked"]
    assert len(report.violations) == len(raw["violations"]) == 5
    for typed, record in zip(report.violations, raw["violations"], strict=True):
        for field in dataclasses.fields(typed):
            assert getattr(typed, field.name) == record[field.name], field.name
