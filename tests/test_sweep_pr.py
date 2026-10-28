"""cm-fkd.11 (cm-fkd.4/ST2): the sweep PR's branch name, title, commit
message and body, pure (curamoir.sweep_pr).

**Fixture provenance.** The body tests run on REAL sweep output:
tests/test_fix_counts.py's fixture_sweep drives the committed
`fixtures/sample-repo` through the real lint, remedies, scope check, test
gate and re-lint, and the four edits it applies fix every planted
violation PLANTED.json lists (the fifth, handlers_v2.py, is the runner's
remedy). With the test command `true` the gate keeps all three files and
record_fixes returns five RULE records; with fixture_sweep's grep command the
gate drops billing.py (a REDUCED gate with a real drop reason). The
acceptance test checks the body's (principle, path, line) triples against
PLANTED.json itself, not against a copy of it here.

Where a test needs a shape the fixture cannot give (a tier-1 note carrying a
token, a thousand fixes to overflow the cap), it builds the records through
the same pure cores the runner's records come from (tier1.tier1_fix_records,
fix_counts.rule_fix_records) and the real types (Tier1Finding,
ReportedViolation, GateResult), never parallel dicts.
"""

import dataclasses
import html
import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import curamoir.sweep_pr as sweep_pr_module
from curamoir.fix_counts import rule_fix_records
from curamoir.green_gate import ABANDONED, GREEN, REDUCED, DroppedFix, GateResult
from curamoir.idempotence import parse_sweep_marker, sweep_branch_prefix, sweep_marker
from curamoir.lint_report import ReportedViolation
from curamoir.pr_markdown import location
from curamoir.redaction import PLACEHOLDER, redact
from curamoir.remedies import SkippedRemedy
from curamoir.sweep import WorkList
from curamoir.sweep_pr import (
    GRADE_PENDING,
    MAX_BODY_SIZE,
    SweepBodyError,
    _fitted,
    commit_message,
    final_body,
    github_size,
    pr_records,
    pr_title,
    render_pr_body,
    sweep_branch,
)
from curamoir.tier1 import Tier1Finding, tier1_fix_records
from tests.test_fix_counts import (
    BILLING,
    BILLING_PRINT,
    BILLING_RAISE,
    HANDLERS,
    HANDLERS_PRINT,
    HANDLERS_RAISE,
    RED_IF_QUANTITY_ZEROED,
    VARIANT,
    fixture_sweep,
)

ROOT = Path(__file__).resolve().parents[1]
PLANTED = json.loads((ROOT / "fixtures" / "sample-repo" / "PLANTED.json")
                     .read_text(encoding="utf-8"))
EVERY_FIX = [BILLING_PRINT, BILLING_RAISE, HANDLERS_PRINT, HANDLERS_RAISE]
DAY = date(2026, 10, 23)
TOKEN = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"


#: Every entity pr_markdown.plain_text writes and its character: the
#: test's own copy of sweep_pr._ENTITIES, so a missing entry there is caught.
ENTITIES = {"&amp;": "&", "&lt;": "<", "&#91;": "[", "&#58;": ":", "&#46;": ".",
            "&#8203;": ""}


def rendered(body):
    """The body as GitHub shows it: plain_text's entities read back, in one
    pass."""
    return re.sub("|".join(map(re.escape, ENTITIES)), lambda m: ENTITIES[m.group()], body)


#: `https://evil.example` as plain_text writes it: no autolink.
EVIL = "https&#58;//evil.example"


@pytest.fixture(scope="module")
def green(tmp_path_factory):
    """The real sweep of the fixture with every planted violation fixed and
    a green gate: SimpleNamespace(work, gate, remedies, records, base)."""
    work, gate, remedies, records, base = fixture_sweep(
        tmp_path_factory.mktemp("green"), EVERY_FIX)
    return SimpleNamespace(work=work, gate=gate, remedies=remedies, records=records, base=base)


@pytest.fixture(scope="module")
def reduced(tmp_path_factory):
    """The same sweep, but the tests are red with the billing fix: the gate
    drops billing.py."""
    work, gate, remedies, records, base = fixture_sweep(
        tmp_path_factory.mktemp("reduced"), EVERY_FIX, command=RED_IF_QUANTITY_ZEROED)
    return SimpleNamespace(work=work, gate=gate, remedies=remedies, records=records, base=base)


def body_of(run, records=None, work=None, gate=None, skipped=(), left_out=(), **options):
    return final_body(run.records if records is None else records,
                      run.work if work is None else work,
                      run.gate if gate is None else gate,
                      run.base, skipped, left_out, **options)


def section(body, heading):
    """The lines under `### heading`, up to the next heading."""
    lines = body.splitlines()
    start = lines.index(f"### {heading}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("#")), len(lines))
    return [line for line in lines[start:end] if line]


def fix_lines(body):
    return [line for line in section(body, "Fixes") if line.startswith("- ")]


def tier1_record(base, path=HANDLERS, line=31, note="re-raise drops the cause",
                 principle_id="errors-carry-context", number=1, end_line=None):
    """A tier-1 finding and its record, through tier1.tier1_fix_records (the
    pure core of record_tier1_fixes): (finding, record)."""
    end_line = line if end_line is None else end_line
    finding = Tier1Finding(f"t1-{number:02d}", principle_id, path, line, end_line, "q", note)
    (record,) = tier1_fix_records(
        [finding], {path: [SimpleNamespace(old_start=line, old_count=1)]},
        kept={path}, replies={number: "fixed"}, base=base)
    return finding, record


# --- the acceptance criteria -----------------------------------------------------------

def test_the_fixtures_five_fixes_are_listed_with_their_three_principle_ids(green):
    """AC1: one line per fix, each naming its principle id and the planted
    violation's path and line, exactly PLANTED.json's five across three
    principles; the deletion says it was the runner's remedy."""
    assert len(green.records) == 5
    assert green.gate.strategy == GREEN

    body = body_of(green)

    lines = fix_lines(body)
    assert len(lines) == 5
    listed = set()
    for line in lines:
        principle, rule, path = line.split("`")[1:6:2]
        number = line.split("` line ", 1)[1].split()[0]
        listed.add((principle, path, int(number)))
        assert rule in {r.rule_id for r in green.records}
    assert listed == {(v["principle_id"], v["path"], v["line"])
                      for v in PLANTED["violations"]}
    assert {p for p, _, _ in listed} == {"structured-logging", "errors-carry-context",
                                         "no-file-variant-proliferation"}
    (deleted,) = [line for line in lines if VARIANT in line]
    assert "remedy" in deleted
    assert all("remedy" not in line for line in lines if VARIANT not in line)
    assert "5 fixes of 3 principles" in body


def test_the_marker_survives_redaction_and_parses_to_the_base(green):
    """AC2, and the marker is a whole line of its own."""
    body = body_of(green)

    assert parse_sweep_marker(body) == green.base
    assert sweep_marker(green.base) in body.splitlines()


PEM = "-----BEGIN RSA PRIVATE KEY----- MIIE"


def truncated_key_body(green, where):
    """The body with a truncated private key (PEM, no END line) at `where`."""
    note = f"key pasted here: {PEM}" if where == "note" else "n"
    finding, record = tier1_record(green.base, note=note)
    work = dataclasses.replace(green.work, tier1=(finding,))
    left_out = (f"keys/{PEM}.pem",) if where == "path" else ()
    skipped = ((SkippedRemedy("src/a.py", "r", "delete_file", f"quotes {PEM}"),)
               if where == "reason" else ())
    return body_of(green, records=(*green.records, record), work=work, left_out=left_out,
                   skipped=skipped)


@pytest.mark.parametrize("where", ["note", "path", "reason"])
def test_a_truncated_private_key_in_repository_text_is_redacted_on_its_own_line(green,
                                                                                 where):
    """redaction's PEM rule replaces a key with no END marker through to the
    end of the text. pr_markdown.one_line redacts each piece of repository or
    agent text on its own, so the key goes to the end of that piece, not of
    the body: every section survives."""
    body = truncated_key_body(green, where)

    assert "PRIVATE KEY" not in body and "MIIE" not in body
    assert PLACEHOLDER in rendered(body)
    assert body.splitlines().count("### Grade") == 1
    assert section(body, "Grade") == [GRADE_PENDING]


def test_a_body_whose_marker_does_not_read_back_is_refused(green, monkeypatch):
    """final_body raises rather than return a body idempotence could not
    read: a sweep PR GitHub cannot place would make every later sweep
    refuse."""
    monkeypatch.setattr(sweep_pr_module, "sweep_marker", lambda base: sweep_marker("d" * 40))

    with pytest.raises(SweepBodyError, match="marker"):
        body_of(green)


def test_a_body_redaction_would_still_change_is_refused(green, monkeypatch):
    """The backstop verifies, never rewrites (module docstring,
    "Redaction"): a body that redaction would change is not sent."""
    monkeypatch.setattr(sweep_pr_module, "redact", lambda text: text.replace("base=", "b="))

    with pytest.raises(SweepBodyError, match="redaction would still change"):
        body_of(green)


def test_a_token_in_a_tier1_note_is_redacted(green):
    """AC3: the scan's note is the agent's text, so E0-R7 applies to it."""
    finding, record = tier1_record(green.base, note=f"uses {TOKEN} inline")
    work = dataclasses.replace(green.work, tier1=(finding,))

    body = body_of(green, records=(*green.records, record), work=work)

    assert TOKEN not in body and "ghp_" not in body
    (line,) = [line for line in fix_lines(body) if "tier 1" in line]
    assert line == (f"- `errors-carry-context` (tier 1: no rule yet) `{HANDLERS}` line 31: "
                    f"uses &#91;{PLACEHOLDER[1:]} inline")  # `[` as an entity: plain_text
    assert len(fix_lines(body)) == 6


def test_records_outside_paths_for_pr_are_not_listed(green):
    """AC4: the green run's five records against a gate that kept only
    handlers.py and the deletion: billing.py's two are not in the PR, so
    they are not in its body, its title or its commit message."""
    gate = dataclasses.replace(
        green.gate, strategy=REDUCED, kept=(HANDLERS, VARIANT),
        dropped=(DroppedFix(BILLING, "the tests fail with this fix"),))

    body = body_of(green, gate=gate)

    lines = fix_lines(body)
    assert len(lines) == 3
    assert not any(BILLING in line for line in lines)
    assert [r.path for r in pr_records(green.records, gate)] == [HANDLERS, VARIANT, HANDLERS]
    assert pr_title(green.records, gate, DAY).startswith("curamoir sweep 2026-10-23: 3 fixes")
    assert commit_message(green.records, gate, DAY, green.base) == (
        "curamoir sweep 2026-10-23: 3 fixes of 3 principles\n"
        "\n"
        f"Base: {green.base}\n"
        "\n"
        "errors-carry-context: 1\n"
        "no-file-variant-proliferation: 1\n"
        "structured-logging: 1\n")


def test_an_over_long_body_is_truncated_and_keeps_the_marker():
    """AC5: a thousand fixes overflow GitHub's limit; the fix list is cut
    with a count of what was left out, and the marker stays."""
    base = "c" * 40
    items = [violation(n) for n in range(1500)]
    records = rule_fix_records(items, [], kept={item.path for item in items}, remedied=(),
                               base=base)
    work = work_of(items)
    gate = green_gate(sorted({item.path for item in items}))

    full = render_pr_body(records, work, gate, base, (), (), limit=10**9)
    assert github_size(full) > MAX_BODY_SIZE

    body = final_body(records, work, gate, base, (), ())

    assert github_size(body) <= MAX_BODY_SIZE
    assert parse_sweep_marker(body) == base
    shown = fix_lines(body)
    assert shown[-1].startswith("- ... and ")
    more = int(shown[-1].split()[3])
    assert len(shown) - 1 + more == 1500
    assert shown[:-1] == fix_lines(full)[:len(shown) - 1]
    # Everything after the fix list is still there.
    assert section(body, "Promotion candidates") and section(body, "Grade")


def test_the_branch_is_the_configured_prefix_the_date_and_the_short_base():
    """AC6: <guardrails.branch_prefix>sweep-YYYY-MM-DD-<base[:8]>, so the
    cap and the duplicate check (sweep_branch_prefix) see it."""
    base = "0123456789abcdef0123456789abcdef01234567"

    branch = sweep_branch("bot/", DAY, base)

    assert branch == "bot/sweep-2026-10-23-01234567"
    assert branch.startswith(sweep_branch_prefix("bot/"))
    assert sweep_branch("curamoir/", DAY, base) == "curamoir/sweep-2026-10-23-01234567"


@pytest.mark.parametrize("prefix", ["", " bot/", "bot/ ", None])
def test_a_prefix_the_config_would_refuse_is_refused(prefix):
    with pytest.raises(ValueError, match="branch_prefix"):
        sweep_branch(prefix, DAY, "a" * 40)


@pytest.mark.parametrize("base", ["a" * 39, "A" * 40, "g" * 40, "a" * 8, None, "a" * 41])
def test_a_base_that_is_not_a_full_sha_is_refused(base):
    with pytest.raises(ValueError, match="base"):
        sweep_branch("curamoir/", DAY, base)


def test_a_sha256_base_is_shortened_the_same_way():
    assert sweep_branch("curamoir/", DAY, "b" * 64) == "curamoir/sweep-2026-10-23-bbbbbbbb"


@pytest.mark.parametrize("day", ["2026-10-23", datetime(2026, 10, 23, 12, 0)])
def test_the_date_must_be_exactly_a_date(day, green):
    """The caller passes the UTC day; a datetime is refused, not truncated
    in some time zone."""
    with pytest.raises(ValueError, match="date"):
        sweep_branch("curamoir/", day, "a" * 40)
    with pytest.raises(ValueError, match="date"):
        pr_title(green.records, green.gate, day)


# --- the rest of the body --------------------------------------------------------------

def test_the_gate_its_drops_and_the_files_left_for_the_next_run_are_listed(reduced):
    """The real REDUCED gate: its summary (render() without the paths,
    which the cut lists carry), the dropped file with its
    reason, and the files left for the next run, both those trimmed past
    max_files_per_pr and those the gate dropped."""
    assert reduced.gate.strategy == REDUCED
    (drop,) = reduced.gate.dropped

    body = body_of(reduced, left_out=("src/sample/extra.py",))

    gate = section(body, "Test gate")
    assert gate[0] == ("tests red with every fix; reduced to 2 fixed file(s) that are green, "
                       f"dropping the 1 listed below ({len(reduced.gate.runs)} runs)")
    assert BILLING in reduced.gate.render() and BILLING not in gate[0]
    assert f"- `{BILLING}`: {drop.reason}" in gate
    assert section(body, "Files left for the next run") == [
        "- `src/sample/extra.py` (past `max_files_per_pr`)",
        f"- `{BILLING}` (dropped by the test gate)"]
    assert len(fix_lines(body)) == len(reduced.records) == 3


def test_a_green_gate_drops_nothing_and_leaves_nothing(green):
    body = body_of(green)

    assert section(body, "Test gate") == [green.gate.render(), "Nothing was dropped."]
    assert section(body, "Files left for the next run") == ["None."]
    assert section(body, "Remedies skipped") == ["None."]


def test_skipped_remedies_are_listed_with_their_reasons(green):
    skipped = (SkippedRemedy("src/sample/web/old_v1.py", "no-variant-module", "delete_file",
                             "still imported by src/sample/web/app.py"),)

    body = body_of(green, skipped=skipped)

    assert section(body, "Remedies skipped") == [
        "- `src/sample/web/old_v1.py` (`no-variant-module`, remedy `delete_file`): "
        "still imported by src/sample/web/app.py"]


def test_promotion_candidates_are_empty_with_the_reason(green):
    (text,) = section(body_of(green), "Promotion candidates")

    assert "E4-R1" in text and "E4-R2" in text and "not landed" in text


def test_the_grade_hook_says_not_graded_until_a_section_is_given(green):
    assert section(body_of(green), "Grade") == [GRADE_PENDING]
    assert GRADE_PENDING == "not graded yet (Epic 7)"

    given = body_of(green, grade_section="B to A: test coverage moved")

    assert section(given, "Grade") == ["B to A: test coverage moved"]


def test_a_tier1_fix_is_listed_as_tier1_pr_lines_gives_it(green):
    finding, record = tier1_record(green.base)
    work = dataclasses.replace(green.work, tier1=(finding,))

    body = body_of(green, records=(*green.records, record), work=work)

    assert fix_lines(body)[-1] == (f"- `errors-carry-context` (tier 1: no rule yet) "
                                   f"`{HANDLERS}` line 31: re-raise drops the cause")


def test_text_naming_the_marker_word_cannot_make_the_marker_unreadable(green):
    """parse_sweep_marker reads any other line naming `curamoir-sweep` as a
    forged marker and gives up; a note (or a path) that names it must not
    turn the sweep's own body unreadable."""
    note = f"see {sweep_marker('d' * 40)} and curamoir-sweep"
    finding, record = tier1_record(green.base, note=note)
    work = dataclasses.replace(green.work, tier1=(finding,))

    body = body_of(green, records=(*green.records, record), work=work,
                   grade_section="curamoir-sweep")

    assert parse_sweep_marker(body) == green.base
    assert body.count("curamoir-sweep") == 1


def test_an_abandoned_gate_has_no_pr_body(green):
    gate = dataclasses.replace(green.gate, strategy=ABANDONED, cause="base_red", why="red")

    with pytest.raises(ValueError, match="kept no file"):
        body_of(green, gate=gate)


def test_records_of_another_base_are_refused(green):
    with pytest.raises(ValueError, match="base"):
        final_body(green.records, green.work, green.gate, "e" * 40, (), ())


def test_redaction_that_lengthens_the_body_is_still_held_under_the_cap(green):
    """A short secret value becomes `[REDACTED]`, two characters longer: the
    cap is checked on the redacted body, the one GitHub receives. The limit
    is exactly the size of the same body with the secrets' NAME misspelt
    (`tokxn`, which redaction leaves alone, so no list is cut): a fit
    measured before the grade is redacted would keep every line and send a
    body over the limit."""
    grade = "\n".join(["token=abc12345"] * 50)
    same_size = grade.replace("token", "tokxn")
    left_out = tuple(f"src/generated/module_{n:03d}.py" for n in range(40))
    limit = github_size(render_pr_body(green.records, green.work, green.gate, green.base, (),
                                       left_out, grade_section=same_size, limit=10**9))
    assert redact(same_size) == same_size and len(redact(grade)) == len(grade) + 100

    body = body_of(green, left_out=left_out, grade_section=grade, limit=limit)

    assert github_size(body) <= limit
    assert section(body, "Grade") == ["token=&#91;REDACTED]"] * 50
    assert section(body, "Files left for the next run")[-1].startswith("- ... and ")
    assert parse_sweep_marker(body) == green.base


def test_an_overlong_left_out_list_is_cut_too(green):
    left_out = tuple(f"src/generated/module_{n:05d}.py" for n in range(5000))

    body = body_of(green, left_out=left_out)

    assert github_size(body) <= MAX_BODY_SIZE
    assert parse_sweep_marker(body) == green.base
    assert len(fix_lines(body)) == 5
    assert section(body, "Files left for the next run")[-1].startswith("- ... and ")


# --- locations redaction leaves alone, Markdown that stays closed, the cap's edges -----

def balanced(line):
    """Every backtick run on `line` opens a code span that a later run of
    the same length closes (CommonMark's rule), with none left over: no
    span is cut short and no stray backtick is shown literally."""
    runs = [match.group() for match in re.finditer(r"`+", line)]
    while runs:
        opener = runs.pop(0)
        if opener not in runs:
            return False
        runs = runs[runs.index(opener) + 1:]
    return True


def test_a_tier1_location_in_a_file_named_for_a_secret_survives_redaction(green):
    """redaction's generic rule reads `token_store.py` as a NAME, and a `:`
    after it as its separator: `path:120-125` lost its lines to the
    placeholder. The location is `path` lines N-M, which it leaves alone."""
    path = "src/auth/token_store.py"
    finding, record = tier1_record(green.base, path=path, line=120, end_line=125,
                                   note="re-raise drops the cause")
    work = dataclasses.replace(green.work, tier1=(finding,))
    gate = dataclasses.replace(green.gate, kept=(*green.gate.kept, path))

    body = body_of(green, records=(*green.records, record), work=work, gate=gate)

    (line,) = [line for line in fix_lines(body) if path in line]
    assert line == (f"- `errors-carry-context` (tier 1: no rule yet) `{path}` lines 120-125: "
                    "re-raise drops the cause")
    assert PLACEHOLDER not in body
    assert all(balanced(line) for line in fix_lines(body))


def test_a_rule_location_in_a_file_named_for_a_secret_survives_redaction():
    base = "1" * 40
    item = dataclasses.replace(violation(1), path="src/secrets.py", line=12345678)
    records = rule_fix_records([item], [], kept={item.path}, remedied=(), base=base)

    body = final_body(records, work_of([item]), green_gate([item.path]), base, (), ())

    (line,) = fix_lines(body)
    assert line == "- `structured-logging` `no-bare-print` `src/secrets.py` line 12345678"
    assert PLACEHOLDER not in body and balanced(line)


def test_a_body_of_short_lines_fits_even_if_github_counts_newlines_as_crlf(green):
    left_out = tuple(f"a/{n}.py" for n in range(20000))

    body = body_of(green, left_out=left_out)

    assert len(body.replace("\n", "\r\n").encode("utf-8")) <= MAX_BODY_SIZE < 65536
    assert section(body, "Files left for the next run")[-1].startswith("- ... and ")


def test_the_fit_tolerates_a_size_that_is_not_monotone_in_the_cut():
    """A `... and N more` line can be longer than the line it replaces, so
    keeping 0 lines can be longer than keeping 1: the search must not take
    0's failure for "nothing fits", and must only return what it checked."""
    sizes = {0: 120, 1: 50, 2: 80, 3: 95, 4: 200}

    def compose(keep):
        return "x" * sizes[keep]

    def fits(text):
        return len(text) <= 90

    assert len(_fitted(compose, 4, fits)) == 80
    assert len(_fitted(compose, 4, lambda text: len(text) <= 60)) == 50
    with pytest.raises(SweepBodyError):
        _fitted(compose, 4, lambda text: len(text) <= 40)
    only_zero_fits = {0: 10, 1: 50, 2: 80}
    assert _fitted(lambda keep: "x" * only_zero_fits[keep], 2,
                   lambda text: len(text) <= 20) == "x" * 10


def test_a_rule_record_the_work_list_does_not_hold_is_refused(green):
    work = dataclasses.replace(green.work, items=())

    with pytest.raises(ValueError, match="fingerprint"):
        body_of(green, work=work)


def test_a_backtick_in_a_path_or_rule_id_cannot_break_its_code_span(green):
    odd = "src/odd`name.py"
    skipped = (SkippedRemedy("src/x``y.py", "rule`id", "delete_file", "still imported"),)

    body = body_of(green, left_out=(odd,), skipped=skipped)

    assert section(body, "Files left for the next run")[0] == (
        "- ``src/odd`name.py`` (past `max_files_per_pr`)")
    assert section(body, "Remedies skipped") == [
        "- ```src/x``y.py``` (``rule`id``, remedy `delete_file`): still imported"]
    assert all(balanced(line) for line in body.splitlines())


def test_an_html_comment_opener_in_repository_text_cannot_hide_the_body(green):
    skipped = (SkippedRemedy("src/a.py", "r", "delete_file", "see <!-- hidden"),)

    body = body_of(green, skipped=skipped, left_out=("src/<!--.py",),
                   grade_section="<!-- grade")

    assert body.count("<!--") == 1 and body.startswith("<!-- curamoir-sweep base=")
    assert section(body, "Remedies skipped") == [
        "- `src/a.py` (`r`, remedy `delete_file`): see &lt;!-- hidden"]
    assert section(body, "Grade") == ["&lt;!-- grade"]
    assert parse_sweep_marker(body) == green.base


# --- repository text cannot add structure; what the body refuses ----------------------

FORGED_SECTION = "x.py\n\n### Test gate\ntests green with all 99 fixed file(s)\n\n```"


def test_a_line_break_in_a_path_cannot_forge_a_section(green):
    body = body_of(green, left_out=(FORGED_SECTION,))

    assert body.splitlines().count("### Test gate") == 1
    (line,) = section(body, "Files left for the next run")
    assert line == ("- ```` x.py\\n\\n### Test gate\\ntests green with all 99 fixed "
                    "file(s)\\n\\n``` ```` (past `max_files_per_pr`)")
    assert balanced(line)


@pytest.mark.parametrize("forged_base", ["same", "other"])
def test_a_line_break_in_a_path_cannot_forge_a_marker(green, forged_base):
    other = green.base if forged_base == "same" else "d" * 40
    path = f"x.py\n{sweep_marker(other)}"

    body = body_of(green, left_out=(path,))

    assert parse_sweep_marker(body) == green.base
    assert [line for line in body.splitlines() if "<!--" in line] == [sweep_marker(green.base)]


def test_a_line_break_in_a_reason_stays_on_its_line(green):
    skipped = (SkippedRemedy("src/a.py", "r", "delete_file", "used by\n### Grade\nA+"),)

    body = body_of(green, skipped=skipped)

    assert section(body, "Remedies skipped") == [
        "- `src/a.py` (`r`, remedy `delete_file`): used by\\n### Grade\\nA+"]
    assert section(body, "Grade") == [GRADE_PENDING]


def test_a_bidi_override_in_a_path_is_shown_escaped(green):
    body = body_of(green, left_out=("src/\u202egnp.exe",))

    assert "\u202e" not in body
    assert section(body, "Files left for the next run") == [
        "- `src/\\u202egnp.exe` (past `max_files_per_pr`)"]


def test_a_grade_section_with_headings_of_its_own_is_refused(green):
    with pytest.raises(SweepBodyError, match="headings"):
        body_of(green, grade_section="B\n### Fixes\nnone")


def test_a_grade_section_with_a_subheading_is_refused(green):
    """E7-R5's contract (final_body's docstring): the grade is plain text,
    with no Markdown headings, so a `####` of its own is refused, not sent."""
    with pytest.raises(SweepBodyError, match="headings"):
        body_of(green, grade_section="B+\n#### Details\nsee the ledger")


# --- secrets behind a separator are redacted before it is escaped ---------------------

#: (id, the text placed after the separator, the secret that must not reach
#: the body). Each is a real match of its redaction rule (checked below).
SECRETS = [
    ("ghp", TOKEN, TOKEN),
    ("akia", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("sk", "sk-" + "A1b2C3d4" * 3, "sk-" + "A1b2C3d4" * 3),
    ("xoxb", "xoxb-1234567890-abcdefghijkl", "xoxb-1234567890-abcdefghijkl"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"),
    ("bearer", "Bearer abcdefghijklmnop0123456789", "abcdefghijklmnop0123456789"),
]

#: Separators an escape turns into word characters (`\n` becomes `\\n`,
#: U+200B becomes `\\u200b`), which a `\b`-anchored rule then cannot match after.
SEPARATORS = [("newline", "\n"), ("tab", "\t"), ("zwsp", "​"), ("ctrl-a", "\x01")]


def secret_body(green, reduced, where, text):
    """The body with `text` at `where`: a path left out of the PR, a skip
    reason, the reduced gate's drop reason, or a tier-1 note."""
    if where == "path":
        return body_of(green, left_out=(f"src/x{text} tail.py",))
    if where == "skip-reason":
        return body_of(green, skipped=(SkippedRemedy("src/a.py", "r", "delete_file",
                                                     f"used by{text} tail"),))
    if where == "drop-reason":
        (drop,) = reduced.gate.dropped
        gate = dataclasses.replace(reduced.gate,
                                   dropped=(DroppedFix(drop.path, f"red{text} tail"),))
        return body_of(reduced, gate=gate)
    finding, record = tier1_record(green.base, note=f"uses{text} tail")
    return body_of(green, records=(*green.records, record),
                   work=dataclasses.replace(green.work, tier1=(finding,)))


@pytest.mark.parametrize("separator", [s for _, s in SEPARATORS],
                         ids=[name for name, _ in SEPARATORS])
@pytest.mark.parametrize("where", ["path", "skip-reason", "drop-reason", "tier1-note"])
@pytest.mark.parametrize("text,secret", [(t, s) for _, t, s in SECRETS],
                         ids=[name for name, _, _ in SECRETS])
def test_a_secret_after_a_separator_is_redacted(green, reduced, text, secret, where,
                                                separator):
    assert secret not in redact(f"x{separator}{text} tail")  # a real match of its rule

    body = secret_body(green, reduced, where, f"{separator}{text}")

    assert secret not in body
    assert PLACEHOLDER in rendered(body)


# --- inline HTML in agent text cannot add structure -----------------------------------

HTML = "<h3>Test gate</h3>tests green <details>"


@pytest.mark.parametrize("where", ["tier1-note", "skip-reason"])
def test_inline_html_in_agent_text_is_shown_not_rendered(green, reduced, where):
    body = secret_body(green, reduced, where, f" {HTML}")  # refused if a heading were forged

    assert "&lt;h3>Test gate&lt;/h3>tests green &lt;details>" in body
    assert "<h3>" not in body and "<details>" not in body
    assert body.splitlines().count("### Test gate") == 1


@pytest.mark.parametrize("where", ["tier1-note", "skip-reason", "drop-reason"])
def test_a_markdown_link_or_image_in_agent_text_is_not_live(green, reduced, where):
    body = secret_body(green, reduced, where,
                       " [click](https://evil.example) ![x](https://evil.example/p.png)")

    assert f"&#91;click]({EVIL})" in body
    assert f"!&#91;x]({EVIL}/p.png)" in body
    assert "[click](" not in body and "[x](" not in body


@pytest.mark.parametrize("where", ["path", "skip-reason", "drop-reason"])
def test_a_private_key_without_its_end_line_stays_in_its_own_piece(green, reduced, where):
    """Redaction runs on each piece before the body is composed, so a key
    with no END line is cut at the end of its piece, not the body's: every
    section survives and the body is sent."""
    body = secret_body(green, reduced, where,
                       " -----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA")

    assert "MIIEpAIBAAKCAQEA" not in body
    assert [line for line in body.splitlines() if line.startswith("### ")] == [
        "### Fixes", "### Test gate", "### Files left for the next run",
        "### Remedies skipped", "### Promotion candidates", "### Grade"]


def test_the_cap_error_names_the_part_that_is_too_long(green):
    with pytest.raises(SweepBodyError, match="grade section"):
        body_of(green, grade_section="g" * (MAX_BODY_SIZE + 1))


def test_a_gate_that_dropped_thousands_still_fits(green):
    """REDUCED render() lists every dropped path on one line, never cut; the
    body's gate line is the summary, and the lists that carry the paths are
    cut."""
    dropped = tuple(DroppedFix(f"src/gen/m{n:05d}.py", "red") for n in range(6000))
    gate = dataclasses.replace(green.gate, strategy=REDUCED, dropped=dropped)
    assert github_size(gate.render()) > MAX_BODY_SIZE

    body = body_of(green, gate=gate)

    assert github_size(body) <= MAX_BODY_SIZE
    assert section(body, "Test gate")[0].endswith(
        f"dropping the 6000 listed below ({len(green.gate.runs)} runs)")


def test_a_pr_whose_files_carry_no_fix_is_refused(green):
    gate = dataclasses.replace(green.gate, kept=("src/sample/other.py",))

    with pytest.raises(ValueError, match="no fix lands"):
        body_of(green, gate=gate)
    with pytest.raises(ValueError, match="no fix lands"):
        pr_title(green.records, gate, DAY)
    with pytest.raises(ValueError, match="no fix lands"):
        commit_message(green.records, gate, DAY, green.base)


def test_a_left_out_path_that_is_in_the_pr_is_refused(green):
    with pytest.raises(ValueError, match="both in the PR and left out"):
        body_of(green, left_out=(HANDLERS,))


def test_two_records_of_one_fingerprint_are_refused(green):
    with pytest.raises(ValueError, match="fingerprint"):
        body_of(green, records=(*green.records, green.records[0]))


@pytest.mark.parametrize(("line", "end_line"), [(0, None), (5, 4), (-1, 3), (True, None),
                                                (1.0, None)])
def test_a_location_out_of_order_or_below_one_is_refused(line, end_line):
    with pytest.raises(ValueError, match="line"):
        location("a.py", line, end_line)


def test_a_lone_carriage_return_counts_as_two():
    assert github_size("a\rb") == 4
    assert github_size("a\nb") == 4
    assert github_size("a\r\nb") == 5


# --- title and commit message ----------------------------------------------------------

def test_the_title_names_the_date_and_counts(green):
    assert pr_title(green.records, green.gate, DAY) == (
        "curamoir sweep 2026-10-23: 5 fixes of 3 principles")


def test_the_title_is_singular_for_one_fix(green):
    gate = dataclasses.replace(green.gate, kept=(VARIANT,))

    assert pr_title(green.records, gate, DAY) == (
        "curamoir sweep 2026-10-23: 1 fix of 1 principle")


def test_the_commit_message_is_the_title_then_the_base_and_each_principle(green):
    message = commit_message(green.records, green.gate, DAY, green.base)

    assert message == (
        "curamoir sweep 2026-10-23: 5 fixes of 3 principles\n"
        "\n"
        f"Base: {green.base}\n"
        "\n"
        "errors-carry-context: 2\n"
        "no-file-variant-proliferation: 1\n"
        "structured-logging: 2\n")


def test_the_commit_message_sorts_its_principles_whatever_the_record_order(green):
    message = commit_message(tuple(reversed(green.records)), green.gate, DAY, green.base)

    assert message.splitlines()[-3:] == ["errors-carry-context: 2",
                                         "no-file-variant-proliferation: 1",
                                         "structured-logging: 2"]


def test_the_commit_message_and_the_parts_refuse_a_base_that_is_not_a_sha(green):
    """commit_message must not write `Base: garbage`; _parts checks the base
    itself, even when the records carry the same bad base."""
    with pytest.raises(ValueError, match="full commit SHA"):
        commit_message(green.records, green.gate, DAY, "garbage")
    records = tuple(dataclasses.replace(r, base="G" * 40) for r in green.records)
    with pytest.raises(ValueError, match="full commit SHA"):
        sweep_pr_module._parts(records, green.work, green.gate, "G" * 40, (), (), None)


# --- the header, the gate line, tier-1 notes ------------------------------------------

def test_the_header_says_file_or_files(green):
    one = dataclasses.replace(green.gate, kept=(VARIANT,))

    assert section_text(body_of(green), "curamoir sweep") == (
        f"5 fixes of 3 principles at base `{green.base}`, in 3 files. Each fix is listed "
        "with its principle id; the sweep's ledger line records every one.")
    assert section_text(body_of(green, gate=one), "curamoir sweep").startswith(
        f"1 fix of 1 principle at base `{green.base}`, in 1 file. ")


def test_a_green_gate_line_is_plain_text(green, monkeypatch):
    """GateResult.render() is shown outside a code span, so it goes through
    plain_text like any other text there: no HTML, no live link."""
    monkeypatch.setattr(GateResult, "render",
                        lambda self: "tests green <h3>x</h3> [x](https://evil.example)")

    body = body_of(green)

    assert section(body, "Test gate")[0] == (
        f"tests green &lt;h3>x&lt;/h3> &#91;x]({EVIL})")
    assert "<h3>" not in body


def test_a_backtick_in_a_tier1_note_is_shown_as_a_quote(green):
    finding, record = tier1_record(green.base, note="use `logger.exception`")
    work = dataclasses.replace(green.work, tier1=(finding,))

    body = body_of(green, records=(*green.records, record), work=work)

    assert fix_lines(body)[-1].endswith(": use 'logger.exception'")


# --- redaction is checked, never redone over escapes (module docstring) ---------------

LIVE_PLACEHOLDER_LINK = re.compile(r"\[REDACTED\]\(")


@pytest.mark.parametrize(("note", "shown"), [
    ("Bearer abcdefgh12345678(https://evil.example)",
     f"Bearer &#91;REDACTED]({EVIL})"),
    ('token: abcdefgh12345678"(https://evil.example)',
     f"token: &#91;REDACTED]({EVIL})"),
    ("token=abcdefgh12345678", "token=&#91;REDACTED]"),
])
def test_a_redacted_note_keeps_its_escape_and_opens_no_link(green, note, shown):
    """Whole-body redaction read `token=\\[REDACTED]` as a fresh value and
    rewrote it to `token=[REDACTED]`, undoing plain_text's escape."""
    finding, record = tier1_record(green.base, note=note)
    work = dataclasses.replace(green.work, tier1=(finding,))

    body = body_of(green, records=(*green.records, record), work=work)

    assert fix_lines(body)[-1].endswith(f": {shown}")
    assert not LIVE_PLACEHOLDER_LINK.search(body)


@pytest.mark.parametrize("note", [
    "returns token: <str> unchanged",
    "logs session_token=<sid> per request",
    "prints secret: <class 'str'>",
    "token=[abcdef",
])
def test_a_placeholder_after_a_secret_word_does_not_refuse_the_pr(green, note):
    """The check reads the body as rendered: `&lt;sid>` is shown as `<sid>`,
    5 characters, under redaction's floor. Read escaped, the same note looked
    like a secret and refused every fix in the PR with it."""
    finding, record = tier1_record(green.base, note=note)
    work = dataclasses.replace(green.work, tier1=(finding,))

    body = body_of(green, records=(*green.records, record), work=work)

    assert PLACEHOLDER not in body


def test_a_backslash_before_a_bracket_opens_no_link(green, reduced):
    """`\\[` in the text: an escape written `\\[` would itself be escaped by
    that backslash and the link would be live; the entity cannot be."""
    body = secret_body(green, reduced, "tier1-note", r" x \[click](https://evil.example)")

    assert rf"\&#91;click]({EVIL})" in body
    assert "[click](" not in body


# --- autolinks and mentions are not live; the grade is escaped too ------------------

PING = "ping @octocat see https://evil.example and WWW.evil.example or me@corp.example"
PING_SHOWN = ("ping @&#8203;octocat see https&#58;//evil.example and WWW&#46;evil.example "
              "or me@&#8203;corp.example")


@pytest.mark.parametrize("where", ["tier1-note", "skip-reason"])
def test_a_mention_a_bare_url_or_an_email_in_agent_text_is_not_live(green, reduced, where):
    """GitHub autolinks a bare URL, a `www.` host and an e-mail address, and
    `@name` NOTIFIES that user: model text must do none of it. Each is
    written with an entity, and reads back as the original text."""
    body = secret_body(green, reduced, where, f" {PING}")

    for raw in ("@octocat", "https://", "WWW.evil", "@corp", "://"):
        assert raw not in body.split("\n", 1)[1]  # the marker line is the body's own
    assert PING_SHOWN in body
    assert PING in rendered(body)


def test_only_a_colon_before_two_slashes_is_escaped(green):
    """`token: x` and `a:b` read as written; `www` keeps its case."""
    body = body_of(green, skipped=(SkippedRemedy(
        "src/a.py", "r", "delete_file", "token: x, a:b, Www.x and awww.y"),))

    assert section(body, "Remedies skipped")[0].endswith(
        ": token: x, a:b, Www&#46;x and awww&#46;y")


@pytest.mark.parametrize(("note", "shown"), [
    ("token=ab://cd", "token=ab&#58;//cd"),
    ("token=ab@cd", "token=ab@&#8203;cd"),
    ("token=www.cd", "token=www&#46;cd"),
    ("token=<abcd", "token=&lt;abcd"),
    ("token=[abcd", "token=&#91;abcd"),
    ("token=ab&cd", "token=ab&amp;cd"),
    ("token=a#1", "token=a#&#8203;1"),
], ids=["colon", "at", "dot", "lt", "bracket", "amp", "hash"])
def test_every_entity_is_read_back_before_the_redaction_check(green, note, shown):
    """The value is under redaction's 8-character floor as a reader sees it,
    and over it as escaped (`ab&#58;//cd`): the check must decode every
    entity plain_text writes (sweep_pr._ENTITIES) or refuse an innocent
    PR."""
    assert redact(note) == note and redact(shown) != shown
    finding, record = tier1_record(green.base, note=note)
    work = dataclasses.replace(green.work, tier1=(finding,))

    body = body_of(green, records=(*green.records, record), work=work)

    assert fix_lines(body)[-1].endswith(f": {shown}")
    assert note in rendered(body)


@pytest.mark.parametrize("where", ["tier1-note", "skip-reason"])
@pytest.mark.parametrize(("text", "escaped"), [
    ("hi &#64;octocat", "hi &amp;#&#8203;64;octocat"),  # `#6`: a `#` before a digit
    ("hi &commat;octocat", "hi &amp;commat;octocat"),
    ("hi &#x40;octocat", "hi &amp;#x40;octocat"),
    ("me&#64;x.y", "me&amp;#&#8203;64;x.y"),
], ids=["dec", "named", "hex", "email"])
def test_an_entity_already_in_agent_text_cannot_decode_to_a_mention(green, reduced, where,
                                                                    text, escaped):
    """GitHub decodes entities before it looks for mentions and links, so
    `&` is escaped first: `&#64;octocat` is shown as that text, never as
    `@octocat`. html.unescape stands in for GitHub's decoding."""
    body = secret_body(green, reduced, where, f" {text}")

    assert escaped in body
    shown = html.unescape(body.split("\n", 1)[1])
    assert "@octocat" not in shown and "@x.y" not in shown
    assert text in rendered(body)


ISSUE_REFS = "see #123, org/repo#45 and GH-7 or gh-8, C# and #x; sha 0123abc"


@pytest.mark.parametrize("where", ["tier1-note", "skip-reason"])
def test_an_issue_or_pr_url_in_agent_text_is_not_live(green, reduced, where):
    """GitHub links `org/repo/issues/N` and an issue URL after decoding
    entities, so the `://` escape alone left them live."""
    text = " see https://github.com/cli/cli/issues/1 and cli/cli/pull/2 or a/b/discussions/3"
    body = secret_body(green, reduced, where, text)

    shown = html.unescape(body.split("\n", 1)[1])
    for raw in ("issues/1", "pull/2", "discussions/3"):
        assert raw not in shown
    assert text.strip() in rendered(body)


@pytest.mark.parametrize("where", ["tier1-note", "skip-reason"])
def test_an_issue_reference_in_agent_text_is_not_live(green, reduced, where):
    """`#123`, `org/repo#45` and `GH-7` would link (and backlink this PR
    from another repository's timeline); a `#` not before a digit and a
    commit SHA are left as they are."""
    body = secret_body(green, reduced, where, f" {ISSUE_REFS}")

    assert ("see #&#8203;123, org/repo#&#8203;45 and GH&#8203;-7 or gh&#8203;-8, C# and #x; "
            "sha 0123abc") in body
    shown = html.unescape(body.split("\n", 1)[1])
    for raw in ("#123", "#45", "GH-7", "gh-8"):
        assert raw not in shown
    assert ISSUE_REFS in rendered(body)


GRADE = ("B to A\n<h3>x</h3>\n<details>open\n[x](https://evil.example)\n"
         "thanks @octocat\nsee https://evil.example")


def test_the_grade_section_is_escaped_line_by_line(green):
    """grade_section is caller text: no HTML, link, autolink or mention, and
    its lines stay lines."""
    body = body_of(green, grade_section=GRADE)

    assert section(body, "Grade") == [
        "B to A", "&lt;h3>x&lt;/h3>", "&lt;details>open", f"&#91;x]({EVIL})",
        "thanks @&#8203;octocat", f"see {EVIL}"]
    grade = body.split("### Grade\n", 1)[1]
    for raw in ("<h3>", "<details>", "[x](", "@octocat", "https://"):
        assert raw not in grade
    assert rendered(grade) == GRADE + "\n"


@pytest.mark.parametrize("grade", ["### x", "B\n### x", "B\r\n#\tx", "B\r### x"],
                         ids=["first", "second", "crlf-tab", "lone-cr"])
def test_a_grade_heading_is_refused_before_it_is_escaped(green, grade):
    """The heading check sees the grade as given: escaping turns `#` then a
    tab into `#\\t`, no heading, but such a grade broke E7-R5's contract
    all the same."""
    with pytest.raises(SweepBodyError, match="headings"):
        body_of(green, grade_section=grade)


def test_a_refused_grade_heading_does_not_quote_a_secret(green):
    """The error is recorded (cm-fkd.14), and the grade is not redacted yet
    when its headings are checked: the error names line numbers only."""
    with pytest.raises(SweepBodyError, match="headings") as error:
        body_of(green, grade_section=f"B\n# token={TOKEN}")

    assert TOKEN not in str(error.value) and "ghp_" not in str(error.value)
    assert "[2]" in str(error.value)


@pytest.mark.parametrize("grade", ["A\n===", "B\n---", "x\n  -  ", "y\nz\n=\t"],
                         ids=["h1", "h2", "indented-dash", "tab"])
def test_a_setext_grade_heading_is_refused(green, grade):
    """A `===` or `---` line under a non-blank line makes it a heading no
    `#` check sees."""
    with pytest.raises(SweepBodyError, match="headings"):
        body_of(green, grade_section=grade)


def test_a_rule_or_a_list_in_the_grade_is_not_a_heading(green):
    """`---` after a blank line is a thematic break, and `- x` a list item:
    neither makes a heading, so neither is refused."""
    body = body_of(green, grade_section="A\n\n---\n- x\nB = C")

    assert section(body, "Grade") == ["A", "---", "- x", "B = C"]


def test_a_marker_word_or_comment_in_an_escaped_grade_is_still_neutralised(green):
    body = body_of(green, grade_section="curamoir-sweep\n<!-- x")

    assert parse_sweep_marker(body) == green.base
    assert body.count("curamoir-sweep") == 1
    assert section(body, "Grade") == ["curamoir\u2011sweep", "&lt;!-- x"]


def test_a_bracketed_route_in_a_skip_reason_passes(green):
    """Next.js-style paths are common in skip reasons; no secret word sits
    before them, so the check has nothing to refuse."""
    body = body_of(green, skipped=(SkippedRemedy(
        "src/a.py", "r", "delete_file", "used by app/[slug]/page.tsx and pages/[id].tsx"),))

    assert "app/&#91;slug]/page.tsx and pages/&#91;id].tsx" in body


def test_text_an_escape_makes_look_like_a_secret_is_refused_not_rewritten(green):
    """`ab\x01cd` is under redaction's 8-character floor; escaped it is
    `ab\\x01cd`, over it. Redacting the body again turned it into the live
    link `token=[REDACTED](https://evil.example)`; the check refuses."""
    finding, record = tier1_record(green.base, note='token=ab\x01cd"(https://evil.example)')
    work = dataclasses.replace(green.work, tier1=(finding,))

    with pytest.raises(SweepBodyError, match="redaction would still change"):
        body_of(green, records=(*green.records, record), work=work)


def test_a_secret_in_the_grade_section_is_redacted(green):
    body = body_of(green, grade_section=f"B\nuses {TOKEN}\napi_key = 'abcdefgh1234'")

    assert TOKEN not in body and "abcdefgh1234" not in body
    shown = "&#91;" + PLACEHOLDER[1:]
    assert section(body, "Grade") == ["B", f"uses {shown}", f"api_key = {shown}"]


# --- headings, escapes, Unicode, sizes ------------------------------------------------

@pytest.mark.parametrize("grade", ["  ### x", "#\tx", "#", "B\n   ## x"],
                         ids=["indented", "tab", "bare", "indented-3"])
def test_a_grade_heading_cmark_reads_is_refused(green, grade):
    """CommonMark reads a heading indented up to three spaces, one whose `#`
    run is followed by a tab, and a bare `#`."""
    with pytest.raises(SweepBodyError, match="headings"):
        body_of(green, grade_section=grade)


def test_a_tab_a_control_and_a_line_separator_in_a_path_are_shown_escaped(green):
    body = body_of(green, left_out=("src/a\tb\x01c\u2028d.py",))

    assert "\t" not in body and "\x01" not in body and "\u2028" not in body
    assert section(body, "Files left for the next run") == [
        "- `src/a\\tb\\x01c\\u2028d.py` (past `max_files_per_pr`)"]


@pytest.mark.parametrize("record_form,gate_form", [("NFD", "NFC"), ("NFC", "NFD")])
def test_a_path_is_matched_whatever_its_unicode_form(green, record_form, gate_form):
    name = "src/caf\u00e9.py"
    record = dataclasses.replace(green.records[0],
                                 path=unicodedata.normalize(record_form, name))
    gate = green_gate([unicodedata.normalize(gate_form, name)])
    assert record.path != gate.kept[0]

    assert pr_records([record], gate) == (record,)


def test_the_cap_counts_utf8_bytes_not_characters(green):
    """Each emoji is one character and four UTF-8 bytes: a cap that counted
    characters would keep about four times too many of these lines."""
    assert github_size("\U0001f600") == 4
    left_out = tuple(f"src/{chr(0x1F600 + n % 64) * 40}_{n:04d}.py" for n in range(1000))

    body = body_of(green, left_out=left_out)

    assert len(body.encode("utf-8")) + body.count("\n") <= MAX_BODY_SIZE
    assert len(body) < MAX_BODY_SIZE // 2
    assert section(body, "Files left for the next run")[-1].startswith("- ... and ")


def section_text(body, heading):
    """The one paragraph under `## heading`."""
    lines = body.splitlines()
    return lines[lines.index(f"## {heading}") + 2]


# --- builders --------------------------------------------------------------------------

def violation(number):
    path = f"src/pkg/module_{number:04d}.py"
    return ReportedViolation(
        path=path, line=10, col=1, end_line=10, end_col=20, severity="error",
        rule_id="no-bare-print", principle_id="structured-logging",
        principle_rule="log through the logger", message="print", fix="use logging",
        fix_source="rule", why_link="", match="print(x)", fingerprint=f"fp{number:06d}")


def work_of(items):
    return WorkList(items=tuple(items), report=None)


def green_gate(kept):
    return GateResult(strategy=GREEN, cause=None, command="true", command_source="config",
                      kept=tuple(kept), dropped=(), runs=())


# --- files put back for an edit outside E3-R2's scope (cm-9ab) -------------------------

def test_scope_dropped_paths_are_left_for_the_next_run_one_line_per_path_and_kind(green):
    from curamoir.fix_scope import FILE_ADDED, FILE_NOT_IN_WORK_LIST, OUTSIDE_VIOLATION, Breach

    dropped = (
        Breach(OUTSIDE_VIOLATION, "src/b.py", 3, "edit far from the violation"),
        Breach(FILE_NOT_IN_WORK_LIST, "src/b.py", None, "not in the work list"),
        Breach(OUTSIDE_VIOLATION, "src/b.py", 9, "a second, same kind"),
        Breach(FILE_ADDED, "src/a`new.py", None, "added"),
        Breach("<kind> [x](y)", "src/c.py", None, "odd kind"),
    )

    body = body_of(green, scope_dropped=dropped)

    assert section(body, "Files left for the next run") == [
        "- ``src/a`new.py`` (removed: added outside E3-R2's scope)",
        "- `src/b.py` (dropped: an edit outside E3-R2's scope, file_not_in_work_list)",
        "- `src/b.py` (dropped: an edit outside E3-R2's scope, outside_violation)",
        "- `src/c.py` (dropped: an edit outside E3-R2's scope, &lt;kind> &#91;x](y))"]
