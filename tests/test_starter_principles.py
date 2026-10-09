"""E2-R2: the starter set `/curamoir:init` seeds into a repository.

This is content, so most of it cannot be tested — whether a principle is worth
holding is a judgement. What can be tested is everything that makes it usable:
that it parses, that every field E2-R1 requires is populated, that the ids the
bundled fixture and its rules cite are present, and that it ships at the tier
the ladder starts from.

The last point matters more than it looks. A starter set shipped at `rule` or
`guard` would enforce, on day one, opinions the repository's owner has not
agreed to — which is the failure mode the file's own preamble warns about. E4
promotes on evidence; nothing arrives pre-promoted.
"""

from pathlib import Path

import pytest

from curamoir.principles import REQUIRED_FIELDS, parse_principles

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "principles.md"
STARTER = parse_principles(TEMPLATE.read_text(encoding="utf-8"))


def test_the_starter_set_is_the_size_e2_r2_asks_for():
    assert 8 <= len(STARTER.principles) <= 12, len(STARTER.principles)


def test_the_template_round_trips_so_a_promotion_cannot_reformat_it():
    original = TEMPLATE.read_text(encoding="utf-8")

    from curamoir.principles import serialise_principles

    assert serialise_principles(parse_principles(original)) == original


@pytest.mark.parametrize("principle", STARTER.principles, ids=lambda p: p.id)
def test_every_field_e2_r1_requires_is_populated(principle):
    for name in REQUIRED_FIELDS:
        value = getattr(principle, name)
        if name == "promoted_on":
            continue
        assert value and str(value).strip(), f"{principle.id} has an empty {name}"


@pytest.mark.parametrize("principle", STARTER.principles, ids=lambda p: p.id)
def test_everything_ships_unpromoted(principle):
    """The ladder starts at `principle`, and E4 promotes on evidence. A starter
    set that arrived at `rule` would enforce opinions the repo never agreed to."""
    assert principle.tier == "principle"
    assert principle.promoted_on is None


@pytest.mark.parametrize("principle", STARTER.principles, ids=lambda p: p.id)
def test_the_rule_is_one_imperative_sentence(principle):
    """E2-R1 says "one sentence, imperative". A rule that needs a paragraph is
    two principles, and a rule phrased as an observation cannot be enforced."""
    rule = principle.rule
    assert rule.endswith("."), f"{principle.id}: not a sentence"
    assert rule.count(".") == 1, f"{principle.id}: more than one sentence"
    assert len(rule.split()) <= 25, f"{principle.id}: {len(rule.split())} words"


@pytest.mark.parametrize("principle", STARTER.principles, ids=lambda p: p.id)
def test_every_principle_says_where_it_came_from(principle):
    """Operating assumption 5 is "extraction, not invention", and this set is
    spec-derived rather than extracted from a real codebase's review history.
    Saying so per principle is what makes that reviewable instead of implied."""
    assert principle.extra.get("source"), f"{principle.id} cites no source"


@pytest.mark.parametrize("principle", STARTER.principles, ids=lambda p: p.id)
def test_the_why_is_strong_enough_to_justify_becoming_a_guard(principle):
    """The bead's own note: "each needs a `why` strong enough to justify
    eventually becoming a guard that blocks edits". A one-line why cannot carry
    that, because a guard is the most intrusive thing this system does."""
    assert len(principle.why.split()) >= 30, (
        f"{principle.id}: {len(principle.why.split())} words of justification"
    )


def test_the_set_covers_every_example_the_spec_names():
    """E2-R2 names six by example and calls them "the floor, not the set"."""
    # The extracted ids, which are not always the spec's wording: the spec's
    # "no config checks deep in call chains" is the source's broader "validate
    # at the boundary", and its "one implementation per concept" is the
    # source's "shared utilities over hand-rolled helpers".
    covered = {
        "structured-logging",
        "errors-carry-context",
        "no-file-variant-proliferation",
        "validate-at-the-boundary",
        "shared-utilities",
        "tests-assert-behaviour",
    }
    ids = {p.id for p in STARTER.principles}

    assert covered <= ids, f"missing: {sorted(covered - ids)}"


def test_the_fixture_cites_only_ids_the_starter_set_defines():
    """The fixture's `principles.md` is a three-principle subset used by E3's
    acceptance test. If the starter set renamed one of them, the fixture's
    planted violations would cite an id that resolves to nothing — the
    self-confirming test the provenance pass warned about."""
    fixture = parse_principles(
        (TEMPLATE.parents[1] / "fixtures" / "sample-repo" / ".curamoir" / "principles.md")
        .read_text(encoding="utf-8")
    )

    starter_ids = {p.id for p in STARTER.principles}
    fixture_ids = {p.id for p in fixture.principles}

    assert fixture_ids <= starter_ids, f"fixture-only ids: {sorted(fixture_ids - starter_ids)}"


def test_init_seeds_the_starter_set_rather_than_a_placeholder(tmp_path):
    """`templates/principles.md` did not exist, so `_copy_template`'s fallback
    was the only branch that ever ran and every init wrote "No principles yet"."""
    from curamoir.scaffold import init_repo

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)

    seeded = parse_principles(
        (tmp_path / ".curamoir" / "principles.md").read_text(encoding="utf-8")
    )
    assert len(seeded.principles) == len(STARTER.principles)


def test_doctor_counts_the_seeded_principles(tmp_path):
    """What a developer sees after `/curamoir:init`: the count is the signal
    that the file parsed, and it was previously always zero."""
    from curamoir.doctor import diagnose
    from curamoir.scaffold import init_repo

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)

    findings = [f for f in diagnose(tmp_path) if f.check == "principles"]

    assert findings, "doctor does not report on principles at all"
    assert str(len(STARTER.principles)) in findings[0].detail
