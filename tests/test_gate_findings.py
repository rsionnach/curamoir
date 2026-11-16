"""gate/findings.py (cm-8ap.13): the shared Finding contract and the merge.

The parser's long-standing rules (schema, fingerprints, closures, the open
set, the prior file) are tested through the bridge in test_r5_supervisor.py,
which now runs on this module. This file tests what bead 1 adds
(docs/r5-gate-v2-curamoir-spec.md §5 R5-R11 to R5-R16, §10 bead 1, §11):

- the three parser fixes of spec v1.10 (line ends at LF only, ``~`` in a line
  reference, a re-raise beside an empty closure);
- ``prose_unfiled`` and the ``Not filed`` heading;
- the blocking set the caller passes (R5-R12, §11), clarity advisory under
  the contract's default;
- the corpus path: a replay entry's ``recorded.json`` round-trips losslessly,
  with a null path or range allowed there and nowhere else;
- the merge: dedupe, grouping, the severity policy, priority order, the
  deferred list, and its reproducibility from raw output;
- the fix object's ``superseded_by`` (R5-R17, §11);
- the bridge: the same objects, its own blocking set, and its command line
  under Python 3.9.

Fixtures are traced to the spec, which R5-R35 accepts (§10 bead 1, v1.11):
each test cites the spec rule it pins, and none is a trimmed real report,
which the publication rule forbids. The real artefacts used are the
delivered corpus's own entries.
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
#
# Pins §11's normalised-claim rule: a line reference may carry "~", and
# "line ~42" normalises as "line 42" does. The v1.10 inputs (a "line ~N" claim, a Unicode separator, a CR) are
# spec-traced (R5-R35): no real report, corpus claim or run record in this
# repository or NthLayer's holds one (checked 2026-11-15), so the real
# artefacts here exercise only the "false" side.

@pytest.mark.parametrize("claim", ["off by one at line ~42", "off by one at lines ~4-~9",
                                   "off by one at line ~4 \u2013 ~9", "off by one at line 42"])
def test_a_line_reference_with_a_tilde_normalises_as_one_without(claim):
    assert gf.normalise_claim(claim) == "off by one at"


def test_a_tilde_elsewhere_stays():
    assert gf.normalise_claim("expands ~ to the home directory") == "expands ~ to the home directory"


# ---- spec v1.10 fix 1: an output line ends at LF only
#
# Pins §11's Finding rule on line ends: LF ends a line, a CR before it is
# tolerated, and every Unicode separator (U+2028, U+2029, ...) is content.

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
#
# Pins §11's closure rule (a re-raise beside a closure of the same finding
# keeps the re-raise and rejects the closure) and R5-R25 (closing every prior
# finding with empty evidence is UNAVAILABLE).

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


# ---- R5-R12 and §11: the caller's blocking set
#
# Pins §11's advisory rule and R5-R12: the blocking set is the caller's
# configuration (default correctness, edge cases, provenance), not a constant
# in the parser; a lens outside it is advisory with nothing to prove, and a
# gate that still blocks on clarity still labels clarity's findings.

@pytest.mark.parametrize(("lens", "unproven"), [("correctness", 1), ("edge_cases", 1),
                                                ("provenance", 1), ("clarity", 0), ("excellence", 0)])
def test_under_the_default_set_only_a_blocking_lens_has_unproven_findings(lens, unproven):
    out = parse([finding(lens=lens, level="cited")], lens=lens)
    assert len(out["unproven"]) == unproven


def test_the_default_set_is_the_contract_s_three_lenses():
    assert gf.blocking_set(gf.BLOCKING_LENSES) == {"correctness", "edge_cases", "provenance"}


@pytest.mark.parametrize(("lens", "unproven"), [("clarity", 1), ("correctness", 0), ("excellence", 0)])
def test_a_caller_s_set_decides_which_lens_is_labelled(lens, unproven):
    out = parse([finding(lens=lens, level="cited")], lens=lens, blocking=("clarity",))
    assert len(out["unproven"]) == unproven


def test_a_blocking_set_takes_either_spelling_of_a_lens():
    assert gf.blocking_set(["edge-cases"]) == {"edge_cases"}
    out = parse([finding(lens="edge_cases", level="cited")], lens="edge_cases", blocking=["edge-cases"])
    assert len(out["unproven"]) == 1


@pytest.mark.parametrize(("blocking", "reason"), [
    ("correctness", "a collection of lens names"),       # a string is not a set of names
    ([1], "a collection of lens names"),
    (["bogus"], "unknown lens 'bogus'"),
    (["correctness", "excellence"], "excellence is advisory"),
], ids=["string", "not-a-name", "unknown", "excellence"])
def test_a_bad_blocking_set_is_refused(blocking, reason):
    with pytest.raises(ValueError, match=reason):
        parse([], blocking=blocking)
    with pytest.raises(ValueError, match=reason):
        gf.merge_round({}, blocking=blocking)


def test_an_empty_blocking_set_blocks_nothing():
    out = parse([finding(level="cited")], blocking=())
    assert out["unproven"] == []
    m = gf.merge_round(opened(finding(severity="CRITICAL")), blocking=())
    assert m["fix_list"] == [] and len(m["deferred"]) == 1


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


def test_a_tab_indented_sub_bullet_is_not_an_issue():
    prose = "#### Important\n1. **The parser crashes on an empty list**\n\t- File: a.py:1\n\t- Scenario: x\n"
    assert parse([], prose=prose)["prose_unfiled"] == [
        {"heading": "IMPORTANT", "text": "The parser crashes on an empty list"}]


# The shapes of this repository's own reviewers' Not-filed lines (the reports
# themselves are private): a parenthetical before the reason, then one gloss.
@pytest.mark.parametrize("line", ["- foo is fine \u2014 out_of_scope (outside the diff)",
                                  "- bar \u2014 `pre_existing` (there before this change).",
                                  "- baz: not_an_issue (looked at, and fine);",
                                  "- The probe passes `--timeout` against the variable (b0a1999): not_an_issue "
                                  "(the option outranks the variable).",
                                  "- None turned up (the freeze): out_of_scope (limits list: follow-up bead)."])
def test_a_not_filed_reason_may_carry_one_gloss(line):
    assert parse([], prose=f"#### Not filed\n{line}\n")["prose_unfiled"] == []


def test_a_gloss_is_not_a_reason():
    out = parse([], prose="#### Not filed\n- qux is noted (out_of_scope is not said here)\n")
    assert out["prose_unfiled"] == [{"heading": "Not filed", "text": "qux is noted (out_of_scope is not said here)"}]


def test_a_heading_may_end_in_a_colon():
    prose = "#### Important:\n1. **A defect under a colon heading**\n\n### Not filed:\n- no reason here\n"
    assert parse([], prose=prose)["prose_unfiled"] == [
        {"heading": "IMPORTANT", "text": "A defect under a colon heading"},
        {"heading": "Not filed", "text": "no reason here"}]


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
             "- foo is fine (not_an_issue).\n"
             "- bar is outside the diff (`out_of_scope`).\n"
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
    stale = [f for f in _corpus_findings() if gf.comparison_key(f) != f["fingerprint"]]
    assert stale, "corpus-v1 holds a finding fingerprinted by an older normaliser"
    assert gf.corpus_finding(stale[0])["fingerprint"] == stale[0]["fingerprint"]


def test_a_corpus_finding_with_no_path_is_keyed_with_an_empty_one():
    # As NthLayer's corpus writer computed it: every null-path finding's
    # stored fingerprint is sha256(lens||normalised claim).
    no_path = [f for f in _corpus_findings() if f["path"] is None]
    assert no_path and all(gf.comparison_key(f) == f["fingerprint"] for f in no_path)
    f = no_path[0]
    expect = hashlib.sha256(f"{f['lens']}||{gf.normalise_claim(f['claim'])}".encode()).hexdigest()[:16]
    assert gf.comparison_key(f) == expect


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
    ({"end_line": 3}, "both be null"),
    ({"start_line": 9, "end_line": 3}, "both be null"),
    ({"path": ""}, "path must be a non-empty string"),
    ({"claim": None}, "claim"),
    ({"evidence": 3}, "evidence must be a string or null"),
    ({"fingerprint": "XYZ"}, "fingerprint must be 16 hex"),
    ({"status": "done"}, "status must be one of"),
    ({"verification_level": "proven"}, "verification_level"),
    ({"resolves": "nope"}, "resolves"),
    ({"regression": "no"}, "regression"),
], ids=["pass-name-lens", "severity", "half-range", "half-range-end", "inverted-range", "empty-path", "no-claim",
        "evidence-type", "fingerprint", "status", "level", "resolves", "regression"])
def test_a_corpus_finding_is_still_checked_against_the_schema(change, reason):
    with pytest.raises(gf.ReportError, match=reason):
        gf.corpus_finding(_corpus_record(**change))


def test_a_malformed_recorded_json_is_refused():
    for bad, reason in [([], "JSON object"), ({"rounds": {}}, "rounds must be a list"),
                        ({"rounds": [1]}, "each round"), ({"rounds": [{"findings": {}}]}, "list or null"),
                        ({"rounds": [], "cross_repo_findings": [_corpus_record(severity="LOW")]}, "severity"),
                        ({"rounds": [{"findings": [_corpus_record()]},
                                     {"findings": [_corpus_record(severity="LOW")]}]}, "finding 2: severity")]:
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
    """Pins R5-R13's dedupe (overlapping ranges on one path, one normalised
    claim: one finding for both lenses), recorded as merge.deduped (§11).

    Synthetic, traced to that rule (R5-R35): the corpus holds no cross-lens
    duplicate (checked 2026-11-16, as a sequential gate would predict; the
    bridge runs lenses one at a time on trees that change between them).
    test_the_corpus_holds_no_cross_lens_duplicate keeps that claim checked."""
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


def test_the_corpus_holds_no_cross_lens_duplicate():
    # Why the dedupe fixture above is synthetic. If a delivered entry ever
    # holds one, this fails, and that entry becomes the dedupe fixture.
    for by_lens in _first_reviews():
        assert gf.merge_round(by_lens)["deduped"] == []


def test_the_corpus_s_real_grouped_case_is_grouped_and_not_deduped():
    """Pins R5-R13's grouping (overlapping ranges, different claims: separate
    entries, grouped), recorded as merge.grouped (§11), on the real case §10
    bead 1 names: corpus entry opensrm-wo62-4-3, where clarity and edge-cases
    overlap in manifest_builder.py with different claims."""
    rounds = json.loads((CORPUS / "opensrm-wo62-4-3" / "recorded.json").read_text())["rounds"]
    pick = {}
    for review in rounds:
        for f in review["findings"] or ():
            if f["path"] == "src/nthlayer_generate/dashboards/manifest_builder.py":
                pick[gf.schema_lens(review["lens"])] = f
    clarity, edge = pick["clarity"], pick["edge_cases"]
    assert gf.normalise_claim(clarity["claim"]) != gf.normalise_claim(edge["claim"])
    assert clarity["start_line"] <= edge["end_line"] and edge["start_line"] <= clarity["end_line"]
    m = gf.merge_round({"clarity": [clarity], "edge_cases": [edge]})
    c, e = gf.comparison_key(clarity), gf.comparison_key(edge)
    assert m["grouped"] == [[e, c]] and m["deduped"] == []
    # Two entries: edge cases blocks, so advisory clarity's IMPORTANT joins
    # the fix list after it (R5-R12, R5-R14).
    assert m["fix_list"] == [e, c]


def test_each_fingerprint_is_in_at_most_one_group():
    # §11: a fingerprint is in at most one group. A chain of overlaps
    # (a with b, b with c, a not with c) is one group, not two.
    a = finding(lens="correctness", start=10, end=12, claim="first")
    b = finding(lens="edge_cases", start=12, end=20, claim="second")
    c = finding(lens="provenance", start=20, end=22, claim="third")
    m = gf.merge_round(opened(a, b, c))
    assert len(m["grouped"]) == 1 and sorted(m["grouped"][0]) == sorted([fp(a), fp(b), fp(c)])
    flat = [x for g in m["grouped"] for x in g]
    assert len(flat) == len(set(flat))


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


def test_a_caller_that_blocks_on_clarity_has_clarity_block_alone():
    # R5-R12: the set is the caller's. With clarity in it, a clarity CRITICAL
    # makes a fix round of its own and sorts as a blocking entry.
    clarity = finding(lens="clarity", severity="CRITICAL", path="src/c.py", claim="unreadable")
    minor = finding(lens="correctness", severity="MINOR")
    m = gf.merge_round(opened(clarity, minor), blocking=("correctness", "clarity"))
    assert m["fix_list"] == [fp(clarity)] and m["entries"][fp(clarity)]["standing"] == "blocking"
    four = ("correctness", "clarity", "edge_cases", "provenance")
    raw = {"clarity": report([finding(lens="clarity", level="cited")])}
    done = gf.merge_from_raw(raw, blocking=four)
    assert done["lenses"]["clarity"]["unproven"] and done["merge"]["fix_list"]
    assert gf.merge_from_raw(raw)["merge"]["fix_list"] == []


def test_a_clarity_claim_a_blocking_lens_also_raised_blocks():
    clarity = finding(lens="clarity", severity="CRITICAL")
    correctness = finding(lens="correctness", severity="IMPORTANT")
    m = gf.merge_round(opened(clarity, correctness))
    # Correctness's record makes it block, so correctness's record stands for it.
    assert m["fix_list"] == [fp(correctness)] and m["entries"][fp(correctness)]["blocking"]
    assert m["entries"][fp(correctness)]["lenses"] == ["correctness", "clarity"]


@pytest.mark.parametrize("advisory", ["excellence", "clarity"])
def test_an_advisory_critical_merged_with_a_blocking_lens_s_minor_does_not_block(advisory):
    # Standing is decided record by record: the CRITICAL is advisory, the
    # blocking lens's record is a MINOR (R5-R12, R5-R27).
    minor = finding(lens="correctness", severity="MINOR")
    serious = finding(lens=advisory, severity="CRITICAL")
    m = gf.merge_round(opened(minor, serious))
    assert m["fix_list"] == [] and len(m["deferred"]) == 1
    entry = m["entries"][m["deferred"][0]]
    assert entry["blocking"] is False and entry["lenses"] == ["correctness", advisory]


def test_an_excellence_important_never_joins_the_fix_list_even_merged_with_clarity():
    blocking = finding(lens="provenance", severity="IMPORTANT", path="src/p.py")
    clarity = finding(lens="clarity", severity="MINOR")
    excellence = finding(lens="excellence", severity="IMPORTANT")
    m = gf.merge_round(opened(blocking, clarity, excellence))
    assert m["fix_list"] == [fp(blocking)]
    assert m["entries"][m["deferred"][0]]["standing"] == "deferred"


def test_a_blocking_entry_is_represented_by_its_blocking_record():
    # An excellence CRITICAL beside a correctness IMPORTANT: the entry blocks
    # through correctness, and its severity and lens are correctness's.
    correctness = finding(lens="correctness", severity="IMPORTANT")
    excellence = finding(lens="excellence", severity="CRITICAL")
    m = gf.merge_round(opened(correctness, excellence))
    assert m["fix_list"] == [fp(correctness)]
    entry = m["entries"][fp(correctness)]
    assert (entry["lens"], entry["severity"], entry["standing"]) == ("correctness", "IMPORTANT", "blocking")
    assert m["deduped"] == [[fp(correctness), fp(excellence)]]


def test_a_corpus_finding_with_no_location_is_never_deduped():
    a = {**_corpus_record(lens="correctness", severity="IMPORTANT", claim="same words")}
    b = {**_corpus_record(lens="edge_cases", severity="IMPORTANT", claim="same words")}
    m = gf.merge_round({"correctness": [a], "edge-cases": [b]})
    assert len(m["fix_list"]) == 2 and m["deduped"] == [] and m["grouped"] == []


@pytest.mark.parametrize("second", [
    {},                                                     # a null-path corpus pair
    {"start_line": 50, "end_line": 60, "severity": "MINOR"},  # one claim, apart
], ids=["no-location", "apart"])
def test_two_findings_with_one_key_in_an_open_set_are_refused(second):
    if second:
        a = finding(start=1, end=2)
        b = {**a, **second}
    else:
        a = _corpus_record(lens="correctness", severity="IMPORTANT")
        b = dict(a)
    with pytest.raises(ValueError, match="share the key"):
        gf.merge_round({"correctness": [a, b]})


def test_a_corpus_finding_with_a_null_level_or_no_location_keys_merges():
    bare = {k: v for k, v in _corpus_record(lens="correctness", severity="IMPORTANT",
                                            claim="no location keys").items()
            if k not in ("path", "start_line", "end_line")}
    null_level = _corpus_record(lens="correctness", severity="IMPORTANT", verification_level=None)
    assert gf.corpus_finding(bare) == bare and gf.corpus_finding(null_level) == null_level
    m = gf.merge_round({"correctness": [bare, null_level]})
    assert len(m["fix_list"]) == 2 and m["deduped"] == []
    # A null level ranks as asserted, below cited.
    assert gf._rank(null_level) == gf._rank({**null_level, "verification_level": "asserted"})
    assert gf._rank({**null_level, "verification_level": "cited"}) < gf._rank(null_level)


@pytest.mark.parametrize("call", ["merge_round", "merge_from_raw"])
def test_two_spellings_of_one_lens_are_refused(call):
    e = finding(lens="edge_cases")
    if call == "merge_round":
        args = {"edge-cases": prior(e, lens="edge_cases"), "edge_cases": []}
    else:
        args = {"edge-cases": report([e]), "edge_cases": report([])}
    with pytest.raises(ValueError, match="name one lens"):
        getattr(gf, call)(args)


@pytest.mark.parametrize("shape", [
    {"path": None, "start_line": 10, "end_line": 20},     # the corpus has one such finding
    {"path": "src/m.py", "start_line": None, "end_line": None},  # and nine of these
], ids=["no-path-with-range", "path-without-range"])
def test_corpus_findings_without_a_full_location_are_never_deduped_or_grouped(shape):
    a = _corpus_record(lens="correctness", severity="IMPORTANT", claim="one claim", **shape)
    b = _corpus_record(lens="edge_cases", severity="IMPORTANT", claim="one claim", **shape)
    c = _corpus_record(lens="provenance", severity="IMPORTANT", claim="another claim", **shape)
    m = gf.merge_round({"correctness": [a], "edge_cases": [b], "provenance": [c]})
    assert len(m["fix_list"]) == 3 and m["deduped"] == [] and m["grouped"] == []


def _first_reviews():
    """Each corpus entry's first-review findings by lens, one round per entry
    (entries with none skipped)."""
    for path in _recorded():
        by_lens = {}
        for review in json.loads(path.read_text())["rounds"]:
            if review["iteration"] == 1:
                for f in review["findings"] or ():
                    by_lens.setdefault(gf.schema_lens(review["lens"]), []).append(f)
        if by_lens:
            yield by_lens


def test_every_corpus_entry_s_first_review_merges():
    # The delivered corpus's real location shapes, merged as one round per
    # entry: nothing crashes, and every finding lands in exactly one list.
    merged = 0
    for by_lens in _first_reviews():
        m = gf.merge_round(by_lens)
        listed = m["fix_list"] + m["deferred"]
        assert len(listed) == len(set(listed))
        assert len(listed) + sum(len(d) - 1 for d in m["deduped"]) == sum(map(len, by_lens.values()))
        merged += 1
    assert merged >= 19


def test_equal_severity_and_lens_sort_by_location():
    # Fingerprints that order the other way from their paths.
    a = finding(path="src/a.py", claim="alpha")
    b = finding(path="src/b.py", claim="beta")
    if fp(a) < fp(b):
        a, b = finding(path="src/a.py", claim="gamma"), finding(path="src/b.py", claim="alpha")
    assert fp(a) > fp(b)
    later_line = finding(path="src/a.py", start=40, end=41, claim="delta")
    m = gf.merge_round(opened(b, later_line, a))
    assert m["fix_list"] == [fp(a), fp(later_line), fp(b)]


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


@pytest.mark.parametrize(("priors", "reason"), [
    ({"edge-cases": [], "edge_cases": []}, "name one lens"),
    ({"provenance": []}, "provenance, which has no report"),
    ({"bogus": []}, "unknown lens"),
], ids=["both-spellings", "no-report", "unknown"])
def test_merge_from_raw_refuses_a_prior_it_would_drop(priors, reason):
    with pytest.raises(ValueError, match=reason):
        gf.merge_from_raw({"edge-cases": report([])}, priors=priors)


def test_a_prior_spelled_either_way_reaches_its_lens():
    earlier = prior(finding(lens="edge_cases"), lens="edge_cases")
    for spelling in ("edge-cases", "edge_cases"):
        done = gf.merge_from_raw({"edge-cases": report([])}, priors={spelling: earlier})
        assert done["lenses"]["edge_cases"]["unaccounted"] == [earlier[0]["fingerprint"]]


def test_merge_from_raw_hands_each_lens_its_prior_open_set():
    earlier = prior(finding(lens="correctness"))
    raw = {"correctness": report([closure(earlier[0]["fingerprint"])])}
    done = gf.merge_from_raw(raw, priors={"correctness": earlier})
    assert done["lenses"]["correctness"]["closures"] == [earlier[0]["fingerprint"]]
    assert done["merge"]["fix_list"] == []


# ---- the fix object's superseded_by (R5-R17, §11)
#
# Pins §11's fix.superseded_by and R5-R17: each finding marked superseded
# names the finding whose fix rewrote its lines; a superseded status with no
# such entry, or naming a finding off the round's fix list, is recorded as
# could_not_fix.

def test_a_superseded_status_naming_a_finding_on_the_fix_list_is_kept():
    out = gf.fix_outcome({"aa": "fixed", "bb": "superseded"}, {"bb": "aa"}, ["aa", "bb"])
    assert out == {"statuses": {"aa": "fixed", "bb": "superseded"}, "superseded_by": {"bb": "aa"}}


@pytest.mark.parametrize("links", [None, {}, {"bb": "zz"}], ids=["none", "no-entry", "off-the-list"])
def test_a_superseded_status_without_a_valid_entry_is_could_not_fix(links):
    out = gf.fix_outcome({"aa": "fixed", "bb": "superseded"}, links, ["aa", "bb"])
    assert out == {"statuses": {"aa": "fixed", "bb": "could_not_fix"}, "superseded_by": {}}


def test_an_entry_for_a_finding_not_marked_superseded_is_dropped():
    statuses = {"aa": "fixed", "bb": "disputed"}
    links = {"bb": "aa", "cc": "aa"}
    out = gf.fix_outcome(statuses, links, ["aa", "bb"])
    assert out == {"statuses": statuses, "superseded_by": {}}
    assert links == {"bb": "aa", "cc": "aa"}  # the inputs are not changed


@pytest.mark.parametrize(("statuses", "links", "reason"), [
    ({"aa": "done"}, {}, "unknown status 'done'"),
    (["aa"], {}, "map a fingerprint"),
    ({"aa": "superseded"}, ["aa"], "map a fingerprint"),
    ({"aa": "superseded"}, {"aa": ["bb"]}, "by its fingerprint"),
], ids=["unknown-status", "statuses-list", "links-list", "link-not-a-string"])
def test_a_malformed_fix_outcome_is_refused(statuses, links, reason):
    with pytest.raises(ValueError, match=reason):
        gf.fix_outcome(statuses, links, ["aa", "bb"])


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


def test_the_bridge_passes_its_four_lens_set_so_clarity_keeps_its_label(tmp_path):
    # Spec v1.11, R5-R12: until clarity moves to advisory in a bead of its
    # own (cm-ig5), the bridge passes its four-lens set and labels clarity.
    # The module's own command line, under the contract's default, does not.
    bridge = _load_bridge()
    assert gf.blocking_set(bridge.BRIDGE_BLOCKING) == {"correctness", "clarity", "edge_cases", "provenance"}
    path = tmp_path / "r.md"
    path.write_text(report([finding(lens="clarity", level="cited")]), encoding="utf-8")
    run = subprocess.run([sys.executable, str(BRIDGE), "parse", "clarity", str(path)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert len(json.loads(run.stdout)["unproven"]) == 1
    module = subprocess.run([sys.executable, "-c",
                             "import sys; from curamoir.gate import findings; "
                             "sys.exit(findings.main(sys.argv[1:]))", "parse", "clarity", str(path)],
                            capture_output=True, text=True, timeout=60)
    assert module.returncode == 0, module.stderr
    assert json.loads(module.stdout)["unproven"] == []


def test_the_bridge_prefers_its_checkout_s_module_to_one_earlier_on_the_path(tmp_path):
    # A decoy curamoir ahead of everything on PYTHONPATH: the bridge puts its
    # own src/ in front, so the checkout's parser runs, not the decoy.
    decoy = tmp_path / "curamoir" / "gate"
    decoy.mkdir(parents=True)
    (tmp_path / "curamoir" / "__init__.py").write_text("")
    (decoy / "__init__.py").write_text("")
    (decoy / "findings.py").write_text("def main(argv):\n    print('DECOY')\n    return 0\n")
    path = tmp_path / "r.md"
    path.write_text(report([]), encoding="utf-8")
    run = subprocess.run([sys.executable, str(BRIDGE), "parse", "correctness", str(path)],
                         capture_output=True, text=True, timeout=60,
                         env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stderr
    assert "DECOY" not in run.stdout and json.loads(run.stdout)["findings"] == []


def test_a_bridge_with_no_module_beside_it_says_so_and_exits_1(tmp_path):
    lone = tmp_path / ".claude" / "hooks" / "lib"
    lone.mkdir(parents=True)
    shutil.copy(BRIDGE, lone / "r5_findings.py")
    run = subprocess.run([sys.executable, "-I", "-S", str(lone / "r5_findings.py"), "parse", "correctness", "x"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 1
    assert "cannot import curamoir.gate.findings from" in run.stderr
