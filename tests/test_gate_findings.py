"""gate/findings.py (cm-8ap.13): the shared Finding contract and the merge.

The parser's long-standing rules (schema, fingerprints, closures, the open
set, the prior file) are tested through the bridge in test_r5_supervisor.py,
which now runs on this module. This file tests what bead 1 adds
(docs/r5-gate-v2-curamoir-spec.md §5 R5-R11 to R5-R16, §10 bead 1, §11):

- the three parser fixes of spec v1.10 (line ends at LF only, ``~`` in a line
  reference, a re-raise beside an empty closure);
- ``prose_unfiled`` and the ``Not filed`` heading;
- clarity advisory (R5-R12);
- the corpus path: a replay entry's ``recorded.json`` round-trips losslessly,
  with a null path or range allowed there and nowhere else;
- the merge: dedupe, grouping, the severity policy, priority order, the
  deferred list, and its reproducibility from raw output;
- the bridge: the same objects, and its command line under Python 3.9.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from curamoir.gate import findings as gf

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / ".claude" / "hooks" / "lib" / "r5_findings.py"
CORPUS = ROOT / "fixtures" / "r5-replay"


def finding(lens="correctness", severity="IMPORTANT", path="src/m.py", start=10, end=12,
            claim="the guard never fires", level="script", **extra):
    return {"lens": lens, "severity": severity, "path": path, "start_line": start,
            "end_line": end, "claim": claim, "evidence": "the lines", "fix": "change it",
            "verification_level": level, **extra}


def report(records, prose="Review.\n"):
    return prose + gf.PREFIX + " " + json.dumps(records, ensure_ascii=False) + "\n"


def parse(records, lens="correctness", prose="Review.\n", **kw):
    return gf.parse_report(report(records, prose), lens, **kw)


def prior(*records, lens="correctness"):
    return parse(list(records), lens)["open_set"]


def closure(target, evidence="tests/test_m.py:4 now asserts it", lens="correctness"):
    return finding(lens=lens, claim="the guard fires now", resolves=target, evidence=evidence)


# ---- spec v1.10 fix 2: "~" in a line reference

@pytest.mark.parametrize("claim", ["off by one at line ~42", "off by one at lines ~4-~9",
                                   "off by one at line ~4 \u2013 ~9", "off by one at line 42"])
def test_a_line_reference_with_a_tilde_normalises_as_one_without(claim):
    assert gf.normalise_claim(claim) == "off by one at"


def test_a_tilde_elsewhere_stays():
    assert gf.normalise_claim("expands ~ to the home directory") == "expands ~ to the home directory"


# ---- spec v1.10 fix 1: an output line ends at LF only

@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_unicode_separator_in_a_claim_is_content(separator):
    claim = f"the hook splits{separator}on this"
    out = parse([finding(claim=claim)])
    assert [f["claim"] for f in out["findings"]] == [claim]


def test_a_cr_before_lf_is_tolerated():
    text = report([finding()]).replace("\n", "\r\n")
    assert len(gf.parse_report(text, "correctness")["findings"]) == 1


def test_a_separator_after_the_contract_line_is_not_a_line_end():
    # The array line is the last line even with a separator after it; that
    # separator is content of that line, which is then not valid JSON.
    with pytest.raises(gf.ReportError, match="not JSON"):
        gf.parse_report(gf.PREFIX + " []\u2028\n", "correctness")


def test_the_command_line_reads_a_lone_cr_as_content(tmp_path, capsys):
    # Text mode with universal newlines would end the line at the CR, leaving
    # "]" as the last line; the file is read with newline="".
    path = tmp_path / "r.md"
    path.write_bytes(b"Review.\nR5-FINDINGS: [\r]\n")
    assert gf.main(["parse", "correctness", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []


# ---- spec v1.10 fix 3: a re-raise beside an empty closure

def test_a_re_raise_beside_an_empty_closure_keeps_the_re_raise_and_is_not_unavailable():
    earlier = prior(finding())
    fp = earlier[0]["fingerprint"]
    out = parse([closure(fp, evidence=""), finding()], prior_open=earlier)
    assert [f["fingerprint"] for f in out["open_set"]] == [fp]
    assert out["closures"] == [] and out["unaccounted"] == []
    assert [r["reason"] for r in out["closure_rejected"]] == ["a closure needs evidence (the resolving lines)"]


def test_closing_every_prior_with_empty_evidence_and_re_raising_none_is_still_unavailable():
    earlier = prior(finding(), finding(claim="a second defect"))
    empties = [closure(f["fingerprint"], evidence="") for f in earlier]
    with pytest.raises(gf.ReportError, match="every prior finding is closed with empty evidence"):
        parse(empties, prior_open=earlier)


def test_a_re_raise_beside_a_closure_with_evidence_rejects_the_closure():
    # Section 11: "a report that re-raises a finding and also closes it has
    # that closure rejected and the re-raise kept".
    earlier = prior(finding())
    fp = earlier[0]["fingerprint"]
    out = parse([closure(fp), finding()], prior_open=earlier)
    assert [f["fingerprint"] for f in out["open_set"]] == [fp] and out["closures"] == []
    assert out["closure_rejected"] == [{"resolves": fp, "fingerprint": out["findings"][0]["fingerprint"],
                                        "reason": "the same report re-raises the finding it closes"}]


# ---- R5-R12: clarity and excellence are advisory, with nothing to prove

@pytest.mark.parametrize(("lens", "unproven"), [("correctness", 1), ("edge_cases", 1),
                                                ("provenance", 1), ("clarity", 0), ("excellence", 0)])
def test_only_a_blocking_lens_has_unproven_findings(lens, unproven):
    out = parse([finding(lens=lens, level="cited")], lens=lens)
    assert len(out["unproven"]) == unproven


# ---- prose_unfiled (§11 "Prose and entries"); the prose shape is the lens
#      templates' Output Format: bold-led items under severity headings.

PROSE = """### Correctness Issues

#### Critical
None.

#### Important
1. **The stub is pinned instead of failing**
   - File: tests/golden/test_golden_legacy_cli.py:157
   - What's wrong: except Exception catches AssertionError

#### Minor
- **Docstring names one stub only**

```
#### Important
1. **not an item: inside a fence**
```
"""


def test_an_issue_in_the_prose_with_no_entry_is_prose_unfiled():
    out = parse([finding(severity="MINOR", claim="docstring names one stub")], prose=PROSE)
    assert out["prose_unfiled"] == [{"heading": "IMPORTANT", "text": "The stub is pinned instead of failing"}]
    assert out["counts"]["important"] == 0  # telemetry: it never blocks or fails


def test_every_prose_issue_filed_is_not_prose_unfiled():
    out = parse([finding(), finding(claim="docstring", severity="MINOR")], prose=PROSE)
    assert out["prose_unfiled"] == []


def test_a_closure_does_not_file_a_prose_issue():
    earlier = prior(finding())
    out = parse([closure(earlier[0]["fingerprint"]), finding(claim="docstring", severity="MINOR")],
                prose=PROSE, prior_open=earlier)
    assert [u["heading"] for u in out["prose_unfiled"]] == ["IMPORTANT"]


def test_none_and_sub_bullets_are_not_issues():
    prose = "#### Critical\nNone.\n\n#### Important\nNone.\n   - **indented, a sub-bullet**\n"
    assert parse([], prose=prose)["prose_unfiled"] == []


def test_a_plain_bullet_under_a_heading_is_an_issue():
    prose = "#### Minor\n- tests/golden/x.py:277 results matched by position\n"
    assert parse([], prose=prose)["prose_unfiled"] == [
        {"heading": "MINOR", "text": "tests/golden/x.py:277 results matched by position"}]


def test_a_list_after_none_or_a_prose_line_is_not_issues():
    prose = ("#### Minor\nNone.\n\nChecks performed:\n- patch targets match\n\n"
             "#### Important\n- **a real one**\n\nVerified with no finding:\n- each cell\n")
    assert parse([], prose=prose)["prose_unfiled"] == [{"heading": "IMPORTANT", "text": "a real one"}]


def test_with_fewer_entries_the_least_matching_item_is_unfiled():
    prose = ("#### Important\n1. **The stub is pinned instead of failing**\n"
             "2. **Recording rules are sorted in the CLI cells**\n")
    out = parse([finding(claim="Env.files sorts recording rules in the CLI cells")], prose=prose)
    assert out["prose_unfiled"] == [{"heading": "IMPORTANT", "text": "The stub is pinned instead of failing"}]


def test_a_not_filed_line_needs_its_reason():
    prose = ("#### Not filed\n"
             "- The old cells sort too; pre_existing\n"
             "- `id` masking in dashboards is documented \u2014 not_an_issue\n"
             "- Panel ids are masked globally (out_of_scope)\n"
             "- The loader walks up from the cwd\n")
    out = parse([], prose=prose)
    assert out["prose_unfiled"] == [{"heading": "Not filed", "text": "The loader walks up from the cwd"}]
    assert out["counts"] == {"critical": 0, "important": 0, "minor": 0}


def test_a_reasoned_not_filed_line_is_never_unfiled():
    assert parse([], prose="### Not filed\n- **Bold remark** about scope; out_of_scope\n")["prose_unfiled"] == []


def test_a_crlf_report_s_prose_is_read_like_an_lf_one():
    lf = parse([finding(severity="MINOR", claim="docstring")], prose=PROSE)
    crlf = gf.parse_report(report([finding(severity="MINOR", claim="docstring")], PROSE).replace("\n", "\r\n"),
                           "correctness")
    assert crlf["prose_unfiled"] == lf["prose_unfiled"] != []


# ---- the corpus path: recorded.json round-trips losslessly (§10 bead 1)

def _recorded():
    return sorted(CORPUS.glob("*/recorded.json"))


def _corpus_findings():
    for path in _recorded():
        for review in json.loads(path.read_text())["rounds"]:
            yield from review["findings"] or ()


def test_the_corpus_is_there_to_round_trip():
    # Not a silent pass over an empty glob: the delivered corpus-v1 counts.
    found = list(_corpus_findings())
    assert len(_recorded()) >= 134 and len(found) >= 84
    assert any(f["path"] is None for f in found)
    assert any(f["path"] is not None and f["start_line"] is None for f in found)


@pytest.mark.parametrize("path", _recorded(), ids=lambda p: p.parent.name)
def test_every_recorded_json_round_trips_losslessly(path):
    text = path.read_text()
    recorded = json.loads(text)
    loaded = gf.load_recorded(recorded)
    assert loaded == recorded and loaded is not recorded
    # Key order and every null kept: the same JSON, byte for byte.
    assert json.dumps(loaded, indent=1, ensure_ascii=False) == json.dumps(recorded, indent=1, ensure_ascii=False)


def test_a_corpus_fingerprint_is_kept_as_stored_and_compared_recomputed():
    stale = [f for f in _corpus_findings() if gf.key(f) != f["fingerprint"]]
    assert stale, "corpus-v1 holds a finding fingerprinted by an older normaliser"
    assert gf.corpus_finding(stale[0])["fingerprint"] == stale[0]["fingerprint"]


def test_a_corpus_finding_with_no_path_is_keyed_with_an_empty_one():
    # As NthLayer's corpus writer computed it: every null-path finding's
    # stored fingerprint is sha256(lens||normalised claim).
    no_path = [f for f in _corpus_findings() if f["path"] is None]
    assert no_path and all(gf.key(f) == f["fingerprint"] for f in no_path)
    f = no_path[0]
    expect = hashlib.sha256(f"{f['lens']}||{gf.normalise_claim(f['claim'])}".encode()).hexdigest()[:16]
    assert gf.key(f) == expect


def _corpus_record(**change):
    base = {"lens": "provenance", "severity": "CRITICAL", "path": None, "start_line": None,
            "end_line": None, "claim": "no test covers the branch", "evidence": None, "fix": None,
            "fingerprint": "0123456789abcdef", "verification_level": "asserted", "resolves": None,
            "regression": False, "status": None, "derived_from": "reviewer_transcript"}
    return {**base, **change}


def test_a_null_path_and_range_parse_in_corpus_mode_but_not_from_a_reviewer():
    record = _corpus_record()
    assert gf.corpus_finding(record) == record
    with pytest.raises(gf.ReportError, match="path must be a non-empty string"):
        parse([{**record, "evidence": "e", "lens": "provenance"}], lens="provenance")
    with pytest.raises(gf.ReportError, match="start_line and end_line"):
        parse([{**finding(), "start_line": None, "end_line": None}])


@pytest.mark.parametrize(("change", "reason"), [
    ({"lens": "edge-cases"}, "lens must be one of"),
    ({"severity": "HIGH"}, "severity"),
    ({"start_line": 3}, "both be null"),
    ({"start_line": 9, "end_line": 3}, "both be null"),
    ({"path": ""}, "path must be a non-empty string"),
    ({"claim": None}, "claim"),
    ({"evidence": 3}, "evidence must be a string or null"),
    ({"fingerprint": "XYZ"}, "fingerprint must be 16 hex"),
    ({"status": "done"}, "status must be one of"),
    ({"verification_level": "proven"}, "verification_level"),
    ({"resolves": "nope"}, "resolves"),
    ({"regression": "no"}, "regression"),
], ids=["pass-name-lens", "severity", "half-range", "inverted-range", "empty-path", "no-claim",
        "evidence-type", "fingerprint", "status", "level", "resolves", "regression"])
def test_a_corpus_finding_is_still_checked_against_the_schema(change, reason):
    with pytest.raises(gf.ReportError, match=reason):
        gf.corpus_finding(_corpus_record(**change))


def test_a_malformed_recorded_json_is_refused():
    for bad, reason in [([], "JSON object"), ({"rounds": {}}, "rounds must be a list"),
                        ({"rounds": [1]}, "each round"), ({"rounds": [{"findings": {}}]}, "list or null"),
                        ({"rounds": [], "cross_repo_findings": [_corpus_record(severity="LOW")]}, "severity")]:
        with pytest.raises(gf.ReportError, match=reason):
            gf.load_recorded(bad)


# ---- the merge (R5-R3, R5-R12 to R5-R16)

def opened(*records):
    """Open sets per lens, as parse_report leaves them after a first review."""
    by_lens = {}
    for r in records:
        by_lens.setdefault(r["lens"], []).append(r)
    return {lens: prior(*rs, lens=lens) for lens, rs in by_lens.items()}


def fp(record):
    return gf.fingerprint(record["lens"], record["path"], record["claim"])


def test_one_claim_on_overlapping_lines_from_two_lenses_is_one_entry_attributed_to_both():
    c = finding(lens="correctness", severity="IMPORTANT", start=10, end=14)
    e = finding(lens="edge_cases", severity="CRITICAL", start=13, end=20, claim="The  GUARD never fires ")
    m = gf.merge_round(opened(c, e))
    # The most severe record represents it; both fingerprints are listed.
    assert m["fix_list"] == [fp(e)] and m["deduped"] == [[fp(e), fp(c)]]
    assert m["entries"][fp(e)]["lenses"] == ["correctness", "edge_cases"]
    assert m["deferred"] == [] and m["grouped"] == []


def test_one_claim_on_separate_lines_is_two_entries():
    c = finding(lens="correctness", start=10, end=12)
    e = finding(lens="edge_cases", start=30, end=31)
    m = gf.merge_round(opened(c, e))
    assert m["fix_list"] == [fp(c), fp(e)] and m["deduped"] == [] and m["grouped"] == []


def test_different_claims_on_overlapping_lines_stay_separate_but_grouped():
    c = finding(lens="correctness", start=10, end=14)
    p = finding(lens="provenance", start=14, end=15, claim="no test kills a mutant of the guard")
    other = finding(lens="provenance", path="src/other.py", start=14, end=15, claim="another file")
    m = gf.merge_round(opened(c, p, other))
    assert m["fix_list"] == [fp(c), fp(p), fp(other)]
    assert m["grouped"] == [[fp(c), fp(p)]] and m["deduped"] == []


def test_the_fix_list_is_ordered_by_severity_then_lens():
    records = [finding(lens="provenance", severity="IMPORTANT", claim="p important"),
               finding(lens="edge_cases", severity="IMPORTANT", claim="e important", path="src/e.py"),
               finding(lens="correctness", severity="IMPORTANT", claim="c important", path="src/z.py"),
               finding(lens="provenance", severity="CRITICAL", claim="p critical", path="src/p.py")]
    m = gf.merge_round(opened(*records))
    assert m["fix_list"] == [fp(records[3]), fp(records[2]), fp(records[1]), fp(records[0])]


def test_minor_findings_and_excellence_are_deferred():
    minor = finding(lens="correctness", severity="MINOR", claim="a minor one")
    blocking = finding(lens="correctness", severity="IMPORTANT", path="src/b.py")
    excellence = finding(lens="excellence", severity="CRITICAL", path="src/x.py", claim="excellence says")
    m = gf.merge_round(opened(minor, blocking, excellence))
    assert m["fix_list"] == [fp(blocking)]
    assert m["deferred"] == [fp(excellence), fp(minor)]


def test_clarity_joins_the_fix_list_last_only_when_a_blocking_lens_needs_a_fix_round():
    clarity = finding(lens="clarity", severity="CRITICAL", path="src/c.py", claim="unreadable")
    blocking = finding(lens="provenance", severity="IMPORTANT", path="src/p.py")
    with_fix = gf.merge_round(opened(clarity, blocking))
    assert with_fix["fix_list"] == [fp(blocking), fp(clarity)] and with_fix["deferred"] == []
    alone = gf.merge_round(opened(clarity, finding(lens="correctness", severity="MINOR")))
    assert alone["fix_list"] == [] and fp(clarity) in alone["deferred"]


def test_a_clarity_claim_a_blocking_lens_also_raised_blocks():
    clarity = finding(lens="clarity", severity="CRITICAL")
    correctness = finding(lens="correctness", severity="IMPORTANT")
    m = gf.merge_round(opened(clarity, correctness))
    assert m["fix_list"] == [fp(clarity)] and m["entries"][fp(clarity)]["blocking"]
    assert m["entries"][fp(clarity)]["lenses"] == ["correctness", "clarity"]


def test_a_corpus_finding_with_no_location_is_never_deduped():
    a = {**_corpus_record(lens="correctness", severity="IMPORTANT", claim="same words")}
    b = {**_corpus_record(lens="edge_cases", severity="IMPORTANT", claim="same words")}
    m = gf.merge_round({"correctness": [a], "edge-cases": [b]})
    assert len(m["fix_list"]) == 2 and m["deduped"] == [] and m["grouped"] == []


def test_the_merge_does_not_depend_on_the_order_of_lenses_or_findings():
    records = [finding(lens="correctness", start=10, end=14),
               finding(lens="edge_cases", start=12, end=13, severity="CRITICAL"),
               finding(lens="provenance", start=13, end=13, claim="mutant survives"),
               finding(lens="clarity", severity="IMPORTANT", path="src/c.py", claim="naming"),
               finding(lens="correctness", severity="MINOR", path="src/d.py", claim="minor one")]
    sets = opened(*records)
    first = gf.merge_round(sets)
    for order in itertools.permutations(sets):
        assert gf.merge_round({k: list(reversed(sets[k])) for k in order}) == first


def test_the_merge_refuses_a_closure_or_a_finding_in_another_lens_s_set():
    with pytest.raises(ValueError, match="no closures"):
        gf.merge_round({"correctness": [{**finding(), "resolves": "0123456789abcdef"}]})
    with pytest.raises(ValueError, match="open set"):
        gf.merge_round({"correctness": [finding(lens="provenance")]})
    with pytest.raises(ValueError, match="unknown lens"):
        gf.merge_round({"structure": []})


def test_the_merge_is_reproducible_from_the_raw_output_alone():
    """R5-R16: a recorded round's raw output merges again to the same lists."""
    raw = {
        "correctness": report([finding(lens="correctness", start=10, end=14)], PROSE),
        "edge-cases": report([finding(lens="edge_cases", start=12, end=13, severity="CRITICAL"),
                              finding(lens="edge_cases", severity="MINOR", path="src/e.py", claim="tiny")]),
        "provenance": report([finding(lens="provenance", start=13, end=13, claim="mutant survives")]),
        "clarity": report([finding(lens="clarity", severity="IMPORTANT", path="src/c.py", claim="naming")]),
    }
    done = gf.merge_from_raw(raw)
    # As the round record keeps it: raw output and merge decisions, as JSON.
    stored = json.loads(json.dumps({"raw": raw, "merge": done["merge"]}))
    again = gf.merge_from_raw(stored["raw"])
    assert json.loads(json.dumps(again["merge"])) == stored["merge"]
    assert again["merge"]["fix_list"] and again["merge"]["deferred"] and again["merge"]["deduped"]
    # And the merge is the merge of the parsed open sets.
    assert done["merge"] == gf.merge_round({k: v["open_set"] for k, v in done["lenses"].items()})


def test_merge_from_raw_hands_each_lens_its_prior_open_set():
    earlier = prior(finding(lens="correctness"))
    raw = {"correctness": report([closure(earlier[0]["fingerprint"])])}
    done = gf.merge_from_raw(raw, priors={"correctness": earlier})
    assert done["lenses"]["correctness"]["closures"] == [earlier[0]["fingerprint"]]
    assert done["merge"]["fix_list"] == []


# ---- the bridge imports this module

def _load_bridge():
    import importlib.util
    spec = importlib.util.spec_from_file_location("r5_findings_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_bridge_holds_this_module_s_objects():
    bridge = _load_bridge()
    assert bridge._impl is gf
    for name in ("parse_report", "fingerprint", "normalise_claim", "main", "ReportError", "merge_round"):
        assert getattr(bridge, name) is getattr(gf, name)


@pytest.mark.parametrize("python", [sys.executable, "python3.9"])
def test_the_bridge_s_command_line_runs(tmp_path, python):
    exe = shutil.which(python)
    if exe is None:
        pytest.skip(f"no {python} on this machine")  # the 3.9 floor needs a 3.9 to check it
    path = tmp_path / "r.md"
    path.write_text(report([finding(claim="line ~3 is off by one\u2028here")], PROSE), encoding="utf-8")
    run = subprocess.run([exe, str(BRIDGE), "parse", "correctness", str(path)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["findings"][0]["fingerprint"] == gf.fingerprint("correctness", "src/m.py",
                                                              "line ~3 is off by one\u2028here")
    assert out["prose_unfiled"] == [{"heading": "MINOR", "text": "Docstring names one stub only"}]


def test_a_bridge_with_no_module_beside_it_says_so_and_exits_1(tmp_path):
    lone = tmp_path / ".claude" / "hooks" / "lib"
    lone.mkdir(parents=True)
    shutil.copy(BRIDGE, lone / "r5_findings.py")
    run = subprocess.run([sys.executable, "-I", "-S", str(lone / "r5_findings.py"), "parse", "correctness", "x"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 1
    assert "cannot import curamoir.gate.findings from" in run.stderr
