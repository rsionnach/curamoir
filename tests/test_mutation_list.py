"""gate/mutation_list.py (cm-8ap.19): the brief's mutation list, read and applied.

Spec §11 "Mutation block" and R5-R30/R5-R31; the user's rulings of
2026-11-24 (one block, content as CommonMark defines it, unknown keys
refused, `behavior` named as `behaviour`, an optional `behaviour`, no block a
refusal); d-cm-8ap.19-6..9 for the region and the operators. The fenced-block
cases follow CommonMark's fenced code block rules (spec 0.31.2, section 4.5),
not this module: each case names the rule it pins.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from curamoir.gate import mutation_list as m
from curamoir.gate.mutation_list import MutationListError, Stale

ENTRY = {"path": "src/a.py", "locate": {"lines": [1, 2]}, "mutation": "invert_condition",
         "expect_red": "pytest -q tests/test_a.py"}


def entries(*raw):
    return m.parse_entries(yaml.safe_dump(list(raw)))


def entry(**change):
    return entries(dict(ENTRY, **change))[0]


# --- the block (CommonMark 4.5) ------------------------------------------------

def test_the_blocks_content_is_extracted_verbatim():
    brief = "Intro.\n\n```r5-mutations\n- path: a.py\n  x: 'y  '\n\n```\n\nAfter.\n"

    assert m.extract_block(brief) == "- path: a.py\n  x: 'y  '\n\n"


@pytest.mark.parametrize(("brief", "content"), [
    # A tilde fence; the info string's first word decides.
    ("~~~ r5-mutations extra words\nA\n~~~\n", "A\n"),
    # The closing fence must be at least as long as the opening one.
    ("````r5-mutations\n```\nA\n````\n", "```\nA\n"),
    # Up to the opening fence's indentation is removed from each line.
    ("  ```r5-mutations\n    A\n B\nC\n  ```\n", "  A\nB\nC\n"),
    # An unclosed fence runs to the end of the document.
    ("```r5-mutations\nA\nB", "A\nB"),
    # A closing fence may have trailing spaces, and up to three of indent.
    ("```r5-mutations\nA\n   ```   \n", "A\n"),
])
def test_commonmark_fences(brief, content):
    assert m.extract_block(brief) == content


@pytest.mark.parametrize(("brief", "why"), [
    ("no block here\n", "has no fenced"),
    ("```python\nx = 1\n```\n", "has no fenced"),
    # Four spaces of indent is an indented code block, not a fence.
    ("    ```r5-mutations\n    A\n    ```\n", "has no fenced"),
    # A backtick fence's info string may not contain a backtick.
    ("```r5-mutations `x`\nA\n```\n", "has no fenced"),
    ("```r5-mutationsx\nA\n```\n", "has no fenced"),
    ("```r5-mutations\nA\n```\n\n```r5-mutations\nB\n```\n", "2 `r5-mutations` blocks"),
])
def test_no_block_or_two_is_refused(brief, why):
    with pytest.raises(MutationListError, match=why):
        m.extract_block(brief)


# --- the entries ------------------------------------------------------------------

def test_an_entry_with_every_field():
    parsed = entry(locate={"anchor": "def f("}, behaviour="f refuses an empty list",
                   mutation={"replace": {"from": "list", "to": "dict"}})

    assert (parsed.index, parsed.path, parsed.locate, parsed.mutation, parsed.behaviour) == \
        (1, "src/a.py", ("anchor", "def f("), ("replace", "list", "dict"),
         "f refuses an empty list")
    assert parsed.describe_mutation() == "replace 'list' with 'dict'"
    assert parsed.describe_locate() == "anchor 'def f('"


@pytest.mark.parametrize(("raw", "why"), [
    ("[]", "non-empty list"),
    ("{}", "non-empty list"),
    ("- [1]", "entry 1 is not a mapping"),
    ("key: [unclosed", "not valid YAML"),
])
def test_a_block_that_is_not_a_list_of_entries_is_refused(raw, why):
    with pytest.raises(MutationListError, match=why):
        m.parse_entries(raw)


@pytest.mark.parametrize(("change", "why"), [
    ({"behavior": "x"}, "entry 1 has `behavior`; the field is `behaviour`"),
    ({"expect_green": "x"}, "`expect_green`, which is not a field"),
    ({"path": "/abs/a.py"}, "not a plain repo-relative path"),
    ({"path": "src/../a.py"}, "not a plain repo-relative path"),
    ({"path": "a.txt"}, "a.txt is not Python; use `replace`"),
    ({"locate": {"lines": [3, 2]}}, "first <= last"),
    ({"locate": {"lines": [0, 2]}}, "1-based"),
    ({"locate": {"lines": [1, True]}}, "1-based"),
    ({"locate": {"anchor": ""}}, "one line of text"),
    ({"locate": {"anchor": "a\nb"}}, "one line of text"),
    ({"locate": {"lines": [1, 2], "anchor": "x"}}, "locate is"),
    ({"locate": "line 3"}, "locate is"),
    ({"mutation": "invert"}, "is not one of"),
    ({"mutation": {"replace": {"from": "a", "to": "a"}}}, "different from to"),
    ({"mutation": {"replace": {"from": "", "to": "a"}}}, "from non-empty"),
    ({"expect_red": " "}, "expect_red is the test command"),
    ({"behaviour": ""}, "behaviour is non-empty text"),
])
def test_an_entry_the_contract_does_not_allow_is_refused(change, why):
    with pytest.raises(MutationListError, match=why):
        entry(**change)


@pytest.mark.parametrize("field", ["path", "locate", "mutation", "expect_red"])
def test_every_required_field_is_required(field):
    raw = {k: v for k, v in ENTRY.items() if k != field}

    with pytest.raises(MutationListError, match=f"missing {field}"):
        entries(raw)


def test_a_text_file_takes_a_replace():
    assert entry(path="docs/a.md", mutation={"replace": {"from": "a", "to": "b"}}).path == \
        "docs/a.md"


def test_entries_are_numbered_from_one_and_the_error_names_which():
    with pytest.raises(MutationListError, match="entry 2 is missing"):
        entries(ENTRY, {"path": "b.py"})


# --- the region (d-cm-8ap.19-6) ------------------------------------------------

SOURCE = textwrap.dedent('''\
    import os


    def classify(spec):
        if spec.get("slos") and os.sep:
            return "gate"
        return "api"


    def other():
        return total(1, 2) == 3 + 4
''')


def test_lines_are_an_inclusive_one_based_range():
    assert m.region(SOURCE, entry(locate={"lines": [5, 6]})) == (5, 6)


def test_lines_past_the_end_are_stale():
    with pytest.raises(Stale, match="run past the end"):
        m.region(SOURCE, entry(locate={"lines": [10, 12]}))


def test_an_anchor_takes_the_statement_it_starts():
    assert m.region(SOURCE, entry(locate={"anchor": "def classify("})) == (4, 7)


def test_an_anchor_on_a_line_inside_a_statement_takes_that_line():
    assert m.region(SOURCE, entry(locate={"anchor": 'return "gate"'})) == (6, 6)


@pytest.mark.parametrize("anchor", ["def missing(", "return"])
def test_an_anchor_on_no_line_or_several_is_stale(anchor):
    with pytest.raises(Stale, match=r"lines of src/a\.py, not one"):
        m.region(SOURCE, entry(locate={"anchor": anchor}))


# --- the operators (d-cm-8ap.19-7..9) -----------------------------------------

@pytest.mark.parametrize(("mutation", "locate", "line", "becomes"), [
    ("invert_condition", [4, 7], 5, '    if not (spec.get("slos") and os.sep):'),
    ("drop_argument", [4, 7], 5, '    if spec.get() and os.sep:'),
    ("return_constant", [4, 7], 6, "        return None"),
    ("delete_statement", [6, 7], 6, "        pass"),
    ("swap_operator", [4, 7], 5, '    if (spec.get("slos")) or (os.sep):'),
    ("swap_operator", [11, 11], 11, "    return (total(1, 2)) != (3 + 4)"),
    ("drop_argument", [11, 11], 11, "    return total(1) == 3 + 4"),
])
def test_each_operator_changes_the_first_matching_node_and_nothing_else(mutation, locate, line,
                                                                       becomes):
    mutated = m.mutate(SOURCE, entry(mutation=mutation), locate[0], locate[1])

    before, after = SOURCE.splitlines(), mutated.splitlines()
    assert after[line - 1] == becomes
    assert [a for n, a in enumerate(after) if n != line - 1] == \
        [b for n, b in enumerate(before) if n != line - 1]


def test_replace_changes_its_text_once_inside_the_region():
    mutated = m.mutate(SOURCE, entry(mutation={"replace": {"from": '"gate"', "to": '"api"'}}),
                       4, 7)

    assert mutated.splitlines()[5] == '        return "api"'


@pytest.mark.parametrize(("mutation", "locate", "why"), [
    ("return_constant", [1, 1], "finds nothing to act on"),
    ("invert_condition", [10, 11], "finds nothing to act on"),
    ({"replace": {"from": "return", "to": "yield"}}, [4, 7], "occurs 2 times"),
    ({"replace": {"from": "absent", "to": "x"}}, [4, 7], "occurs 0 times"),
])
def test_a_mutation_with_nothing_to_act_on_is_stale(mutation, locate, why):
    with pytest.raises(Stale, match=why):
        m.mutate(SOURCE, entry(mutation=mutation), locate[0], locate[1])


def test_a_file_that_does_not_parse_is_stale():
    with pytest.raises(Stale, match="does not parse"):
        m.mutate("def broken(:\n", entry(), 1, 1)


def test_a_non_ascii_line_is_cut_at_the_right_character():
    source = 'x = "é" + "ü" == "ö"\n'

    mutated = m.mutate(source, entry(mutation="swap_operator"), 1, 1)

    # The comparison is the first node at that position (it holds the sum),
    # and its slice ends after the multi-byte characters, not inside them.
    assert mutated == 'x = ("é" + "ü") != ("ö")\n'



# --- correctness review, round 1 ------------------------------------------------

def test_a_swapped_comparison_keeps_its_operands_precedence():
    mutated = m.mutate("def f(a, b, c):\n    return (a or b) == c\n",
                       entry(mutation="swap_operator"), 1, 2)

    assert mutated.splitlines()[1] == "    return (a or b) != (c)"
    namespace = {}
    exec(mutated, namespace)
    # (0 or 1) != 1 is False: the swap, not `a or (b != c)`, which is True.
    assert namespace["f"](0, 1, 1) is False


def test_an_anchor_on_a_decorated_defs_own_line_takes_the_function():
    source = "class K:\n    @staticmethod\n    def classify(x):\n        if x:\n" \
             "            return 1\n        return 2\n"

    # From the anchored def line to the function's end (the decorator above
    # holds nothing an operator acts on).
    assert m.region(source, entry(locate={"anchor": "def classify("})) == (3, 6)
    assert m.mutate(source, entry(locate={"anchor": "def classify("}), 3, 6) \
        .splitlines()[3] == "        if not (x):"


@pytest.mark.parametrize("separator", ["\x0c", "\u2028"])
def test_lines_are_counted_as_python_counts_them(separator):
    source = f"x = 1  # a{separator}comment\ndef f(a):\n    return a == 1\n"

    mutated = m.mutate(source, entry(locate={"lines": [3, 3]}, mutation="swap_operator"), 3, 3)

    assert mutated.split("\n")[2] == "    return (a) != (1)"
    assert m.region(source, entry(locate={"anchor": "return a == 1"})) == (3, 3)
