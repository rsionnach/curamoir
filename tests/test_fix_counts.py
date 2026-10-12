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
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import curamoir.fix_counts as counts_module
from curamoir.config import ConfigError, load_config
from curamoir.fix_counts import (
    AGENT,
    DELTA_CAP,
    REMEDY,
    RULE,
    TIER1,
    FixRecord,
    fix_counts,
    found_delta,
    found_set,
    ledger_fields,
    read_snapshot,
    record_fixes,
    relint,
    rule_fix_records,
    snapshot_path,
    write_snapshot,
)
from curamoir.fix_scope import ScopeCheckError, lint_sandbox, verify_fix
from curamoir.git_guard import prepare_sandbox, snapshot_git_state
from curamoir.green_gate import REDUCED, run_test_gate
from curamoir.job_run import JobRun
from curamoir.ledger import Ledger
from curamoir.lint_report import ReportedViolation
from curamoir.remedies import apply_file_remedies
from curamoir.sweep import LintUntrusted
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


def fixture_sweep(tmp_path, edits, command="true", before_relint=None, relint_options=None):
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
        records = record_fixes(sandbox, gate, remedies, **(relint_options or {}))
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

def test_the_sweep_line_carries_the_records_the_summary_and_the_found_delta(tmp_path):
    work, gate, _remedies, records, base = fixture_sweep(
        tmp_path, [BILLING_PRINT, BILLING_RAISE, HANDLERS_PRINT, HANDLERS_RAISE],
        command=RED_IF_QUANTITY_ZEROED)
    delta = found_delta(None, found_set(work, base))
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with JobRun(ledger, "sweep") as run:
        run.succeeded("opened one PR", **gate.ledger_fields(), **ledger_fields(records, delta))

    (line,) = ledger.entries()
    assert line["outcome"] == "success" and line["job"] == "sweep"
    assert line["fixes"] == [record.as_dict() for record in records]
    assert line["fixes"][0] == {
        "principle_id": "errors-carry-context", "rule_id": "no-unchained-raise",
        "source": "rule", "path": HANDLERS, "fingerprint": records[0].fingerprint,
        "fixed_by": "agent", "base": base}
    assert line["fix_summary"] == {"errors-carry-context": 1,
                                   "no-file-variant-proliferation": 1,
                                   "structured-logging": 1}
    assert "fix_counts" not in line and "found" not in line
    # No snapshot yet: everything the base's lint found is `added`, and the
    # line says it is a baseline, not "introduced".
    found = line["found_delta"]
    assert found["base"] == base and found["previous_base"] is None
    assert found["baseline"] is True and found["truncated"] is False
    assert sorted(f for fps in found["added"].values() for f in fps) == sorted(
        item.fingerprint for item in work.items)
    assert found["removed"] == {}
    assert found["rules_checked"] == list(work.report.rules_checked)
    assert found["tools"] == dict(work.report.tools)
    assert line["test_gate"]["strategy"] == REDUCED
    # And the reader reads back what was written.
    now = datetime.fromisoformat(line["at"])
    assert fix_counts(ledger.entries(), now=now, window_days=90) == line["fix_summary"]


def test_ledger_fields_refuse_records_from_another_base():
    delta = found_delta(None, synthetic_found({"r": ["f"]}, base="b" * 40))
    other = FixRecord("p", None, TIER1, "src/a.py", None, AGENT, "c" * 40)
    with pytest.raises(ValueError, match="base"):
        ledger_fields([other], delta)


def test_the_relint_failing_is_a_refusal_not_zero_fixes(tmp_path):
    """A re-lint that exits 2 (or times out) must raise, so the runner
    refuses: recording zero fixes would starve the ladder silently."""
    lint = tmp_path / "bin" / "broken-lint"
    lint.parent.mkdir()
    lint.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(2)\n", encoding="utf-8")
    lint.chmod(0o755)
    with pytest.raises(LintUntrusted):
        fixture_sweep(tmp_path, [HANDLERS_PRINT], relint_options={"lint": lint})


# --- what was found, as a delta against a rolling snapshot -----------------------------

def synthetic_found(fingerprints, base="a" * 40, rules=None):
    items = [SimpleNamespace(rule_id=rule, fingerprint=fp)
             for rule, prints in fingerprints.items() for fp in prints]
    report = SimpleNamespace(rules_checked=tuple(rules or sorted(fingerprints)),
                             tools={"curamoir-lint": "test"})
    return found_set(SimpleNamespace(items=items, report=report), base)


def test_the_second_sweep_records_only_what_changed():
    first = synthetic_found({"r": ["f1", "f2"], "s": ["g1"]}, base="a" * 40)
    second = synthetic_found({"r": ["f2", "f3"], "s": ["g1"]}, base="c" * 40)
    delta = found_delta(first, second)
    assert delta["baseline"] is False and delta["truncated"] is False
    assert delta["base"] == "c" * 40 and delta["previous_base"] == "a" * 40
    assert delta["added"] == {"r": ["f3"]} and delta["removed"] == {"r": ["f1"]}
    assert delta["added_count"] == 1 and delta["removed_count"] == 1
    assert delta["previous_rules_checked"] == ["r", "s"]
    assert delta["digest"] == second["digest"] != first["digest"]


def test_a_rule_new_since_the_snapshot_is_added_but_not_in_the_previous_rules():
    first = synthetic_found({"r": ["f1"]})
    second = synthetic_found({"r": ["f1"], "new": ["n1"]})
    delta = found_delta(first, second)
    assert delta["baseline"] is False and delta["added"] == {"new": ["n1"]}
    assert "new" not in delta["previous_rules_checked"]


def test_a_delta_too_big_records_counts_only():
    first = synthetic_found({"r": []})
    second = synthetic_found({"r": [f"f{i}" for i in range(DELTA_CAP + 1)]})
    delta = found_delta(first, second)
    assert delta["truncated"] is True
    assert delta["added"] is None and delta["removed"] is None
    assert delta["added_count"] == DELTA_CAP + 1 and delta["removed_count"] == 0
    assert delta["added_counts"] == {"r": DELTA_CAP + 1}


def violation_set(count, base, churn=0):
    """`count` violations over 50 rules and 500 files; `churn` of them
    replaced by new ones."""
    prints = {}
    for i in range(churn, count + churn):
        prints.setdefault(f"rule-{i % 50:02d}", []).append(f"{i:064x}")
    return synthetic_found(prints, base=base)


def test_a_steady_state_sweep_line_stays_small_with_10k_violations(tmp_path):
    """The ledger is read whole before every job (the budget guard,
    /curamoir:status): a line carrying every fingerprint was ~700 KB at
    10k violations. A delta with small churn is a few KB."""
    state = tmp_path / ".curamoir" / "state"
    ledger = Ledger(state / "ledger.jsonl")
    first, second = violation_set(10_000, "a" * 40), violation_set(10_000, "c" * 40, churn=5)
    for found in (first, second):
        delta = found_delta(read_snapshot(snapshot_path(tmp_path)), found)
        with JobRun(ledger, "sweep") as run:
            run.succeeded("ok", **ledger_fields((), delta))
        write_snapshot(snapshot_path(tmp_path), found)
    lines = (state / "ledger.jsonl").read_bytes().splitlines()
    assert len(lines[0]) < 50_000, "the baseline line is truncated to counts"
    assert len(lines[1]) < 50_000
    second_line = ledger.entries()[1]["found_delta"]
    assert second_line["baseline"] is False
    assert second_line["added_count"] == 5 and second_line["removed_count"] == 5


def test_the_snapshot_lives_next_to_the_ledger_and_round_trips(tmp_path):
    path = snapshot_path(tmp_path)
    assert path == tmp_path / ".curamoir" / "state" / "found.json"
    assert read_snapshot(path) is None
    found = synthetic_found({"r": ["f1", "f2"]})
    write_snapshot(path, found)
    assert read_snapshot(path) == found
    assert [p.name for p in path.parent.iterdir()] == ["found.json"], "no temp file left"


def test_the_snapshot_is_replaced_atomically(tmp_path, monkeypatch):
    """Written to a temp file in the same directory, then os.replace'd: a
    writer killed mid-write leaves the old snapshot whole."""
    path = snapshot_path(tmp_path)
    old = synthetic_found({"r": ["f1"]})
    write_snapshot(path, old)
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src).parent, Path(dst)))
        assert read_snapshot(path) == old, "the old snapshot is intact until the replace"
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_snapshot(path, synthetic_found({"r": ["f2"]}))
    assert replaced == [(path.parent, path)]

    def crash(src, dst):
        raise OSError("killed")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        write_snapshot(path, synthetic_found({"r": ["f3"]}))
    assert read_snapshot(path)["fingerprints"] == {"r": ["f2"]}
    assert [p.name for p in path.parent.iterdir()] == ["found.json"]


def test_the_snapshot_is_never_written_inside_the_sandbox(tmp_path):
    repo = committed_sample(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        with pytest.raises(ValueError, match="sandbox"):
            write_snapshot(snapshot_path(sandbox.path), synthetic_found({"r": []}),
                           sandbox=sandbox)
        write_snapshot(snapshot_path(repo), synthetic_found({"r": []}), sandbox=sandbox)
    assert read_snapshot(snapshot_path(repo)) is not None


@pytest.mark.parametrize("content", [
    "", "{", "[]", '{"base": "x"}', "null",
    json.dumps({"version": 1, "base": "a", "digest": "0" * 64,
                "fingerprints": {"r": ["f"]}, "rules_checked": ["r"], "tools": {}}),
])
def test_a_corrupt_snapshot_makes_the_next_line_a_baseline(tmp_path, content):
    path = snapshot_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert read_snapshot(path) is None
    delta = found_delta(read_snapshot(path), synthetic_found({"r": ["f"]}))
    assert delta["baseline"] is True and delta["added"] == {"r": ["f"]}


def git_in(repo, *args):
    """Real git, isolated from the user's config, so a global signing or
    hook setting cannot change what the test commits."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    result = subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return result.stdout.strip()


def commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    git_in(repo, "add", name)
    git_in(repo, "commit", "-q", "-m", name)
    return git_in(repo, "rev-parse", "HEAD")


def test_a_snapshot_of_an_ancestor_base_is_compared_and_one_of_a_rewritten_branch_is_not(
        tmp_path):
    """A force-push or rebase leaves the snapshot's base off the current
    base's history: a delta against it compares unrelated trees, so it
    is read as no snapshot (a baseline)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    root = commit(repo, "a")
    rewritten = commit(repo, "b")
    child = commit(repo, "c")
    previous = synthetic_found({"r": ["f1"]}, base=rewritten)
    assert counts_module.snapshot_for(repo, child, previous) == previous
    assert counts_module.snapshot_for(repo, rewritten, previous) == previous, "same base"
    git_in(repo, "reset", "-q", "--hard", root)
    current = commit(repo, "d")
    assert counts_module.snapshot_for(repo, current, previous) is None
    delta = found_delta(counts_module.snapshot_for(repo, current, previous),
                        synthetic_found({"r": ["f1"]}, base=current))
    assert delta["baseline"] is True and delta["previous_base"] is None
    unknown = synthetic_found({"r": ["f1"]}, base="e" * 40)
    assert counts_module.snapshot_for(repo, current, unknown) is None, "not in the repo"
    assert counts_module.snapshot_for(repo, current, None) is None


def test_a_snapshot_write_that_fails_after_the_success_line_warns_and_keeps_the_old(tmp_path):
    """The job already succeeded and its line is recorded; a snapshot the
    runner cannot write must not turn that into a crash. The next line's
    previous_digest then shows the gap."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    path = snapshot_path(tmp_path)
    old = synthetic_found({"r": ["f1"]})
    write_snapshot(path, old)
    path.parent.chmod(0o555)
    try:
        with pytest.warns(RuntimeWarning, match="snapshot"):
            written = counts_module.write_snapshot_or_warn(path, synthetic_found({"r": ["f2"]}))
    finally:
        path.parent.chmod(0o755)
    assert written is False
    assert read_snapshot(path) == old
    assert [p.name for p in path.parent.iterdir()] == ["found.json"], "no temp file left"
    assert counts_module.write_snapshot_or_warn(path, synthetic_found({"r": ["f2"]})) is True
    assert read_snapshot(path)["fingerprints"] == {"r": ["f2"]}


def test_the_snapshot_file_has_one_fingerprint_per_line_and_the_compact_digest(tmp_path):
    """Committed, so git diffs, merges and delta-compresses it line by line;
    the digest stays over the canonical compact JSON of `fingerprints`."""
    path = snapshot_path(tmp_path)
    found = synthetic_found({"r": ["f1", "f2"], "s": ["g1"]})
    write_snapshot(path, found)
    lines = path.read_text(encoding="utf-8").splitlines()
    for fingerprint in ("f1", "f2", "g1"):
        assert sum(fingerprint in line for line in lines) == 1
        assert any(line.strip() in (f'"{fingerprint}"', f'"{fingerprint}",') for line in lines)
    assert read_snapshot(path) == found
    assert path.read_text(encoding="utf-8").endswith("\n")
    tampered = path.read_text(encoding="utf-8").replace('"f1"', '"f9"')
    path.write_text(tampered, encoding="utf-8")
    assert read_snapshot(path) is None, "the digest is still verified"


def test_found_set_is_the_whole_base_lint_by_rule(tmp_path):
    repo = committed_sample(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        found = found_set(work, sandbox.base)
    assert found["base"] == sandbox.base
    assert sorted(f for fps in found["fingerprints"].values() for f in fps) == sorted(
        item.fingerprint for item in work.items)
    assert len(found["digest"]) == 64


# --- the reader ------------------------------------------------------------------------

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def rule_fix(principle, fingerprint, rule_id="r", path="src/a.py"):
    return {"principle_id": principle, "rule_id": rule_id, "source": "rule", "path": path,
            "fingerprint": fingerprint, "fixed_by": "agent", "base": "b" * 40}


def tier1_fix(principle, path="src/a.py"):
    return {"principle_id": principle, "rule_id": None, "source": "tier1",
            "path": path, "fingerprint": None, "fixed_by": "agent", "base": "b" * 40}


_RUN = iter(range(10 ** 6))


def sweep_line(days_ago, fixes, outcome="success", job="sweep", run_id=None, kind="job"):
    return {"at": (NOW - timedelta(days=days_ago)).isoformat(), "kind": kind, "job": job,
            "outcome": outcome, "reason": None, "message": "",
            "run_id": run_id or f"run{next(_RUN)}", "fixes": fixes}


def test_three_structured_logging_fixes_in_30_days_reach_the_default_threshold():
    """The acceptance criterion's data half: what E4-R1 reads to file the one
    candidate. Filing it and drafting its rule are cm-kcq.1 and cm-kcq.2."""
    entries = [
        sweep_line(28, [rule_fix("structured-logging", "f1")]),
        sweep_line(14, [rule_fix("structured-logging", "f2")]),
        sweep_line(1, [rule_fix("structured-logging", "f3")]),
    ]
    defaults = load_config({})["promotion"]
    counts = fix_counts(entries, now=NOW, window_days=defaults["window_days"])
    assert counts == {"structured-logging": 3}
    assert [p for p, n in counts.items() if n >= defaults["fix_threshold"]] == [
        "structured-logging"]


def test_rule_and_tier1_fixes_count_the_same():
    entries = [sweep_line(1, [rule_fix("p", "f1"), tier1_fix("p"), tier1_fix("p")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 3}


def test_fixes_older_than_the_window_are_not_counted():
    entries = [sweep_line(91, [rule_fix("p", "old")]), sweep_line(89, [rule_fix("p", "new")]),
               sweep_line(90, [rule_fix("p", "edge")]), sweep_line(29, [rule_fix("p", "recent")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 3}
    assert fix_counts(entries, now=NOW, window_days=30) == {"p": 1}


def test_each_principle_is_counted_on_its_own():
    entries = [sweep_line(3, [rule_fix("a", "1"), rule_fix("b", "2")]), sweep_line(2, [rule_fix("a", "3")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"a": 2, "b": 1}
    assert fix_counts(entries, now=NOW, window_days=90, principle="a") == {"a": 2}
    assert fix_counts(entries, now=NOW, window_days=90, principle="c") == {}


@pytest.mark.parametrize("outcome", ["failure", "refused", "unknown", None])
def test_a_sweep_line_that_did_not_succeed_counts_nothing(outcome):
    entries = [sweep_line(1, [rule_fix("p", "f")], outcome=outcome)]
    assert fix_counts(entries, now=NOW, window_days=90) == {}


def test_only_sweep_job_lines_count():
    entries = [sweep_line(1, [rule_fix("p", "f")], job="review"),
               sweep_line(1, [rule_fix("p", "g")], kind="guard")]
    assert fix_counts(entries, now=NOW, window_days=90) == {}


def test_the_same_fingerprint_fixed_in_two_sweeps_is_two_fixes():
    """Fixed, reintroduced, fixed again: the "keeps recurring" signal the
    ladder exists to see."""
    entries = [sweep_line(20, [rule_fix("p", "same")]), sweep_line(5, [rule_fix("p", "same")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 2}


def test_a_fingerprint_repeated_within_one_line_is_one_fix():
    entries = [sweep_line(1, [rule_fix("p", "same"), rule_fix("p", "same")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 1}


def test_a_line_duplicated_by_a_merge_is_counted_once():
    line = sweep_line(1, [rule_fix("p", "f")], run_id="abc")
    assert fix_counts([line, dict(line)], now=NOW, window_days=90) == {"p": 1}


def test_a_line_from_slightly_ahead_counts_and_one_from_far_ahead_does_not():
    """A writer's clock a few hours ahead must not hide the sweep just
    written; a line dated next month must not count for the whole window."""
    entries = [sweep_line(-0.25, [rule_fix("p", "soon")]), sweep_line(-30, [rule_fix("p", "later")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 1}


def test_malformed_lines_and_records_are_skipped_not_fatal():
    good = sweep_line(1, [rule_fix("p", "ok")])
    entries = [
        42, None, "text", ["a"],
        {**sweep_line(1, []), "at": "yesterday"},
        {**sweep_line(1, []), "at": None},
        {**sweep_line(1, []), "fixes": "not a list"},
        {**sweep_line(1, []), "fixes": None},
        sweep_line(1, [42, None, {"principle_id": ""}, {"principle_id": 7, "source": "rule"},
                       {**rule_fix("q", "x"), "source": "guessed"},
                       {**rule_fix("q", "y"), "principle_id": None},
                       {**tier1_fix("q"), "fingerprint": ["x"]}]),
        {k: v for k, v in sweep_line(1, [rule_fix("r", "z")]).items() if k != "fixes"},
        good,
    ]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 1}


def test_the_counts_are_in_a_stable_order():
    entries = [sweep_line(1, [rule_fix("b", "1"), rule_fix("a", "2"), rule_fix("c", "3")])]
    assert list(fix_counts(entries, now=NOW, window_days=90)) == ["a", "b", "c"]


def test_a_naive_now_is_read_as_utc():
    entries = [sweep_line(1, [rule_fix("p", "f")])]
    naive = NOW.replace(tzinfo=None)
    assert fix_counts(entries, now=naive, window_days=90) == {"p": 1}


@pytest.mark.parametrize("window", [0, -1, 2.5, True, "90", None])
def test_a_window_that_is_not_a_positive_whole_number_is_refused(window):
    with pytest.raises(ValueError, match="window_days"):
        fix_counts([], now=NOW, window_days=window)


def test_the_reader_reads_a_real_ledger_file_with_a_truncated_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    lines = [json.dumps(sweep_line(1, [rule_fix("p", "a")])), '{"at": "2026-10-1',
             json.dumps(sweep_line(2, [rule_fix("p", "b")]))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert fix_counts(Ledger(path).entries(), now=NOW, window_days=90) == {"p": 2}


def test_a_malformed_first_copy_of_a_line_does_not_hide_a_valid_second():
    good = sweep_line(1, [rule_fix("p", "f")], run_id="dup")
    assert fix_counts([{**good, "fixes": "oops"}, good], now=NOW, window_days=90) == {"p": 1}
    assert fix_counts([{**good, "fixes": [42]}, good], now=NOW, window_days=90) == {"p": 1}


def test_an_empty_first_copy_of_a_line_does_not_hide_a_populated_second():
    """One sweep writes one line; two copies that differ can only be a merge
    mangling one, and an emptied `fixes` is the likelier damage than
    invented records, so the copy with records wins."""
    populated = sweep_line(1, [rule_fix("p", "f")], run_id="dup")
    empty = {**populated, "fixes": []}
    assert fix_counts([empty, populated], now=NOW, window_days=90) == {"p": 1}
    assert fix_counts([populated, empty], now=NOW, window_days=90) == {"p": 1}
    assert fix_counts([empty, dict(empty)], now=NOW, window_days=90) == {}


def test_of_two_valid_copies_of_a_line_the_first_wins():
    first = sweep_line(1, [rule_fix("p", "f")], run_id="dup")
    second = sweep_line(1, [rule_fix("p", "f"), rule_fix("q", "g")], run_id="dup")
    assert fix_counts([first, second], now=NOW, window_days=90) == {"p": 1}
    assert fix_counts([second, first], now=NOW, window_days=90) == {"p": 1, "q": 1}


@pytest.mark.parametrize("run_id", [5, ["x"], {"a": 1}, ""])
def test_a_run_id_that_is_not_text_counts_but_cannot_dedupe(run_id):
    line = {**sweep_line(1, [rule_fix("p", "f")]), "run_id": run_id}
    assert fix_counts([line, dict(line)], now=NOW, window_days=90) == {"p": 2}


def test_the_same_fingerprint_under_two_principles_is_two_fixes():
    entries = [sweep_line(1, [rule_fix("p", "f"), rule_fix("q", "f")])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"p": 1, "q": 1}


def test_the_reader_skips_what_the_writer_would_refuse():
    entries = [sweep_line(1, [
        {**tier1_fix("p"), "rule_id": "r"},
        {**tier1_fix("p"), "fixed_by": "remedy"},
        {**rule_fix("p", "f"), "fixed_by": "someone"},
        {**rule_fix("p", "g"), "base": ""},
        rule_fix("q", "ok"),
    ])]
    assert fix_counts(entries, now=NOW, window_days=90) == {"q": 1}


@pytest.mark.parametrize("now", ["2026-10-12", None, 1760270400,
                                 datetime.max.replace(tzinfo=timezone.utc),
                                 datetime.min.replace(tzinfo=timezone.utc)])
def test_a_now_that_is_not_a_usable_datetime_is_refused(now):
    with pytest.raises(ValueError, match="now"):
        fix_counts([], now=now, window_days=90)


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
