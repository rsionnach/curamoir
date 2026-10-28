"""E3-R1b (cm-fkd.2): the tier-1 scan for principle violations with no rule.

Acceptance criteria, and the tests that hold them:

- "A tier-1 fix (no rule exists yet) is tagged with its principle id in the
  PR body and in the ledger, so the promotion counter counts it":
  test_a_tier1_fix_is_tagged_in_the_ledger_and_the_pr_and_counted.
- "Tier-1 scanning can be disabled and the sweep still functions on lint
  output alone": test_with_tier1_off_the_sweep_runs_on_the_lint_alone.

No test calls a model. The scan and the fix step are stand-in `claude`
scripts at fix_step's `claude=` seam, printing the `--output-format json`
result object as the CLI docs give it (tests/claude_payloads.py, which
says where the shape comes from).

**Fixture provenance.** The committed `fixtures/sample-repo` with two of
tests/test_lint.py's three regex rules: without `no-unchained-raise`, the
fixture's own `errors-carry-context` principle has no rule, so it is tier
1, and its two planted violations (PLANTED.json: billing.py:37,
handlers.py:31) are what the stand-in scan reports, quoted from the files.
"""

import contextlib
import json
import os
import pathlib
import signal
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from curamoir import tier1
from curamoir.config import ConfigError, load_config
from curamoir.fix_counts import (
    TIER1,
    FixRecord,
    fix_counts,
    found_delta,
    found_set,
    ledger_fields,
    record_fixes,
)
from curamoir.fix_scope import (
    OUTSIDE_VIOLATION,
    SIGNATURE_CHANGE,
    FileChange,
    ScopeCheckError,
    base_principles,
    check_scope,
    lint_sandbox,
    verify_fix,
)
from curamoir.fix_step import fix_prompt, fix_step_argv, run_fix_step
from curamoir.git_guard import prepare_sandbox
from curamoir.green_gate import GREEN, run_test_gate
from curamoir.idempotence import (
    DUPLICATE_OPEN_PR,
    NO_WORK,
    TIER1_FAILED,
    WORK,
    pr_field,
)
from curamoir.job_run import JobRun
from curamoir.ledger import Ledger
from curamoir.principles import parse_principles
from curamoir.remedies import apply_file_remedies
from curamoir.rules import load_rules
from curamoir.sweep import work_list, work_list_document
from curamoir.tier1 import (
    ALREADY_SCANNED,
    ATTEMPTS_EXHAUSTED,
    BAD_SPAN,
    FAILED,
    MALFORMED,
    MAX_SCAN_ATTEMPTS,
    MAX_TIER1_SPAN,
    NEAR_LINT_VIOLATION,
    NO_CANDIDATES,
    NO_ELIGIBLE,
    NOT_A_CANDIDATE,
    NOT_ELIGIBLE,
    OFF,
    OVER_CAP,
    OVERLAPS_FINDING,
    QUOTE_MISMATCH,
    READY,
    SANDBOX_CHANGED,
    SCAN_TOOLS,
    SCANNED,
    SPAN_TOO_LONG,
    UNKNOWN_PRINCIPLE,
    Tier1Finding,
    Tier1Plan,
    Tier1ReplyError,
    Tier1Scan,
    already_scanned,
    check_findings,
    choose_candidates,
    eligible_principles,
    parse_findings,
    parse_fix_replies,
    plan_scan,
    record_tier1_fixes,
    rotation_origin,
    run_tier1_scan,
    tier1_fix_records,
    tier1_pr_lines,
    touches,
    trim_pr_paths,
    with_findings,
)
from tests import claude_payloads
from tests.conftest import committed_sample
from tests.test_idempotence import decide_under_default_prefix
from tests.test_lint import FIXTURE, NO_BARE_PRINT, NO_VARIANT_MODULE

BILLING = "src/sample/core/billing.py"
HANDLERS = "src/sample/web/handlers.py"
VARIANT = "src/sample/web/handlers_v2.py"
ECC = "errors-carry-context"
BILLING_RAISE_LINE = 'raise RuntimeError("could not load rates")  # PLANTED: errors-carry-context'
HANDLERS_RAISE_LINE = 'raise ValueError("bad token")  # PLANTED: errors-carry-context'

#: The two plants of errors-carry-context, as a scan would report them.
FOUND_BILLING = {"principle_id": ECC, "path": BILLING, "line": 37, "end_line": 37,
                 "quote": BILLING_RAISE_LINE, "note": "re-raise drops the cause and the path"}
FOUND_HANDLERS = {"principle_id": ECC, "path": HANDLERS, "line": 31, "end_line": 31,
                  "quote": HANDLERS_RAISE_LINE, "note": "re-raise drops the cause"}

#: The agent's fixes (tests/test_fix_counts.py's edits, without the red one).
EDITS = [
    (BILLING, '    print("calculating line total")', '    logger.info("calculating line total")'),
    (BILLING, "    except OSError:\n        raise RuntimeError(\"could not load rates\")",
     "    except OSError as error:\n"
     "        raise RuntimeError(f\"could not load rates from {path}\") from error"),
    (HANDLERS, 'print(f"request failed: {exception}")',
     'logger.error("request failed: %s", exception)'),
    (HANDLERS, '    except IndexError:\n        raise ValueError("bad token")',
     '    except IndexError as error:\n        raise ValueError(f"bad token: {raw!r}") from error'),
]


def finding(line, end_line, path="m.py", principle_id="p"):
    return Tier1Finding("t1-01", principle_id, path, line, end_line, "q", "n")


def hunk(old_start, old_count):
    return SimpleNamespace(old_start=old_start, old_count=old_count)


def clean_lint_repo(tmp_path):
    """The committed fixture with only `no-bare-print`, and its two prints
    committed as logger calls: the lint is clean, and two principles have
    no rule (tier 1 is eligible)."""
    repo = committed_sample(tmp_path, NO_BARE_PRINT)
    for path, old, new in (EDITS[0], EDITS[2]):
        target = repo / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new),
                          encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "no prints"], cwd=repo, check=True)
    return repo


def seed(status, base, outcome="success", **scan_fields):
    """A sweep ledger line whose `tier1` field is what Tier1Scan writes."""
    return {"kind": "job", "job": "sweep", "outcome": outcome,
            "tier1": Tier1Scan(status, base, **scan_fields).ledger_fields()["tier1"]}


def tier1_repo(tmp_path, config="version: 1\nsweep:\n  test_command: \"true\"\n"):
    """The committed fixture with errors-carry-context left without a rule."""
    repo = committed_sample(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE)
    (repo / ".curamoir" / "curamoir.yaml").write_text(config, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "config"], cwd=repo, check=True)
    return repo


def fake_claude(tmp_path, name, result, edits=(), writes=None, exit_code=0, payload=None):
    """A stand-in `claude` (tests/test_fix_step.py's, with the reply text as
    a parameter): logs its argv, makes `edits` and `writes` in its working
    directory, and prints the `--output-format json` result object:
    `payload`, or a documented success with `result` (tests/claude_payloads)."""
    script = tmp_path / "bin" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "bin" / f"{name}.json"
    payload = claude_payloads.success(result) if payload is None else payload
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], "
        "'cwd': os.getcwd()}))\n"
        f"for path, old, new in {list(edits)!r}:\n"
        "    text = open(path, encoding='utf-8').read()\n"
        "    open(path, 'w', encoding='utf-8').write(text.replace(old, new))\n"
        f"for path, text in {dict(writes or {})!r}.items():\n"
        "    open(path, 'w', encoding='utf-8').write(text)\n"
        f"print(json.dumps({payload!r}))\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8")
    script.chmod(0o755)
    return str(script), log


def findings_reply(*findings):
    return "Here is what I found.\n\n```json\n" + json.dumps(
        {"findings": list(findings)}, indent=2) + "\n```\n"


def fixture_principles():
    return parse_principles((FIXTURE / ".curamoir" / "principles.md").read_text("utf-8"))


def fixture_text(path):
    return (FIXTURE / path).read_text(encoding="utf-8")


def check_findings_on_fixture(raw, *, candidates=(BILLING, HANDLERS), lint_items=(),
                              eligible=(ECC,)):
    known = {p.id for p in fixture_principles().principles}
    return check_findings(raw, known=known, eligible=set(eligible), candidates=candidates,
                          base_text=fixture_text, lint_items=lint_items)


def _at(line):
    return datetime.fromisoformat(line["at"])


# --- the acceptance criteria -----------------------------------------------------------

def test_a_tier1_fix_is_tagged_in_the_ledger_and_the_pr_and_counted(tmp_path):
    """The runner's steps (fix_scope, "What the runner does"), with tier 1:
    the scan reports both plants of the rule-less principle, the fix agent
    fixes them and the lint's violations, and the line written when the PR
    opens carries each tier-1 fix under its principle id, which the
    promotion counter counts; the PR body lists them by principle id."""
    repo = tier1_repo(tmp_path)
    scan_claude, scan_log = fake_claude(tmp_path, "scan",
                                        findings_reply(FOUND_BILLING, FOUND_HANDLERS))
    fix_claude, _ = fake_claude(tmp_path, "fix", "t1-01 fixed\nt1-02 fixed", edits=EDITS)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        assert {item.principle_id for item in work.items} == {
            "structured-logging", "no-file-variant-proliferation"}
        plan = plan_scan(sandbox, ledger.entries())
        assert plan.status == READY and [p.id for p in plan.eligible] == [ECC]
        decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [],
                                  tier1=plan.will_scan)
        decision.apply_to(sandbox)
        scan = run_tier1_scan(sandbox, plan, decision.held_paths, ledger.entries(),
                              claude=scan_claude)
        assert scan.status == SCANNED and scan.refused == ()
        assert [(f.id, f.path, f.span) for f in scan.findings] == [
            ("t1-01", BILLING, (37, 37)), ("t1-02", HANDLERS, (31, 31))]
        sandbox.work_list = with_findings(sandbox.work_list, scan)
        assert decision.after_tier1(sandbox.work_list, scan) is decision
        remedies = apply_file_remedies(sandbox)
        prompt = fix_prompt(remedies.for_agent(sandbox.work_list), base_principles(sandbox))
        assert "t1-01" in prompt and "tier-1 finding" in prompt
        fix_run = run_fix_step(sandbox, prompt, claude=fix_claude)
        verdict = verify_fix(sandbox, git_state=fix_run.git_state)
        assert verdict.ok, verdict.render()
        gate = run_test_gate(sandbox, verdict,
                             paths=trim_pr_paths(verdict.paths, sandbox.work_list, 20))
        assert gate.strategy == GREEN
        records = record_fixes(sandbox, gate, remedies)
        records += record_tier1_fixes(sandbox, gate, fix_run)
        delta = found_delta(None, found_set(work, sandbox.base))
        with JobRun(ledger, "sweep") as run:
            run.succeeded("opened one PR", **ledger_fields(records, delta),
                          **scan.ledger_fields())
        body = tier1_pr_lines(records, sandbox.work_list.tier1)

    (line,) = ledger.entries()
    tier1_fixes = [fix for fix in line["fixes"] if fix["source"] == TIER1]
    assert tier1_fixes == [
        {"principle_id": ECC, "rule_id": None, "source": "tier1", "path": BILLING,
         "fingerprint": None, "fixed_by": "agent", "base": line["tier1"]["base"],
         "span": [37, 37]},
        {"principle_id": ECC, "rule_id": None, "source": "tier1", "path": HANDLERS,
         "fingerprint": None, "fixed_by": "agent", "base": line["tier1"]["base"],
         "span": [31, 31]}]
    assert line["tier1"]["scanned"] is True and line["tier1"]["findings"] == 2
    counts = fix_counts(ledger.entries(), now=_at(line), window_days=90)
    assert counts == {ECC: 2, "no-file-variant-proliferation": 1, "structured-logging": 2}
    assert body == [
        f"- `{ECC}` (tier 1: no rule yet) `{BILLING}` line 37: re-raise drops the cause and "
        "the path",
        f"- `{ECC}` (tier 1: no rule yet) `{HANDLERS}` line 31: re-raise drops the cause"]
    # The scan was read-only and saw the lint's paths first.
    argv = json.loads(scan_log.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "Edit(" not in argv[argv.index("--allowedTools") + 1]


def test_with_tier1_off_the_sweep_runs_on_the_lint_alone(tmp_path):
    """`sweep.tier1_scan: false`: no scan is planned, so nothing is called,
    the decision is the lint's, the agent sees the lint's work list only,
    and the line records only rule fixes and `tier1.status: off`."""
    repo = tier1_repo(tmp_path, "version: 1\nsweep:\n  test_command: \"true\"\n"
                                "  tier1_scan: false\n")
    fix_claude, _ = fake_claude(tmp_path, "fix", "done", edits=EDITS[:1] + EDITS[2:3])
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        plan = plan_scan(sandbox, ledger.entries())
        assert plan.status == OFF and not plan.will_scan
        with pytest.raises(ValueError):
            run_tier1_scan(sandbox, plan, (), ledger.entries(), claude="/nonexistent")
        decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [],
                                  tier1=plan.will_scan)
        decision.apply_to(sandbox)
        remedies = apply_file_remedies(sandbox)
        agent_work = remedies.for_agent(sandbox.work_list)
        assert agent_work.tier1 == () and work_list_document(agent_work)["tier1"] == []
        prompt = fix_prompt(agent_work, base_principles(sandbox))
        assert "tier-1" not in prompt
        fix_run = run_fix_step(sandbox, prompt, claude=fix_claude)
        verdict = verify_fix(sandbox, git_state=fix_run.git_state)
        assert verdict.ok, verdict.render()
        gate = run_test_gate(sandbox, verdict)
        records = record_fixes(sandbox, gate, remedies)
        assert record_tier1_fixes(sandbox, gate, fix_run) == ()
        with JobRun(ledger, "sweep") as run:
            run.succeeded("opened one PR",
                          **ledger_fields(records, found_delta(None, found_set(work,
                                                                               sandbox.base))),
                          **plan.ledger_fields())
    (line,) = ledger.entries()
    assert {fix["source"] for fix in line["fixes"]} == {"rule"}
    assert line["tier1"]["status"] == OFF and line["tier1"]["scanned"] is False
    assert fix_counts(ledger.entries(), now=_at(line), window_days=90) == {
        "no-file-variant-proliferation": 1, "structured-logging": 2}


# --- the plan --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [OFF, NO_ELIGIBLE, ALREADY_SCANNED, ATTEMPTS_EXHAUSTED])
def test_a_plan_that_does_not_scan_gives_its_skipped_scan_s_ledger_fields(status):
    plan = Tier1Plan(status, "b" * 40)
    assert plan.ledger_fields() == plan.skipped().ledger_fields()
    assert plan.ledger_fields()["tier1"]["status"] == status


def test_a_ready_plan_gives_no_ledger_fields():
    """A READY plan whose sweep ended before step 2c never scanned, and its
    line must not say it did: no `tier1` field at all."""
    assert Tier1Plan(READY, "b" * 40).ledger_fields() == {}


def test_tier1_scanning_is_on_by_default_and_a_boolean():
    assert load_config({})["sweep"]["tier1_scan"] is True
    with pytest.raises(ConfigError, match=r"sweep\.tier1_scan"):
        load_config({"sweep": {"tier1_scan": "yes"}})


def test_eligible_means_tier_principle_and_no_rule_cites_it(tmp_path):
    """The fixture's three principles are all `tier: principle`; the two
    with a rule are not tier 1, whatever their tier says."""
    repo = committed_sample(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE)
    eligible = eligible_principles(fixture_principles(), load_rules(repo))
    assert [p.id for p in eligible] == [ECC]
    promoted = parse_principles(fixture_text(".curamoir/principles.md").replace(
        "- **tier:** principle\n- **promoted_on:** null\n- **source:** docs/curamoir-spec.md "
        "E2-R2 — \"errors carry context\"",
        "- **tier:** rule\n- **promoted_on:** 2026-10-01\n- **source:** docs/curamoir-spec.md "
        "E2-R2 — \"errors carry context\""))
    assert eligible_principles(promoted, load_rules(repo)) == ()


def test_no_eligible_principle_means_no_scan(tmp_path):
    """The E3 fixture as E3's criterion uses it: every principle has a
    rule, so tier 1 plans no scan and spends nothing."""
    repo = committed_sample(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        assert plan_scan(sandbox, []).status == NO_ELIGIBLE


def test_a_clean_base_scanned_once_is_no_work_and_not_scanned_again(tmp_path):
    """E3-R7 through the real recording path: the lint is clean, the scan
    finds nothing, so the decision after it is NO_WORK, and Decision.record
    writes the scan's `tier1` field on that line. The next sweep at the same
    base reads it back: ALREADY_SCANNED, so no scan, and NO_WORK without
    even listing open PRs."""
    repo = clean_lint_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply())
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        assert work.items == ()
        plan = plan_scan(sandbox, ledger.entries())
        assert plan.status == READY
        decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [],
                                  tier1=plan.will_scan)
        assert decision.kind == WORK
        decision.apply_to(sandbox)
        scan = run_tier1_scan(sandbox, plan, decision.held_paths, ledger.entries(),
                              claude=claude)
        assert scan.status == SCANNED and scan.findings == ()
        sandbox.work_list = with_findings(sandbox.work_list, scan)
        final = decision.after_tier1(sandbox.work_list, scan)
        assert final.kind == NO_WORK
        with JobRun(ledger, "sweep") as run:
            final.record(run, found_delta(None, found_set(work, sandbox.base)),
                         **scan.ledger_fields())
        base = sandbox.base

    (line,) = ledger.entries()
    assert line["work"] == NO_WORK and line["tier1"]["scanned"] is True
    assert line["tier1"]["cost_usd"] == 0.02
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as again:
        assert again.base == base
        work = lint_sandbox(again)
        plan = plan_scan(again, ledger.entries())
        assert plan.status == ALREADY_SCANNED and not plan.will_scan

        def must_not_list():
            raise AssertionError("a clean, already scanned base needs no PR listing")

        assert decide_under_default_prefix(work, again.base, ledger.entries(), must_not_list,
                              tier1=plan.will_scan).kind == NO_WORK


def test_a_failed_scan_with_no_lint_work_is_a_failure_never_no_work(tmp_path):
    """The lint is clean and the scan fails: nothing was shown clean, so
    the decision after it is TIER1_FAILED, which Decision.record writes as
    a failure line, never `no_work`; with lint work the sweep goes on."""
    repo = clean_lint_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", "I could not finish.")
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        plan = plan_scan(sandbox, ledger.entries())
        decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [],
                                  tier1=plan.will_scan)
        decision.apply_to(sandbox)
        scan = run_tier1_scan(sandbox, plan, decision.held_paths, ledger.entries(),
                              claude=claude)
        assert scan.status == FAILED
        sandbox.work_list = with_findings(sandbox.work_list, scan)
        final = decision.after_tier1(sandbox.work_list, scan)
        assert final.kind == TIER1_FAILED and final.terminal
        with JobRun(ledger, "sweep") as run:
            final.record(run, found_delta(None, found_set(work, sandbox.base)),
                         **scan.ledger_fields())
    (line,) = ledger.entries()
    assert line["outcome"] == "failure" and line["work"] == WORK
    assert line["tier1"]["status"] == FAILED and line["tier1"]["scanned"] is False
    assert "nothing was shown clean" in line["message"]


def test_a_failed_scan_with_lint_work_lets_the_sweep_go_on(swept):
    _repo, work = swept
    decision = decide_under_default_prefix(work, "a" * 40, [], lambda: [], tier1=True)
    failed = Tier1Scan(FAILED, "a" * 40, reason="the tier-1 scan exited 1")
    assert decision.after_tier1(decision.work_list, failed) is decision


def test_the_scan_runs_at_most_once_per_base(tmp_path):
    """E3-R7: a line that scanned this base and succeeded, or opened a PR,
    ends tier 1 for it; a failure without a PR, a failed scan, another
    job's line, or another base does not."""
    repo = tier1_repo(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        base = sandbox.base
        scanned = seed(SCANNED, base)
        pr = pr_field(7, "curamoir/sweep-2026-10-20", "https://example.invalid/pr/7", base,
                      [BILLING])
        for done in (scanned, {**seed(SCANNED, base, "failure"), "pr": pr}):
            assert plan_scan(sandbox, [done]).status == ALREADY_SCANNED
        for other in (seed(SCANNED, base, "failure"),
                      seed(FAILED, base),
                      {**scanned, "job": "review"},
                      seed(SCANNED, "0" * 40)):
            assert plan_scan(sandbox, [other]).status == READY
    assert already_scanned([scanned], base) and not already_scanned([scanned], "0" * 40)


def test_a_scan_whose_sweep_failed_without_a_pr_is_scanned_again(tmp_path):
    """The scan found something, the sweep then failed (red tests, a PR
    that would not open), and no PR exists: the next sweep at that base
    scans again rather than read "already scanned" and log `no_work` over
    findings nobody fixed."""
    repo = clean_lint_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply(FOUND_BILLING))
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        plan = plan_scan(sandbox, ledger.entries())
        decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [],
                                  tier1=plan.will_scan)
        decision.apply_to(sandbox)
        scan = run_tier1_scan(sandbox, plan, decision.held_paths, ledger.entries(),
                              claude=claude)
        assert len(scan.findings) == 1
        with JobRun(ledger, "sweep") as run:
            run.failed("the test gate abandoned every fix", **decision.ledger_fields(),
                       found_delta=found_delta(None, found_set(work, sandbox.base)),
                       **scan.ledger_fields())
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as again:
        work = lint_sandbox(again)
        plan = plan_scan(again, ledger.entries())
        assert plan.status == READY
        assert decide_under_default_prefix(work, again.base, ledger.entries(), lambda: [],
                              tier1=plan.will_scan).kind == WORK


def test_scan_attempts_per_base_are_capped(tmp_path):
    """MAX_SCAN_ATTEMPTS failed scans at one base, and the next sweep makes
    no model call: it runs on the lint alone, and with no lint work its
    `no_work` line says tier 1's attempts ran out, not that it found the
    base clean."""
    repo = clean_lint_repo(tmp_path)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    for attempt in range(MAX_SCAN_ATTEMPTS):
        claude, _ = fake_claude(tmp_path / f"a{attempt}", "scan", "no", exit_code=1)
        with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
            work = lint_sandbox(sandbox)
            plan = plan_scan(sandbox, ledger.entries())
            assert plan.status == READY
            decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [],
                                      tier1=True)
            decision.apply_to(sandbox)
            scan = run_tier1_scan(sandbox, plan, (), ledger.entries(), claude=claude)
            final = decision.after_tier1(with_findings(sandbox.work_list, scan), scan)
            with JobRun(ledger, "sweep") as run:
                final.record(run, found_delta(None, found_set(work, sandbox.base)),
                             **scan.ledger_fields())
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        plan = plan_scan(sandbox, ledger.entries())
        assert plan.status == ATTEMPTS_EXHAUSTED and not plan.will_scan
        with pytest.raises(ValueError):
            run_tier1_scan(sandbox, plan, (), ledger.entries(), claude="/nonexistent")
        decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [],
                                  tier1=plan.will_scan)
        assert decision.kind == NO_WORK
        with JobRun(ledger, "sweep") as run:
            decision.record(run, found_delta(None, found_set(work, sandbox.base)),
                            **plan.ledger_fields())
    line = ledger.entries()[-1]
    assert line["work"] == NO_WORK
    assert line["tier1"]["status"] == ATTEMPTS_EXHAUSTED and line["tier1"]["scanned"] is False


def scanned_then(tmp_path, repo, ledger, record, *, cap=None, reply=None, name="scan"):
    """One sweep at `repo`'s HEAD through the scan (a stand-in that reports
    FOUND_BILLING unless `reply` says otherwise); then `record(run, scan,
    decision, work, sandbox)` writes its line on a real JobRun. Returns the
    scan."""
    claude, _ = fake_claude(tmp_path / name, "scan",
                            findings_reply(FOUND_BILLING) if reply is None else reply)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        work = lint_sandbox(sandbox)
        plan = plan_scan(sandbox, ledger.entries())
        assert plan.status == READY
        decision = decide_under_default_prefix(work, sandbox.base, ledger.entries(), lambda: [], tier1=True)
        decision.apply_to(sandbox)
        scan = run_tier1_scan(sandbox, plan, decision.held_paths, ledger.entries(), cap=cap,
                              claude=claude)
        with JobRun(ledger, "sweep") as run:
            record(run, scan, decision, work, sandbox)
    return scan


def test_refusals_after_the_scan_count_as_attempts_and_record_its_cost(tmp_path):
    """The scan succeeds, then the fix step is refused (step 6: out of
    scope) twice at one base. Each refusal line carries the scan's field,
    so the attempts are counted and the spend is on record; the third plan
    makes no call."""
    from curamoir.guards.reasons import Reason
    from curamoir.guards.result import Refusal

    repo = clean_lint_repo(tmp_path)
    ledger = Ledger(tmp_path / "ledger.jsonl")

    def refused(run, scan, decision, work, sandbox):
        assert scan.status == SCANNED and scan.findings
        run.refused(Refusal(Reason.GUARD_ERROR, "the fix was out of scope"),
                    **scan.ledger_fields())

    for attempt in range(MAX_SCAN_ATTEMPTS):
        scanned_then(tmp_path, repo, ledger, refused, name=f"s{attempt}")
    lines = ledger.entries()
    assert [line["outcome"] for line in lines] == ["refused"] * MAX_SCAN_ATTEMPTS
    assert all(line["tier1"]["cost_usd"] == 0.02 for line in lines)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        assert plan_scan(sandbox, ledger.entries()).status == ATTEMPTS_EXHAUSTED


def test_the_rotation_moves_on_at_a_new_base_and_a_retry_rereads_its_slice(tmp_path):
    """A tier-1-only sweep fails without a PR at B1: a new base B2 moves
    past B1's slice (from B1's `cursor`, whatever B1's outcome), and a
    retry back at B1, even after B2's scan, rereads B1's slice (from the
    attempt's `cursor_from`), not B2's next one."""
    repo = clean_lint_repo(tmp_path)
    ledger = Ledger(tmp_path / "ledger.jsonl")

    def failed(run, scan, decision, work, sandbox):
        run.failed("the test gate abandoned every fix", **decision.ledger_fields(),
                   found_delta=found_delta(None, found_set(work, sandbox.base)),
                   **scan.ledger_fields())

    first = scanned_then(tmp_path, repo, ledger, failed, cap=2, reply=findings_reply(),
                         name="b1")
    assert len(first.candidates) == 2
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "B2"], cwd=repo, check=True)
    moved = scanned_then(tmp_path, repo, ledger, failed, cap=2, reply=findings_reply(),
                         name="b2")
    assert len(moved.candidates) == 2
    assert not set(moved.candidates) & set(first.candidates)
    assert min(moved.candidates) > first.cursor or first.cursor == max(first.candidates)
    # Back at B1 (a reverted push): the same-base retry starts where B1's
    # attempt started, not from B2's cursor.
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo, check=True)
    again = scanned_then(tmp_path, repo, ledger, failed, cap=2, reply=findings_reply(),
                         name="b1-again")
    assert again.candidates == first.candidates


def test_a_skipped_plan_line_does_not_shadow_the_attempt_before_it():
    """At base B, an attempt started from B0's cursor; a later line at B
    whose plan skipped the scan (attempts exhausted) has no origin of its
    own, so a retry at B still starts where the attempt did."""
    base, b0 = "b" * 40, "a" * 40
    other = seed(SCANNED, b0, "failure", cursor="src/z.py")
    attempt = seed(FAILED, base, "failure", changed_from=b0, cursor_from="src/m.py",
                   cursor="src/p.py")
    exhausted = seed(ATTEMPTS_EXHAUSTED, base)
    assert rotation_origin([other, attempt, exhausted], base) == (b0, "src/m.py")
    assert rotation_origin([other, exhausted], base) == (b0, "src/z.py")


def test_a_scan_that_changed_the_sandbox_still_gives_its_base_the_origin():
    """A SANDBOX_CHANGED scan made the model call, so it started somewhere:
    a retry at its base restarts from its `changed_from` and `cursor_from`,
    not from the cursor a scan at another base left."""
    base, b0 = "b" * 40, "a" * 40
    other = seed(SCANNED, b0, "failure", cursor="src/z.py")
    changed = seed(SANDBOX_CHANGED, base, "refused", changed_from=b0,
                   cursor_from="src/m.py", cursor="src/p.py")
    assert rotation_origin([other, changed], base) == (b0, "src/m.py")


def test_failed_refused_and_unfinished_scans_all_count_as_attempts(tmp_path):
    repo = tier1_repo(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        failed = seed(FAILED, sandbox.base, "failure")
        refused = seed(SANDBOX_CHANGED, sandbox.base, "refused")
        unfinished = seed(SCANNED, sandbox.base, "failure")
        for pair in ((failed, refused), (refused, unfinished), (unfinished, failed)):
            assert plan_scan(sandbox, list(pair)).status == ATTEMPTS_EXHAUSTED
        assert plan_scan(sandbox, [failed]).status == READY


# --- the scan --------------------------------------------------------------------------

def test_a_scan_that_changes_the_sandbox_is_refused(tmp_path):
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply(),
                            writes={BILLING: "x = 1\n"})
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        plan = plan_scan(sandbox, [])
        with pytest.raises(ScopeCheckError, match="reads only") as raised:
            run_tier1_scan(sandbox, plan, (), [], claude=claude)
    assert raised.value.cost_usd == 0.02
    fields = raised.value.ledger_fields()["tier1"]
    assert fields["status"] == SANDBOX_CHANGED and fields["cost_usd"] == 0.02
    # WHY not "refused": that word is the field's list of rejected findings,
    # and a status spelled the same would read as one of them.
    assert fields["status"] == "sandbox_changed" and fields["refused"] == []


def test_a_git_failure_checking_the_sandbox_after_the_call_is_refused_with_its_cost(
        tmp_path, monkeypatch):
    """Unverified is refused as changed, and the refusal carries the call's
    spend (a bare GitGuardError would carry none)."""
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply(FOUND_BILLING))

    def times_out(*_args):
        raise tier1.GitGuardError("git status did not finish within 60 seconds")

    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        monkeypatch.setattr(tier1, "tree_changes", times_out)
        with pytest.raises(tier1.ScanRefused, match="could not be checked") as raised:
            run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], claude=claude)
    scan = raised.value.scan
    assert scan.status == SANDBOX_CHANGED and scan.called
    assert scan.cost_usd == 0.02 and scan.usage == claude_payloads.USAGE


def test_a_git_failure_checking_the_reply_is_a_failed_scan_with_its_cost(tmp_path,
                                                                        monkeypatch):
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", findings_reply(FOUND_BILLING))

    def times_out(*_args):
        raise tier1.GitGuardError("git cat-file did not finish within 60 seconds")

    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        monkeypatch.setattr(tier1, "_base_text", times_out)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], claude=claude)
    assert scan.status == FAILED and scan.findings == () and scan.called
    assert "could not be checked against the base" in scan.reason
    assert scan.cost_usd == 0.02 and scan.usage == claude_payloads.USAGE


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), 10**400],
                         ids=["nan", "inf", "huge"])
def test_a_cost_that_is_not_a_finite_number_is_taken_as_none(tmp_path, cost):
    """A NaN or infinity would reach the ledger as a bare NaN or Infinity,
    which is not JSON; an int too large for a float cannot be priced."""
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", None)
    # The payload as printed text: json.dumps writes NaN and Infinity as the
    # bare tokens a JSON parser like Python's reads back (the shape of the
    # CLI output this guards against).
    printed = json.dumps(claude_payloads.success(findings_reply(), cost=cost))
    pathlib.Path(claude).write_text(f"#!{sys.executable}\nprint({printed!r})\n",
                                    encoding="utf-8")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], claude=claude)
    assert scan.status == SCANNED and scan.cost_usd is None
    json.loads(json.dumps(scan.ledger_fields(), allow_nan=False))


def test_a_scan_whose_claude_cannot_start_is_failed_but_not_called(tmp_path):
    repo = tier1_repo(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [],
                              claude=str(tmp_path / "no-such-claude"))
    assert scan.status == FAILED and not scan.started and not scan.called
    assert scan.reason.startswith("the tier-1 scan could not start")
    # Still an attempt: a claude that never starts is not retried forever.
    line = {"kind": "job", "job": "sweep", "outcome": "failure", **scan.ledger_fields()}
    assert tier1.scan_attempts([line], scan.base) == 1


@pytest.mark.parametrize("result, exit_code", [
    ("I could not finish.", 0),
    (findings_reply(FOUND_BILLING) + findings_reply(FOUND_HANDLERS), 0),
    (findings_reply(FOUND_BILLING), 1),
])
def test_a_scan_whose_reply_cannot_be_used_failed_and_found_nothing(tmp_path, result,
                                                                     exit_code):
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", result, exit_code=exit_code)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], claude=claude)
    assert scan.status == FAILED and scan.findings == () and not scan.scanned
    assert scan.reason and scan.ledger_fields()["tier1"]["reason"] == scan.reason


@pytest.mark.parametrize("payload", [
    claude_payloads.is_error(findings_reply(FOUND_BILLING)),
    claude_payloads.error_max_turns(),
], ids=["is_error", "error_max_turns"])
def test_a_scan_the_cli_reports_as_an_error_failed(tmp_path, payload):
    """Exit 0 is not enough: a payload with `is_error: true` (with a reply,
    or stopped at --max-turns with none) is a FAILED scan, its cost kept."""
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", None, payload=payload)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], claude=claude)
    assert scan.status == FAILED and scan.findings == ()
    assert scan.cost_usd == 0.02


def test_a_scan_with_no_payload_failed_with_no_usage_or_cost(tmp_path):
    """A scan that crashed and printed something that is not JSON has no
    payload, so it reports neither usage nor cost: both are None, not 0,
    and the scan is FAILED (a status in _CALLED, so the call ran). The
    runner then writes budget.UNRECORDED for the line's token counts
    (fix_scope, "The line's cost"), so the month's total says it is a
    lower bound rather than reading this call as $0."""
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", None)
    pathlib.Path(claude).write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('Segmentation fault: the CLI fell over')\n"
        "sys.exit(139)\n",
        encoding="utf-8")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], claude=claude)
    assert scan.status == FAILED and scan.status in tier1._CALLED
    assert scan.usage is None and scan.cost_usd is None


def test_a_scan_that_hangs_is_stopped_at_the_timeout_and_failed(tmp_path):
    """A stand-in that never answers is killed at the timeout: the scan is
    FAILED because it timed out, and as it printed nothing it reports
    neither usage nor cost. The stand-in writes its pid, so the test can
    check it did not outlive the scan (and kill it if it did)."""
    repo = tier1_repo(tmp_path)
    claude, _ = fake_claude(tmp_path, "scan", None)
    pid_file = tmp_path / "scan.pid"
    pathlib.Path(claude).write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8")
    try:
        with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
            lint_sandbox(sandbox)
            scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], timeout=1,
                                  claude=claude)
        assert scan.status == FAILED and scan.findings == ()
        assert scan.reason == "the tier-1 scan timed out"
        assert scan.usage is None and scan.cost_usd is None
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def test_the_payloads_usage_is_kept_on_the_scan(tmp_path):
    repo = tier1_repo(tmp_path)
    usage = {**claude_payloads.USAGE, "output_tokens": 999}
    claude, _ = fake_claude(tmp_path, "scan", None,
                            payload=claude_payloads.success(findings_reply(), usage=usage))
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], claude=claude)
    assert scan.status == SCANNED and scan.usage == usage


def test_the_scan_command_line_cannot_edit(tmp_path):
    argv = fix_step_argv("p", "m", tmp_path, tools=SCAN_TOOLS)
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed.startswith("Read(") and "Edit" not in allowed
    assert "--permission-mode" in argv and "--restricted" in argv


def test_the_scan_is_given_the_lint_paths_and_skips_held_files(tmp_path):
    repo = tier1_repo(tmp_path)
    claude, log = fake_claude(tmp_path, "scan", findings_reply())
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (HANDLERS,), [], cap=3,
                              claude=claude)
    assert scan.status == SCANNED and scan.findings == ()
    assert scan.candidates[:2] == (BILLING, VARIANT)
    assert len(scan.candidates) == 3 and HANDLERS not in scan.candidates
    prompt = json.loads(log.read_text())["argv"][1]
    assert f"- {BILLING}\n" in prompt and HANDLERS not in prompt


def test_parse_findings_takes_exactly_one_document():
    assert parse_findings(findings_reply(FOUND_BILLING)) == [FOUND_BILLING]
    crlf = findings_reply(FOUND_BILLING).replace("\n", "\r\n")
    assert parse_findings(crlf) == [FOUND_BILLING]
    document = json.dumps({"findings": [FOUND_BILLING]})
    for fence in ("```", "```JSON"):
        assert parse_findings(f"Found:\n{fence}\n{document}\n```\n") == [FOUND_BILLING]
    with pytest.raises(Tier1ReplyError):
        parse_findings(f"```\n{document}\n```\n```json\n{document}\n```\n")
    with pytest.raises(Tier1ReplyError):
        parse_findings(f"```json\n{document}\n```\n```json\n{document}\n```\n")
    # A four-backtick fence is not this module's fence: its tag reads as
    # "`json", so no block is found and the reply is not the document.
    # Pinned as refused (a FAILED scan), not silently parsed.
    with pytest.raises(Tier1ReplyError):
        parse_findings(f"````json\n{document}\n````\n")
    python_first = f"Context:\n```python\nx = 1\n```\nFindings:\n```json\n{document}\n```\n"
    assert parse_findings(python_first) == [FOUND_BILLING]


def test_a_deeply_nested_reply_is_a_reply_error_not_a_crash():
    deep = '{"findings": ' + "[" * 100000 + "]" * 100000 + "}"
    with pytest.raises(Tier1ReplyError):
        parse_findings(deep)


def test_a_quote_of_line_one_ignores_a_byte_order_mark():
    known = {p.id for p in fixture_principles().principles}
    raw = [{"principle_id": ECC, "path": "m.py", "line": 1, "end_line": 1,
            "quote": "raise ValueError('x')", "note": "n"}]
    accepted, refused = check_findings(
        raw, known=known, eligible={ECC}, candidates=["m.py"],
        base_text=lambda path: "\ufeffraise ValueError('x')\n", lint_items=())
    assert refused == () and len(accepted) == 1
    assert parse_findings('{"findings": []}') == []
    for bad in (None, "nothing", '{"findings": {}}', '{"findings": [], "extra": 1}',
                findings_reply() + findings_reply()):
        with pytest.raises(Tier1ReplyError):
            parse_findings(bad)


# --- checking what the scan reported ---------------------------------------------------

def test_a_finding_that_checks_out_is_accepted():
    accepted, refused = check_findings_on_fixture([FOUND_HANDLERS, FOUND_BILLING])
    assert refused == ()
    assert [f.as_dict() for f in accepted] == [
        {"id": "t1-01", **FOUND_BILLING, "quote": BILLING_RAISE_LINE},
        {"id": "t1-02", **FOUND_HANDLERS, "quote": HANDLERS_RAISE_LINE}]


@pytest.mark.parametrize("change, reason", [
    ({"principle_id": "no-such-principle"}, UNKNOWN_PRINCIPLE),
    ({"principle_id": "structured-logging"}, NOT_ELIGIBLE),
    ({"path": "src/sample/config.py"}, NOT_A_CANDIDATE),
    ({"line": 38, "end_line": 38}, BAD_SPAN),
    ({"line": 37, "end_line": 36}, BAD_SPAN),
    ({"line": 0}, BAD_SPAN),
    ({"line": 37 - MAX_TIER1_SPAN, "end_line": 37,
      "quote": fixture_text(BILLING).split("\n")[36 - MAX_TIER1_SPAN]}, SPAN_TOO_LONG),
    ({"quote": 'raise RuntimeError("could not load rates from path")'}, QUOTE_MISMATCH),
    ({"line": 36, "end_line": 37}, QUOTE_MISMATCH),
    ({"quote": ""}, QUOTE_MISMATCH),
    ({"line": True}, MALFORMED),
    ({"extra": 1}, MALFORMED),
    ({"note": None}, MALFORMED),
])
def test_a_finding_that_does_not_check_out_is_refused(change, reason):
    accepted, refused = check_findings_on_fixture([{**FOUND_BILLING, **change}])
    assert accepted == () and [r.reason for r in refused] == [reason]


def test_a_span_of_max_lines_is_allowed():
    lines = fixture_text(BILLING).split("\n")
    first = 37 - MAX_TIER1_SPAN + 1
    accepted, refused = check_findings_on_fixture([{**FOUND_BILLING, "line": first,
                                "quote": "  " + lines[first - 1] + "  "}])
    assert refused == () and accepted[0].span == (first, 37)


def test_a_finding_near_a_lint_violation_is_refused(tmp_path):
    """Within WINDOW + 1 of the lint's print in handlers.py (line 22), one
    insertion could count for the lint fix and the tier-1 fix; line 27 is
    the first line clear of it."""
    lint_items = work_list(committed_sample(tmp_path, NO_BARE_PRINT)).items
    lines = fixture_text(HANDLERS).split("\n")
    near = {**FOUND_HANDLERS, "line": 23, "end_line": 23, "quote": lines[22]}
    adjoining = {**FOUND_HANDLERS, "line": 26, "end_line": 26, "quote": lines[25]}
    clear = {**FOUND_HANDLERS, "line": 27, "end_line": 27, "quote": lines[26]}
    accepted, refused = check_findings_on_fixture([near, adjoining, clear, FOUND_HANDLERS],
                              lint_items=lint_items)
    assert [f.span for f in accepted] == [(27, 27), (31, 31)]
    assert [r.reason for r in refused] == [NEAR_LINT_VIOLATION, NEAR_LINT_VIOLATION]
    # WHY line 26 is refused: an insertion after line 25, the lint window's
    # last line, is the lint fix's to make and would touch 26's edge.
    assert touches(hunk(25, 0), 26, 26)


def test_a_finding_overlapping_an_accepted_one_is_refused():
    lines = fixture_text(BILLING).split("\n")
    wider = {**FOUND_BILLING, "line": 36, "end_line": 37, "quote": lines[35]}
    accepted, refused = check_findings_on_fixture([FOUND_BILLING, wider])
    assert [f.span for f in accepted] == [(36, 37)]
    assert [r.reason for r in refused] == [OVERLAPS_FINDING]


def test_adjoining_findings_are_refused_so_one_insertion_credits_one():
    """An insertion between two adjoining spans (36 and 37) touches both
    edges, so the second finding is refused and the insertion is one fix."""
    lines = fixture_text(BILLING).split("\n")
    above = {**FOUND_BILLING, "line": 36, "end_line": 36, "quote": lines[35]}
    assert touches(hunk(36, 0), 36, 36) and touches(hunk(36, 0), 37, 37)
    accepted, refused = check_findings_on_fixture([above, FOUND_BILLING])
    assert [f.span for f in accepted] == [(36, 36)]
    assert [r.reason for r in refused] == [OVERLAPS_FINDING]
    replies = parse_fix_replies("t1-01 fixed")
    assert len(tier1_fix_records(accepted, {BILLING: [hunk(36, 0)]}, kept=[BILLING],
                                 replies=replies, base="b" * 40)) == 1


def test_a_path_keeps_the_spelling_git_gave_it():
    """A candidate git names in NFD and a finding that names it in NFC are
    one file: the finding, and the base text read for it, use git's own
    spelling, the one `git show` and the PR know."""
    import unicodedata

    nfd = unicodedata.normalize("NFD", "src/café.py")
    text = "def f():\n    raise ValueError('x')\n"
    known = {p.id for p in fixture_principles().principles}
    raw = [{"principle_id": ECC, "path": unicodedata.normalize("NFC", nfd), "line": 2,
            "end_line": 2, "quote": "raise ValueError('x')", "note": "n"}]
    accepted, refused = check_findings(
        raw, known=known, eligible={ECC}, candidates=[nfd],
        base_text=lambda path: text if path == nfd else None, lint_items=())
    assert refused == () and [f.path for f in accepted] == [nfd]


def test_findings_past_the_cap_are_refused():
    known = {p.id for p in fixture_principles().principles}
    accepted, refused = check_findings(
        [FOUND_BILLING, FOUND_HANDLERS], known=known, eligible={ECC},
        candidates=(BILLING, HANDLERS), base_text=fixture_text, lint_items=(), cap=1)
    assert len(accepted) == 1 and [r.reason for r in refused] == [OVER_CAP]


# --- the candidates --------------------------------------------------------------------

def test_candidates_are_the_lint_paths_then_changed_files_then_a_rotation():
    universe = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    assert choose_candidates(["z.py"], universe, [], None, 3) == (
        ("z.py", "a.py", "b.py"), "b.py")
    assert choose_candidates(["z.py"], universe, [], "b.py", 3) == (
        ("z.py", "c.py", "d.py"), "d.py")
    assert choose_candidates(["z.py"], universe, [], "d.py", 4) == (
        ("z.py", "e.py", "a.py", "b.py"), "b.py")
    assert choose_candidates(["z.py"], universe, ["d.py", "gone.py"], "a.py", 3) == (
        ("z.py", "d.py", "b.py"), "b.py")
    assert choose_candidates(["z.py", "y.py"], universe, ["d.py"], "a.py", 2) == (
        ("y.py", "z.py"), "a.py")


def test_a_lint_path_the_scan_cannot_read_still_takes_its_room_in_the_pr(tmp_path):
    """README.md has lint work but is no language the scan reads; it is in
    the PR all the same, so with a cap of 2 there is room for one more."""
    repo = tier1_repo(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        sandbox.work_list = SimpleNamespace(paths=("README.md",))
        candidates, _cursor = tier1.scan_candidates(sandbox, (), [], 2)
    assert len(candidates) == 1 and "README.md" not in candidates


def test_the_lint_paths_are_capped_too_and_a_cap_of_zero_scans_nothing(tmp_path):
    assert choose_candidates(["a.py", "b.py", "c.py"], ["d.py"], [], None, 2) == (
        ("a.py", "b.py"), None)
    repo = tier1_repo(tmp_path)
    claude, log = fake_claude(tmp_path, "scan", findings_reply())
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, []), (), [], cap=0, claude=claude)
    assert scan.status == NO_CANDIDATES and not log.exists()


def test_files_changed_since_the_previous_scan_come_first(tmp_path):
    repo = tier1_repo(tmp_path)
    previous = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()
    (repo / "src" / "sample" / "zzz.py").write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "new"], cwd=repo, check=True)
    entries = [seed(SCANNED, previous)]
    claude, _ = fake_claude(tmp_path, "scan", findings_reply())
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        scan = run_tier1_scan(sandbox, plan_scan(sandbox, entries), (), entries, cap=4,
                              claude=claude)
    assert scan.candidates == (BILLING, HANDLERS, VARIANT, "src/sample/zzz.py")


def test_scan_candidates_works_out_the_origin_from_the_ledger_when_none_is_given(tmp_path):
    """Without `_origin`, scan_candidates finds the rotation's origin itself
    (rotation_origin), so it chooses what run_tier1_scan's explicit pair
    chooses; and the origin matters here, as with no origin at all the
    file changed since the previous scan is not chosen."""
    repo = tier1_repo(tmp_path)
    previous = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()
    (repo / "src" / "sample" / "zzz.py").write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "new"], cwd=repo, check=True)
    entries = [seed(SCANNED, previous)]
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        worked_out = tier1.scan_candidates(sandbox, (), entries, 4)
        given = tier1.scan_candidates(sandbox, (), entries, 4,
                                      _origin=rotation_origin(entries, sandbox.base))
        without = tier1.scan_candidates(sandbox, (), entries, 4, _origin=(None, None))
    assert worked_out == given
    assert "src/sample/zzz.py" in worked_out[0]
    assert worked_out != without


# --- the scope check -------------------------------------------------------------------

def test_a_tier1_span_admits_an_edit_inside_it_and_nothing_far_from_it():
    old = "".join(f"x{n} = {n}\n" for n in range(1, 21))
    inside = old.replace("x10 = 10", "x10 = 100")
    far = old.replace("x20 = 20", "x20 = 200")
    span = (finding(9, 10),)
    assert check_scope([FileChange("m.py", "modified", old, inside)], span).ok
    verdict = check_scope([FileChange("m.py", "modified", old, far)], span)
    assert verdict.kinds() == (OUTSIDE_VIOLATION,)


def test_a_tier1_span_never_licenses_a_signature_change():
    old = "def f(a):\n    return a\n"
    new = "def f(a, b=None):\n    return a\n"
    change = [FileChange("m.py", "modified", old, new)]
    rule_violation = SimpleNamespace(path="m.py", line=1, col=1, end_line=1, end_col=5,
                                     principle_id="p", rule_id="r")
    assert check_scope(change, [rule_violation], permitting={"p"}).ok
    verdict = check_scope(change, [finding(1, 1)], permitting={"p"})
    assert SIGNATURE_CHANGE in verdict.kinds()


# --- the credit ------------------------------------------------------------------------

def test_a_hunk_touches_a_span_by_its_own_lines_or_an_insertion_at_its_edges():
    assert touches(hunk(36, 2), 37, 37) and touches(hunk(37, 1), 37, 37)
    assert not touches(hunk(34, 2), 37, 37) and not touches(hunk(38, 1), 37, 37)
    assert touches(hunk(36, 0), 37, 37) and touches(hunk(37, 0), 37, 37)
    assert not touches(hunk(35, 0), 37, 37) and not touches(hunk(38, 0), 37, 37)


def test_a_tier1_fix_needs_the_pr_path_a_touching_hunk_and_the_agents_word():
    found = (finding(37, 37, BILLING, ECC),)
    base = "b" * 40
    credited = tier1_fix_records(found, {BILLING: [hunk(36, 2)]}, kept=[BILLING],
                                 replies=parse_fix_replies("t1-01 fixed"), base=base)
    assert credited == (FixRecord(ECC, None, TIER1, BILLING, None, "agent", base,
                                  span=(37, 37)),)
    for hunks, kept, replies in (
            ({BILLING: [hunk(36, 2)]}, [], parse_fix_replies("t1-01 fixed")),
            ({BILLING: [hunk(20, 2)]}, [BILLING], parse_fix_replies("t1-01 fixed")),
            ({BILLING: [hunk(36, 2)]}, [BILLING], parse_fix_replies("t1-01 skipped")),
            ({BILLING: [hunk(36, 2)]}, [BILLING], {})):
        assert tier1_fix_records(found, hunks, kept=kept, replies=replies, base=base) == ()


def test_the_fix_reply_credits_only_a_plain_answer_right_after_the_id():
    """A negated or hedged answer is no credit: the answer word must follow
    the id with nothing but separators between them, on a line of its own
    (a list marker allowed). Ids compare by number, so `t1-5` is t1-05."""
    text = ("t1-01 not fixed: it was a false positive\n"
            "t1-02: could not be fixed, skipped\n"
            "- t1-03: fixed\n"
            "`t1-04` fixed\n"
            "t1-5 fixed\n"
            "abc123 fixed, and t1-06 fixed\n"
            "t1-07 fixed\nt1-07 skipped\n"
            "1. **t1-08**: Skipped - needs a redesign\n"
            "xt1-09 fixed\n"
            "| t1-10 | fixed |\n"
            "t1-11 — fixed\n"
            "t1-12 \u2013 fixed\n"  # an en dash
            "t1-13 → fixed\n"
            "| t1-14 | not fixed |\n"
            "t1-15 → could not be fixed\n"
            "t1-16 — not fixed\n")
    assert parse_fix_replies(text) == {3: "fixed", 4: "fixed", 5: "fixed", 8: "skipped",
                                       10: "fixed", 11: "fixed", 12: "fixed", 13: "fixed"}
    assert parse_fix_replies(None) == {}
    huge = "t1-" + "9" * 5000 + " fixed"
    assert parse_fix_replies(huge) == {} and parse_fix_replies("t1-0001 fixed") == {}
    assert tier1_fix_records((finding(37, 37, BILLING, ECC),), {BILLING: [hunk(36, 2)]},
                             kept=[BILLING], replies=parse_fix_replies(huge),
                             base="b" * 40) == ()
    found = (finding(37, 37, BILLING, ECC),)
    for reply, credited in (("t1-1 fixed", 1), ("t1-01 not fixed", 0),
                            ("t1-01: could not be fixed, skipped", 0)):
        assert len(tier1_fix_records(found, {BILLING: [hunk(36, 2)]}, kept=[BILLING],
                                     replies=parse_fix_replies(reply),
                                     base="b" * 40)) == credited


# --- the work list and the PR ----------------------------------------------------------

def test_the_work_list_counts_tier1_findings_as_work(swept):
    _repo, work = swept
    empty = work.__class__(items=(), report=work.report)
    assert empty.outcome == NO_WORK
    with_one = with_findings(empty, SimpleNamespace(findings=(finding(3, 4, "x.py", ECC),)))
    assert with_one.outcome == WORK and with_one.paths == ("x.py",)
    assert with_one.principle_ids == (ECC,)
    assert work_list_document(with_one)["tier1"] == [finding(3, 4, "x.py", ECC).as_dict()]


def test_rule_fixes_take_precedence_when_the_pr_is_trimmed(swept):
    _repo, work = swept
    lint_paths = sorted({item.path for item in work.items})
    paths = ["a_tier1.py", *lint_paths]
    assert trim_pr_paths(paths, work, len(lint_paths)) == tuple(lint_paths)
    assert trim_pr_paths(paths, work, 99) == tuple(sorted(paths))


def test_trimming_matches_the_lint_paths_in_nfc():
    """The verdict's path is the work tree's spelling, which macOS can hand
    back decomposed (NFD); the lint's is git's (NFC). One file, so it is
    the lint's path, and goes first, whichever form each side uses."""
    nfc_name, nfd_name = "caf\u00e9.py", "cafe\u0301.py"
    work = SimpleNamespace(items=(SimpleNamespace(path=nfc_name),))
    assert trim_pr_paths(["a_tier1.py", nfd_name], work, 1) == (nfd_name,)


def test_trimming_matches_a_decomposed_lint_path_to_the_verdicts_composed_one():
    """The mirror of the test above: here the lint's path is the NFD
    spelling and the verdict's is NFC. Still one file, so it is the lint's
    path and goes first, kept in the verdict's own spelling."""
    nfc_name, nfd_name = "caf\u00e9.py", "cafe\u0301.py"
    work = SimpleNamespace(items=(SimpleNamespace(path=nfd_name),))
    assert trim_pr_paths(["a_tier1.py", nfc_name], work, 1) == (nfc_name,)


@pytest.mark.parametrize("paths, expected", [
    (["a.py", "a.py", "b.py"], ("a.py", "b.py")),
    (["caf\u00e9.py", "cafe\u0301.py", "d.py"], ("caf\u00e9.py", "d.py")),
], ids=["duplicate", "nfc_and_nfd"])
def test_trimming_spends_one_cap_slot_per_file(paths, expected):
    """A path the verdict lists twice, or in both its NFC and its NFD
    spelling, is one file, so it takes one of the `cap` slots, not two:
    otherwise the second copy pushes a real file out of the PR. The first
    spelling in `paths` is the one kept."""
    work = SimpleNamespace(items=())
    assert trim_pr_paths(paths, work, 2) == expected
    assert trim_pr_paths(paths, work, 99) == expected


def test_the_decision_waits_for_the_scan_when_the_lint_is_clean(swept):
    """E3-R7: with tier 1 to run, an empty lint list is not yet `no_work`;
    after a scan that found nothing it is. A PR from this base ends the
    sweep before the scan is paid for."""
    _repo, work = swept
    empty = work.__class__(items=(), report=work.report)
    base = "a" * 40
    listed = []

    def open_prs():
        listed.append(True)
        return []

    assert decide_under_default_prefix(empty, base, [], open_prs).kind == NO_WORK and not listed
    decision = decide_under_default_prefix(empty, base, [], open_prs, tier1=True)
    assert decision.kind == WORK and listed
    scanned = Tier1Scan(SCANNED, base)
    assert decision.after_tier1(empty, scanned).kind == NO_WORK
    found = with_findings(empty, SimpleNamespace(findings=(finding(1, 1),)))
    assert decision.after_tier1(found, scanned) is decision
    same_base = [{"branch": "curamoir/sweep-2026-10-12", "number": 7, "url": "u",
                  "base": base, "paths": ["x.py"], "cross_repository": False}]
    assert decide_under_default_prefix(empty, base, [], lambda: same_base, tier1=True).kind == \
        DUPLICATE_OPEN_PR
    held = decide_under_default_prefix(work, base, [], lambda: [{**same_base[0], "base": "c" * 40,
                                                    "paths": list(work.paths)}], tier1=True)
    assert held.kind == WORK and held.held_paths == tuple(sorted(work.paths))
    assert held.after_tier1(held.work_list, scanned).kind == DUPLICATE_OPEN_PR
    # A failed scan wins over held lint work: the line is a failure, since
    # nothing outside the open PR's files was shown clean.
    failed = Tier1Scan(FAILED, base, reason="the tier-1 scan exited 1")
    assert held.after_tier1(held.work_list, failed).kind == TIER1_FAILED


def test_a_tier1_finding_in_a_remedied_file_is_not_the_agents(swept):
    from curamoir.remedies import RemedyResult

    _repo, work = swept
    found = with_findings(work, SimpleNamespace(
        findings=(finding(1, 1, VARIANT, ECC), finding(37, 37, BILLING, ECC))))
    remedies = RemedyResult(deleted=(VARIANT,), skipped=(), deletable_rules=frozenset())
    assert [f.path for f in remedies.for_agent(found).tier1] == [BILLING]
    import unicodedata

    nfd = unicodedata.normalize("NFD", "src/café.py")
    spelled = with_findings(work, SimpleNamespace(
        findings=(finding(1, 1, unicodedata.normalize("NFC", nfd), ECC),)))
    gone = RemedyResult(deleted=(nfd,), skipped=(), deletable_rules=frozenset())
    assert gone.for_agent(spelled).tier1 == ()


# --- sweep.exclude (cm-t3a) ---------------------------------------------------------------

EXCLUDING_WEB = ('version: 1\nsweep:\n  test_command: "true"\n'
                 '  exclude: ["src/sample/web/**"]\n')


def test_excluded_paths_are_never_scan_candidates(tmp_path):
    """The first dogfood sweep spent 9 of its 20 candidates on files whose
    violations are planted for tests (fixtures/). `sweep.exclude` keeps a
    path out of what the scan reads, and so out of what it can find."""
    repo = tier1_repo(tmp_path, config=EXCLUDING_WEB)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        sandbox.work_list = SimpleNamespace(paths=("src/sample/web/handlers.py",))
        candidates, _cursor = tier1.scan_candidates(sandbox, (), [], 50)

    assert candidates, "nothing left to scan"
    assert not [path for path in candidates if path.startswith("src/sample/web/")]


def test_without_an_exclude_the_same_paths_are_candidates(tmp_path):
    repo = tier1_repo(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        sandbox.work_list = SimpleNamespace(paths=())
        candidates, _cursor = tier1.scan_candidates(sandbox, (), [], 50)

    assert [path for path in candidates if path.startswith("src/sample/web/")]
