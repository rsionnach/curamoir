"""E2-R1: the principles file's structure, and the parser for it.

Two constraints pull against each other, and the design follows from taking
both seriously:

- `principles.md` is **human-authored**. Someone edits it by hand, so the format
  has to tolerate the things hands do — a wrapped sentence, a blank line, an
  extra field, prose above the first principle.
- `/curamoir:promote` and `/curamoir:demote` **rewrite** it (E4-R6), and E2-R1
  requires that parsing and re-serialising be byte-identical.

A parse-then-re-emit design cannot honour the second: re-emitting normalises,
and normalising a hand-edited file is a diff nobody asked for. So each principle
keeps the raw text it was parsed from, and `serialise` reassembles those spans.
Byte-identical then holds by construction rather than by careful formatting, and
a promotion edits one line inside one span and leaves every other byte alone.

The `id` is a stable reference: rules cite it (E2-R3), the ledger counts fixes
against it (E3-R5), findings carry it (E5-R1). So this file is also where "two
principles with one id" and "the heading disagrees with the id field" have to be
caught, because every consumer downstream resolves an id and trusts the answer.
"""

from pathlib import Path

import pytest

from curamoir.principles import (
    PERMISSIONS,
    PrincipleError,
    load_principles,
    parse_principles,
    serialise_principles,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample-repo" / ".curamoir" / "principles.md"

ONE = """\
# Golden principles

## structured-logging

- **id:** `structured-logging`
- **title:** Structured logging only
- **rule:** Emit diagnostics through the module logger, never through `print`.
- **why:** A print has no level and no logger name, so it cannot be filtered.
- **good:** `logger.info("done")`
- **bad:** `print("done")`
- **tier:** principle
- **promoted_on:** null
"""

REQUIRED = ("id", "title", "rule", "why", "good", "bad", "tier", "promoted_on")


# --- the shape E2-R1 specifies -------------------------------------------

def test_every_required_field_is_parsed():
    principle = parse_principles(ONE).principles[0]

    assert principle.id == "structured-logging"
    assert principle.title == "Structured logging only"
    assert principle.rule.startswith("Emit diagnostics")
    assert principle.why
    assert principle.good == '`logger.info("done")`'
    assert principle.bad == '`print("done")`'
    assert principle.tier == "principle"
    assert principle.promoted_on is None


@pytest.mark.parametrize("field", REQUIRED)
def test_a_missing_field_fails_validation_naming_the_field(field):
    """E2-R1's acceptance criterion, verbatim: the message names the field.

    An error that says only "invalid principle" sends the author back to read
    the spec; one that names the field is a one-line fix.
    """
    text = "\n".join(
        line for line in ONE.splitlines() if not line.startswith(f"- **{field}:**")
    ) + "\n"

    with pytest.raises(PrincipleError) as caught:
        parse_principles(text)

    assert field in str(caught.value)
    assert "structured-logging" in str(caught.value), "name the principle too"


# --- round-trip, the criterion that drives the design --------------------

def test_the_bundled_fixture_round_trips_byte_identical():
    original = FIXTURE.read_text(encoding="utf-8")

    assert serialise_principles(parse_principles(original)) == original


@pytest.mark.parametrize("text, label", [
    pytest.param(ONE, "the minimal file", id="minimal"),
    pytest.param(ONE.replace("\n## ", "\n\n\n## "), "extra blank lines", id="loose_spacing"),
    pytest.param(ONE + "\n<!-- a trailing comment -->\n", "a trailing comment", id="trailing_prose"),
    pytest.param(ONE.replace("- **tier:** principle", "-   **tier:**   principle"),
                 "irregular spacing inside a field", id="irregular_spacing"),
    pytest.param(ONE.rstrip("\n"), "no trailing newline", id="no_final_newline"),
    pytest.param(
        "## a\n\n"
        + "\n".join(
            f"- **{f}:** " + {"id": "a", "tier": "principle", "promoted_on": "null"}.get(f, "x")
            for f in REQUIRED
        )
        + "\n",
        "no preamble at all", id="no_preamble"),
])
def test_a_hand_edited_file_round_trips_byte_identical(text, label):
    """Hands do all of these, and a rewrite that normalises them is a diff the
    author did not ask for — in a file they own."""
    assert serialise_principles(parse_principles(text)) == text, label


def test_a_promotion_rewrites_one_field_and_leaves_every_other_byte_alone():
    """What round-trip fidelity is FOR. `/curamoir:promote` changes `tier` and
    `promoted_on`; everything else in the file must be untouched."""
    parsed = parse_principles(ONE)

    promoted = parsed.with_field("structured-logging", "tier", "rule")
    promoted = promoted.with_field("structured-logging", "promoted_on", "2026-10-09")
    rendered = serialise_principles(promoted)

    assert "- **tier:** rule\n" in rendered
    assert "- **promoted_on:** 2026-10-09\n" in rendered
    # Every line that was not the two edited ones survives unchanged.
    before = [line for line in ONE.splitlines()
              if not line.startswith(("- **tier:**", "- **promoted_on:**"))]
    after = [line for line in rendered.splitlines()
             if not line.startswith(("- **tier:**", "- **promoted_on:**"))]
    assert before == after


def test_editing_an_unknown_principle_is_an_error_not_a_silent_no_op():
    with pytest.raises(PrincipleError, match="no-such-principle"):
        parse_principles(ONE).with_field("no-such-principle", "tier", "rule")


# --- the id is a reference other epics resolve ---------------------------

def test_two_principles_with_the_same_id_are_refused():
    """Rules cite an id (E2-R3), the ledger counts fixes against it (E3-R5) and
    findings carry it (E5-R1). Two definitions for one id makes every one of
    those lookups arbitrary."""
    text = ONE + "\n" + ONE.split("# Golden principles\n", 1)[1]

    with pytest.raises(PrincipleError, match="structured-logging"):
        parse_principles(text)


def test_a_heading_that_disagrees_with_the_id_field_is_refused():
    """The heading is the anchor E2-R4 links to; the field is what rules cite.
    If they differ, a finding's link and its id point at different things."""
    text = ONE.replace("## structured-logging", "## structured-loggin")

    with pytest.raises(PrincipleError) as caught:
        parse_principles(text)

    assert "structured-loggin" in str(caught.value)


# --- the two constrained fields ------------------------------------------

@pytest.mark.parametrize("tier", ["principle", "rule", "guard"])
def test_the_three_tiers_are_accepted(tier):
    assert parse_principles(ONE.replace("- **tier:** principle", f"- **tier:** {tier}"))


def test_an_unknown_tier_is_refused_and_lists_the_allowed_ones():
    with pytest.raises(PrincipleError) as caught:
        parse_principles(ONE.replace("- **tier:** principle", "- **tier:** suggestion"))

    message = str(caught.value)
    assert "suggestion" in message
    assert all(allowed in message for allowed in ("principle", "rule", "guard"))


@pytest.mark.parametrize("value, expected", [
    pytest.param("null", None, id="null"),
    pytest.param("2026-10-09", "2026-10-09", id="an_iso_date"),
])
def test_promoted_on_accepts_a_date_or_null(value, expected):
    text = ONE.replace("- **promoted_on:** null", f"- **promoted_on:** {value}")

    assert parse_principles(text).principles[0].promoted_on == expected


@pytest.mark.parametrize("value", ["yesterday", "09/10/2026", "2026-13-01"])
def test_a_promoted_on_that_is_not_a_date_is_refused(value):
    """E4 computes promotion windows from this field, so an unparseable date is
    a silently wrong window rather than a formatting nit."""
    text = ONE.replace("- **promoted_on:** null", f"- **promoted_on:** {value}")

    with pytest.raises(PrincipleError, match="promoted_on"):
        parse_principles(text)


# --- the things hands do -------------------------------------------------

def test_a_wrapped_field_value_is_joined():
    """The fixture wraps `why` and `rule` across lines, because it is prose
    someone has to read in an editor."""
    text = ONE.replace(
        "- **why:** A print has no level and no logger name, so it cannot be filtered.",
        "- **why:** A print has no level and no logger name,\n  so it cannot be filtered.",
    )

    principle = parse_principles(text).principles[0]

    assert principle.why == "A print has no level and no logger name, so it cannot be filtered."
    assert serialise_principles(parse_principles(text)) == text


def test_an_unrecognised_field_is_kept_rather_than_rejected():
    """The fixture carries a `source` field that E2-R1 does not require. The
    file is human-authored, so an extra field is the author's business — but it
    must survive a promotion rewrite."""
    text = ONE + "- **source:** docs/curamoir-spec.md E2-R2\n"

    parsed = parse_principles(text)

    assert parsed.principles[0].extra["source"] == "docs/curamoir-spec.md E2-R2"
    assert serialise_principles(parsed) == text


def test_a_file_with_no_principles_parses_as_empty_rather_than_failing():
    """What `/curamoir:init` writes before Epic 2 ships a starter set, and what
    doctor counts. An error here would make a fresh repo look broken."""
    parsed = parse_principles("# Golden principles\n\nNo principles yet.\n")

    assert list(parsed.principles) == []
    assert serialise_principles(parsed) == "# Golden principles\n\nNo principles yet.\n"


def test_loading_a_missing_file_is_empty_not_an_error(tmp_path):
    assert list(load_principles(tmp_path).principles) == []


def test_loading_reads_the_repo_path_a_consumer_would_pass(tmp_path):
    base = tmp_path / ".curamoir"
    base.mkdir()
    (base / "principles.md").write_text(ONE, encoding="utf-8")

    assert [p.id for p in load_principles(tmp_path).principles] == ["structured-logging"]


def test_the_fixture_defines_every_principle_its_planted_violations_cite():
    """The provenance pass's point: `PLANTED.json` tags each violation with a
    principle id, and an id that resolves to nothing makes E3's acceptance test
    self-confirming."""
    import json

    planted = json.loads(
        (FIXTURE.parents[1] / "PLANTED.json").read_text(encoding="utf-8")
    )
    defined = {p.id for p in parse_principles(FIXTURE.read_text(encoding="utf-8")).principles}

    cited = {
        entry["principle_id"]
        for entry in (planted if isinstance(planted, list) else planted.get("violations", []))
        if isinstance(entry, dict) and entry.get("principle_id")
    }
    assert cited, "the fixture cites no principle ids, so this test proves nothing"
    assert cited <= defined, f"cited but undefined: {sorted(cited - defined)}"


def test_ids_whose_anchors_collide_are_refused():
    """E2-R4 links each violation to `principles.md#<anchor>`, and a renderer
    lowercases headings: `no-print` and `No-Print` share one anchor, so one
    principle's violations would link to the other's `why`."""
    second = ONE.replace("structured-logging", "Structured-Logging").split("\n", 2)[2]

    with pytest.raises(PrincipleError, match="anchor"):
        parse_principles(ONE + "\n" + second)


# --- `permits`: the one optional field cúramóir reads (E3-R2) ------------------
#
# E3-R2 forbids a sweep fix that changes a signature "without an explicit
# principle permitting it", so the format must be able to say so. E2-R1 names
# eight required fields and says nothing about others (unknown ones are kept,
# above), so an optional field is within it; what makes `permits` different
# from `source` is that cúramóir ACTS on it, so its values are a closed
# vocabulary and a typo is refused rather than read as "no permission".

def test_a_principle_permits_nothing_unless_it_says_so():
    assert parse_principles(ONE).principles[0].permits == ()


@pytest.mark.parametrize("value", ["`signature_change`", "signature_change",
                                   " `signature_change` "])
def test_permits_names_what_the_principle_allows(value):
    text = ONE + f"- **permits:** {value}\n"

    parsed = parse_principles(text)

    assert parsed.principles[0].permits == ("signature_change",)
    assert "permits" not in parsed.principles[0].extra
    assert serialise_principles(parsed) == text


def test_permits_takes_a_comma_separated_list_and_keeps_each_once():
    text = ONE + "- **permits:** `signature_change`, `signature_change`\n"

    assert parse_principles(text).principles[0].permits == ("signature_change",)


@pytest.mark.parametrize("value", ["`signature-change`", "signatures", "`signature_change`, x"])
def test_an_unknown_permission_is_refused_naming_the_allowed_ones(value):
    """A misspelt permission read as "permits nothing" would quietly reject
    every fix the author meant to allow; read as "permits everything" it would
    quietly allow what E3-R2 forbids. Neither is acceptable, so it is an error."""
    with pytest.raises(PrincipleError) as refused:
        parse_principles(ONE + f"- **permits:** {value}\n")

    message = str(refused.value)
    assert "structured-logging" in message and "permits" in message
    for allowed in PERMISSIONS:
        assert allowed in message


def test_an_empty_permits_is_refused_rather_than_read_as_none():
    with pytest.raises(PrincipleError, match="permits"):
        parse_principles(ONE + "- **permits:**\n")


def test_a_promotion_keeps_the_permission():
    text = ONE + "- **permits:** `signature_change`\n"

    promoted = parse_principles(text).with_field("structured-logging", "tier", "rule")

    assert promoted.principles[0].permits == ("signature_change",)
    assert promoted.principles[0].tier == "rule"
