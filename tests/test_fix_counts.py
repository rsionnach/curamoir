"""E3-R5 (cm-fkd.6): the sweep records per-principle fix counts to the
ledger, and a pure reader turns the ledger back into the counts E4-R1
(cm-kcq.1) compares with `promotion.fix_threshold`.

**Fixture provenance.** The end-to-end tests run on `fixtures/sample-repo`
committed with tests/test_lint.py's regex rules (tests/conftest.py,
committed_sample): the five planted violations PLANTED.json lists, two in
billing.py, two in handlers.py, and handlers_v2.py's name, which the
`no-variant-module` rule's `remedy: delete_file` lets the runner delete.
The edits are the ones tests/test_green_gate.py's acceptance test makes,
including the billing fix that also zeroes the quantity; here the test
command is a grep that is red exactly when that line is present, rather
than the fixture's pytest run, so the gate's decision is the same and the
test does not depend on the ambient python3 having pytest. The lint is the
real `scripts/curamoir-lint`, at the base and again after the gate.

The reader tests seed ledger lines by hand, in the shape JobRun writes
(tests/test_job_run.py pins that shape), because a seeded ledger with
chosen timestamps is what the acceptance criterion describes.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from curamoir.config import ConfigError, load_config
from curamoir.fix_counts import (
    AGENT,
    REMEDY,
    RULE,
    TIER1,
    FixRecord,
    fix_counts,
    found_fields,
    ledger_fields,
    record_fixes,
    relint,
    rule_fix_records,
)
from curamoir.fix_scope import ScopeCheckError, lint_sandbox, verify_fix
from curamoir.git_guard import prepare_sandbox, snapshot_git_state
from curamoir.green_gate import REDUCED, run_test_gate
from curamoir.job_run import JobRun
from curamoir.ledger import Ledger
from curamoir.lint_report import ReportedViolation
from curamoir.remedies import apply_file_remedies
from tests.conftest import committed_sample

BILLING = "src/sample/core/billing.py"
HANDLERS = "src/sample/web/handlers.py"
VARIANT = "src/sample/web/handlers_v2.py"

#: Red exactly when the bad billing fix (it zeroes the quantity) is present.
RED_IF_QUANTITY_ZEROED = f"! grep -qs 'quantity = 0' {BILLING}"

BILLING_PRINT = (BILLING, '    print("calculating line total")',
                 '    logger.info("calculating line total")\n    quantity = 0')
BILLING_RAISE = (BILLING,
                 "    except OSError:\n        raise RuntimeError(\"could not load rates\")",
                 "    except OSError as error:\n"
                 "        raise RuntimeError(f\"could not load rates from {path}\") from error")
HANDLERS_PRINT = (HANDLERS, 'print(f"request failed: {exception}")',
                  'logger.error("request failed: %s", exception)')
HANDLERS_RAISE = (HANDLERS, '    except IndexError:\n        raise ValueError("bad token")',
                  '    except IndexError as error:\n'
                  '        raise ValueError(f"bad token: {raw!r}") from error')


def edit(root, path, old, new):
    target = root / path
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1
    target.write_text(text.replace(old, new), encoding="utf-8")


def fixture_sweep(tmp_path, edits, command="true", before_relint=None):
    """The sweep's steps on the committed fixture, as fix_scope's "What the
    runner must do" orders them, through the fix records: (work, gate,
    remedies, records). `edits` stand in for the agent's."""
    repo = committed_sample(tmp_path)
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        f"version: 1\nsweep:\n  test_command: {json.dumps(command)}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "test command"], cwd=repo, check=True)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        remedies = apply_file_remedies(sandbox)
        state = snapshot_git_state(sandbox)
        for path, old, new in edits:
            edit(sandbox.path, path, old, new)
        verdict = verify_fix(sandbox, git_state=state)
        assert verdict.ok, verdict.render()
        gate = run_test_gate(sandbox, verdict)
        if before_relint:
            before_relint(sandbox)
        records = record_fixes(sandbox, gate, remedies)
        return work, gate, remedies, records, sandbox.base


# --- which violations were fixed --------------------------------------------------------

def test_only_fixes_that_reach_the_pr_are_recorded(tmp_path):
    """Four agent fixes and one remedy; the gate drops billing.py, so its two
    fixes are not fixes. What is left: handlers.py's two and the deletion."""
    work, gate, remedies, records, base = fixture_sweep(
        tmp_path, [BILLING_PRINT, BILLING_RAISE, HANDLERS_PRINT, HANDLERS_RAISE],
        command=RED_IF_QUANTITY_ZEROED)

    assert gate.strategy == REDUCED
    assert gate.paths_for_pr == (HANDLERS, VARIANT)
    assert remedies.deleted == (VARIANT,)
    assert len(work.items) == 5
    assert [(r.principle_id, r.rule_id, r.path, r.fixed_by) for r in records] == [
        ("errors-carry-context", "no-unchained-raise", HANDLERS, AGENT),
        ("no-file-variant-proliferation", "no-variant-module", VARIANT, REMEDY),
        ("structured-logging", "no-bare-print", HANDLERS, AGENT),
    ]
    assert all(r.source == RULE and r.base == base for r in records)
    # Each record names the very violation the base's lint reported.
    reported = {item.fingerprint: item for item in work.items}
    for record in records:
        assert reported[record.fingerprint].rule_id == record.rule_id
        assert reported[record.fingerprint].path == record.path
    assert not any(r.path == BILLING for r in records)


def test_a_kept_file_with_one_of_two_violations_fixed_records_one_fix(tmp_path):
    """The re-lint, not the kept path, decides: handlers.py goes to the PR
    with its print fixed and its raise untouched."""
    _work, gate, _remedies, records, _base = fixture_sweep(tmp_path, [HANDLERS_PRINT])

    assert HANDLERS in gate.paths_for_pr
    assert [(r.rule_id, r.path) for r in records if r.path == HANDLERS] == [
        ("no-bare-print", HANDLERS)]


def test_the_relint_refuses_a_tree_whose_lint_inputs_are_not_the_bases(tmp_path):
    """The re-lint reads `.curamoir/` from the work tree, so it must be the
    base's: a rule file appearing there after the gate would change what is
    reported, and so what counts as fixed."""
    def plant_rule(sandbox):
        (sandbox.path / ".curamoir" / "rules" / "extra.yaml").write_text("x: 1\n")

    with pytest.raises(ScopeCheckError, match=r"\.curamoir"):
        fixture_sweep(tmp_path, [HANDLERS_PRINT], before_relint=plant_rule)


def test_the_relint_refuses_a_kept_file_that_changed_after_the_gate(tmp_path):
    from curamoir.green_gate import GateError

    def rewrite(sandbox):
        (sandbox.path / HANDLERS).write_text("rewritten\n")

    with pytest.raises(GateError):
        fixture_sweep(tmp_path, [HANDLERS_PRINT], before_relint=rewrite)


def test_relint_needs_a_sandbox_that_was_linted_at_its_base(tmp_path):
    repo = committed_sample(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox, \
            pytest.raises(ScopeCheckError, match="lint_sandbox"):
        relint(sandbox, gate=None)


def violation(fingerprint, path="src/a.py", rule_id="no-print", principle="logging"):
    return ReportedViolation(
        path=path, line=1, col=1, end_line=1, end_col=2, severity="error",
        rule_id=rule_id, principle_id=principle, principle_rule="", message="",
        fix="", fix_source="rule", why_link="", match="print()", fingerprint=fingerprint)


def test_a_violation_rewritten_into_another_of_the_same_rule_is_not_a_fix():
    """The old fingerprint vanished, but a new one of the same rule appeared
    in the same file: the net is nothing fixed."""
    records = rule_fix_records([violation("aaa")], [violation("bbb")],
                               kept=["src/a.py"], remedied=(), base="b" * 40)
    assert records == ()


def test_fixing_the_first_of_two_identical_matches_is_one_fix():
    """Fingerprints count identical matches by occurrence, so fixing the
    first renumbers the second into the first's fingerprint: one vanishes,
    none appears, and one fix is recorded (attributed to the vanished
    fingerprint, the module's stated limit)."""
    records = rule_fix_records([violation("first"), violation("second")],
                               [violation("first")], kept=["src/a.py"], remedied=(),
                               base="b" * 40)
    assert [r.fingerprint for r in records] == ["second"]


def test_a_vanished_violation_outside_the_kept_paths_is_not_a_fix():
    records = rule_fix_records([violation("aaa", path="src/dropped.py")], [],
                               kept=["src/a.py"], remedied=(), base="b" * 40)
    assert records == ()


def test_a_fix_record_holds_its_source_to_its_shape():
    FixRecord("p", None, TIER1, "src/a.py", None, AGENT, "b" * 40)
    with pytest.raises(ValueError):
        FixRecord("p", None, RULE, "src/a.py", None, AGENT, "b" * 40)
    with pytest.raises(ValueError):
        FixRecord("p", "r", TIER1, "src/a.py", "f", AGENT, "b" * 40)
    with pytest.raises(ValueError):
        FixRecord("p", None, TIER1, "src/a.py", None, REMEDY, "b" * 40)
    with pytest.raises(ValueError):
        FixRecord("", "r", RULE, "src/a.py", "f", AGENT, "b" * 40)
    with pytest.raises(ValueError):
        FixRecord("p", "r", "other", "src/a.py", "f", AGENT, "b" * 40)


# --- the ledger line -------------------------------------------------------------------

def test_the_sweep_line_carries_the_records_the_counts_and_what_was_found(tmp_path):
    work, gate, _remedies, records, base = fixture_sweep(
        tmp_path, [BILLING_PRINT, BILLING_RAISE, HANDLERS_PRINT, HANDLERS_RAISE],
        command=RED_IF_QUANTITY_ZEROED)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with JobRun(ledger, "sweep") as run:
        run.succeeded("opened one PR", **gate.ledger_fields(),
                      **ledger_fields(work, records, base))

    (line,) = ledger.entries()
    assert line["outcome"] == "success" and line["job"] == "sweep"
    assert line["fixes"] == [record.as_dict() for record in records]
    assert line["fixes"][0] == {
        "principle_id": "errors-carry-context", "rule_id": "no-unchained-raise",
        "source": "rule", "path": HANDLERS, "fingerprint": records[0].fingerprint,
        "fixed_by": "agent", "base": base}
    assert line["fix_counts"] == {"errors-carry-context": 1,
                                  "no-file-variant-proliferation": 1,
                                  "structured-logging": 1}
    # Everything the base's lint found, fixed or not, by rule: what E4-R4's
    # introduced-vs-found compares between consecutive sweeps.
    assert line["found"]["base"] == base
    assert sorted(f for fps in line["found"]["fingerprints"].values() for f in fps) == sorted(
        item.fingerprint for item in work.items)
    assert line["found"]["fingerprints"]["no-bare-print"] == sorted(
        item.fingerprint for item in work.items if item.rule_id == "no-bare-print")
    assert line["found"]["rules_checked"] == list(work.report.rules_checked)
    assert line["found"]["tools"] == dict(work.report.tools)
    assert line["test_gate"]["strategy"] == REDUCED
    # And the reader reads back what was written.
    now = datetime.fromisoformat(line["at"])
    assert fix_counts(ledger.entries(), now=now, window_days=90) == line["fix_counts"]


def test_found_fields_alone_carry_no_fixes(tmp_path):
    repo = committed_sample(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        fields = found_fields(work, sandbox.base)
    assert set(fields) == {"found"}
    assert fields["found"]["base"] == sandbox.base


def test_ledger_fields_refuse_records_from_another_base(tmp_path):
    repo = committed_sample(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
    other = FixRecord("p", None, TIER1, "src/a.py", None, AGENT, "c" * 40)
    with pytest.raises(ValueError, match="base"):
        ledger_fields(work, [other], base="b" * 40)


# --- the reader ------------------------------------------------------------------------

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def fix(principle, fingerprint=None, rule_id="r", path="src/a.py"):
    if fingerprint is None:
        return {"principle_id": principle, "rule_id": None, "source": "tier1",
                "path": path, "fingerprint": None, "fixed_by": "agent", "base": "b" * 40}
    return {"principle_id": principle, "rule_id": rule_id, "source": "rule", "path": path,
            "fingerprint": fingerprint, "fixed_by": "agent", "base": "b" * 40}


_RUN = iter(range(10 ** 6))


def sweep_line(days_ago, fixes, outcome="success", job="sweep", run_id=None, kind="job"):
    return {"at": (NOW - timedelta(days=days_ago)).isoformat(), "kind": kind, "job": job,
            "outcome": outcome, "reason": None, "message": "",
            "run_id": run_id or f"run{next(_RUN)}", "fixes": fixes}


def test_three_structured_logging_fixes_in_30_days_reach_the_default_threshold():
    """The acceptance criterion's data half: what E4-R1 reads to file the one
    candidate. Filing it and drafting its rule are cm-kcq.1 and cm-kcq.2."""
    entries = [
        sweep_line(28, [fix("structured-logging", "f1")]),
        sweep_line(14, [fix("structured-logging", "f2")]),
        sweep_line(1, [fix("structured-logging", "f3")]),
    ]
    defaults = load_config({})["promotion"]
    counts = fix_counts(entries, now=NOW, window_days=defaults["window_days"])
    assert counts == {"structured-logging": 3}
    assert [p for p, n in counts.items() if n >= defaults["fix_threshold"]] == [
        "structured-logging"]


def test_rule_and_tier1_fixes_count_the_same():
    entries = [sweep_line(1, [fix("p", "f1"), fix("p"), fix("p")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 3}


def test_fixes_older_than_the_window_are_not_counted():
    entries = [sweep_line(91, [fix("p", "old")]), sweep_line(89, [fix("p", "new")]),
               sweep_line(90, [fix("p", "edge")]), sweep_line(29, [fix("p", "recent")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 3}
    assert fix_counts(entries, now=NOW, window_days=30) == {"p": 1}


def test_each_principle_is_counted_on_its_own():
    entries = [sweep_line(3, [fix("a", "1"), fix("b", "2")]), sweep_line(2, [fix("a", "3")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"a": 2, "b": 1}
    assert fix_counts(entries, now=NOW, window_days=90, principle="a") == {"a": 2}
    assert fix_counts(entries, now=NOW, window_days=90, principle="c") == {}


@pytest.mark.parametrize("outcome", ["failure", "refused", "unknown", None])
def test_a_sweep_line_that_did_not_succeed_counts_nothing(outcome):
    entries = [sweep_line(1, [fix("p", "f")], outcome=outcome)]
    assert fix_counts(entries, now=NOW, window_days=90) == {}


def test_only_sweep_job_lines_count():
    entries = [sweep_line(1, [fix("p", "f")], job="review"),
               sweep_line(1, [fix("p", "g")], kind="guard")]
    assert fix_counts(entries, now=NOW, window_days=90) == {}


def test_the_same_fingerprint_fixed_in_two_sweeps_is_two_fixes():
    """Fixed, reintroduced, fixed again: the "keeps recurring" signal the
    ladder exists to see."""
    entries = [sweep_line(20, [fix("p", "same")]), sweep_line(5, [fix("p", "same")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 2}


def test_a_fingerprint_repeated_within_one_line_is_one_fix():
    entries = [sweep_line(1, [fix("p", "same"), fix("p", "same")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 1}


def test_a_line_duplicated_by_a_merge_is_counted_once():
    line = sweep_line(1, [fix("p", "f")], run_id="abc")
    assert fix_counts([line, dict(line)], now=NOW, window_days=90) == {"p": 1}


def test_a_line_from_slightly_ahead_counts_and_one_from_far_ahead_does_not():
    """A writer's clock a few hours ahead must not hide the sweep just
    written; a line dated next month must not count for the whole window."""
    entries = [sweep_line(-0.25, [fix("p", "soon")]), sweep_line(-30, [fix("p", "later")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 1}


def test_malformed_lines_and_records_are_skipped_not_fatal():
    good = sweep_line(1, [fix("p", "ok")])
    entries = [
        42, None, "text", ["a"],
        {**sweep_line(1, []), "at": "yesterday"},
        {**sweep_line(1, []), "at": None},
        {**sweep_line(1, []), "fixes": "not a list"},
        {**sweep_line(1, []), "fixes": None},
        sweep_line(1, [42, None, {"principle_id": ""}, {"principle_id": 7, "source": "rule"},
                       {**fix("q", "x"), "source": "guessed"},
                       {**fix("q", "y"), "principle_id": None},
                       {**fix("q"), "fingerprint": ["x"]}]),
        {k: v for k, v in sweep_line(1, [fix("r", "z")]).items() if k != "fixes"},
        good,
    ]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 1}


def test_the_counts_are_in_a_stable_order():
    entries = [sweep_line(1, [fix("b", "1"), fix("a", "2"), fix("c", "3")])]
    assert list(fix_counts(entries, now=NOW, window_days=90)) == ["a", "b", "c"]


def test_a_naive_now_is_read_as_utc():
    entries = [sweep_line(1, [fix("p", "f")])]
    naive = NOW.replace(tzinfo=None)
    assert fix_counts(entries, now=naive, window_days=90) == {"p": 1}


@pytest.mark.parametrize("window", [0, -1, 2.5, True, "90", None])
def test_a_window_that_is_not_a_positive_whole_number_is_refused(window):
    with pytest.raises(ValueError, match="window_days"):
        fix_counts([], now=NOW, window_days=window)


def test_the_reader_reads_a_real_ledger_file_with_a_truncated_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    lines = [json.dumps(sweep_line(1, [fix("p", "a")])), '{"at": "2026-10-1',
             json.dumps(sweep_line(2, [fix("p", "b")]))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert fix_counts(Ledger(path).entries(), now=NOW, window_days=90) == {"p": 2}


# --- config ----------------------------------------------------------------------------

def test_the_promotion_defaults_are_the_specs():
    assert load_config({})["promotion"] == {"fix_threshold": 3, "window_days": 90}


@pytest.mark.parametrize("key", ["fix_threshold", "window_days"])
def test_a_zero_promotion_key_is_refused(key):
    """0 is a COUNT, but not a promotion setting: a threshold of 0 makes
    every principle a candidate, fixed or not, and a window of 0 days counts
    nothing ever."""
    with pytest.raises(ConfigError, match=f"promotion.{key}"):
        load_config({"promotion": {key: 0}})
