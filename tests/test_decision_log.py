"""cm-rp1.1: the decision-log trial's record schema and its append snippet.

Phase 1 has no code of its own: `.curamoir/decisions.md` is the schema of
`.curamoir/decisions.jsonl`, and `.claude/skills/decision-log/SKILL.md` gives
the agent a one-off script (the `bash` block) that checks a record and
appends it. These tests take both from the files as they stand, so a change
to either is checked here:

- the snippet's required fields agree with the tables in decisions.md, read
  from the tables themselves, not from the snippet;
- every example line is in a ```json fence, one of each kind per file, in
  `json.dumps(sort_keys=True)` form, and passes the snippet's own check;
- the snippet refuses one broken record per rule it states;
- run for real in a scratch git repository, it appends as
  `src/curamoir/ledger.py` does, refuses a second valid line for one id,
  allows one over a torn line, and works from a subdirectory and under -O.

The counting rules (outcome in effect, ties, n/a (0)) are counted by hand in
phase 1 and have no code to test yet.
"""

import ast
import collections
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / ".curamoir" / "decisions.md"
SKILL = ROOT / ".claude" / "skills" / "decision-log" / "SKILL.md"
RECORD_LINE = 'record = {"record": "decision", "id": "d-cm-abc-3"}  # every field of its kind'


def _bash_block():
    blocks = re.findall(r"```bash\n(python3 - <<'EOF'\n.*?\nEOF\n)```", SKILL.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1, "SKILL.md holds exactly one append snippet"
    assert blocks[0].count(RECORD_LINE) == 1, "the snippet's record line is the template the tests replace"
    return blocks[0]


def _python_of_snippet():
    return _bash_block().split("\n", 1)[1].rsplit("\nEOF\n", 1)[0]


@pytest.fixture(scope="module")
def snippet():
    """The snippet's REQUIRED and problems(), run from the file's own code."""
    code = _python_of_snippet().split("\nerrors = problems(record)")[0]
    space = {}
    cwd = os.getcwd()
    os.chdir(ROOT)  # its first line asks git for the checkout's top
    try:
        exec(compile(code, str(SKILL), "exec"), space)
    finally:
        os.chdir(cwd)
    return space


def _table(kind):
    """The rows of decisions.md's table for one record kind: {field: (required, content)}."""
    text = SCHEMA.read_text(encoding="utf-8")
    section = re.search(rf"^## `{kind}`\n(.*?)(?=^## )", text, re.S | re.M)
    assert section, f"decisions.md has a ## `{kind}` section"
    rows = re.findall(r"^\| `(\w+)` \| (yes|no) \| (.*) \|$", section.group(1), re.M)
    assert rows, f"the {kind} table has rows"
    return {name: (required == "yes", content) for name, required, content in rows}


def _backticked(content):
    return set(re.findall(r"`([^`]+)`", content))


def _examples(path):
    text = path.read_text(encoding="utf-8")
    fenced = [line for block in re.findall(r"```json\n(.*?)```", text, re.S) for line in block.strip().splitlines()]
    anywhere = [line for line in text.splitlines() if line.lstrip().startswith("{") and '"record"' in line]
    return fenced, anywhere


# --- the schema, as decisions.md states it -----------------------------------

@pytest.mark.parametrize("kind", ["decision", "outcome", "reversal"])
def test_the_snippet_requires_exactly_the_fields_the_table_marks_required(snippet, kind):
    required = {name for name, (needed, _) in _table(kind).items() if needed}
    # `record` itself picks the kind, so the snippet checks it before REQUIRED.
    assert set(snippet["REQUIRED"][kind].split()) | {"record"} == required


def test_the_value_sets_in_the_tables_are_the_ones_the_snippet_checks():
    # Read from the tables, so the checks below cannot drift from what the
    # schema says (the snippet is checked against these by the refusals).
    outcome, reversal, decision = _table("outcome"), _table("reversal"), _table("decision")
    assert _backticked(outcome["value"][1]) == {"correct", "incorrect", "indeterminate"}
    assert _backticked(outcome["source"][1]) == {"human_audit"}
    assert _backticked(outcome["reclassified_to"][1]) == {"A", "B", "C"}
    assert _backticked(reversal["type"][1]) == {"human_override"}
    assert _backticked(reversal["actor_type"][1]) == {"human"}
    assert _backticked(decision["class"][1]) == {"A", "B", "C"}
    assert "escalate" in _backticked(decision["value"][1])
    assert "YYYY-MM-DDTHH:MM:SSZ" in decision["ts"][1]
    # The wording each refusal below encodes, so the table cannot change alone.
    assert "two or more" in decision["options"][1]
    assert "0.0 to 1.0" in decision["confidence"][1]
    assert "`d-<bead>-<n>`" in decision["id"][1]
    assert "`false` for class C, else `true`" in decision["reversible"][1]
    assert "`false` when the user answered" in decision["autonomous"][1]


#: (kind, field) -> the value set its table row gives, read from decisions.md.
def _value_sets():
    outcome, reversal, decision = _table("outcome"), _table("reversal"), _table("decision")
    return {
        ("outcome", "value"): _backticked(outcome["value"][1]),
        ("outcome", "source"): _backticked(outcome["source"][1]),
        ("outcome", "reclassified_to"): _backticked(outcome["reclassified_to"][1]),
        ("reversal", "type"): _backticked(reversal["type"][1]),
        ("reversal", "actor_type"): _backticked(reversal["actor_type"][1]),
        ("decision", "class"): _backticked(decision["class"][1]),
    }


def _with(kind, field, value):
    record = {**_example(kind), field: value}
    if (kind, field) == ("decision", "class"):
        record["reversible"] = record["autonomous"] = value != "C"
    return record


def _snippet_value_sets():
    """{(kind, field): set} for every `r[field]` (or `r.get(field, ...)`) that
    problems() compares, by `in` or `==`, with literal values, in the branch
    for each record kind. Parsed from the snippet's code, so a set widened by
    any value at all shows up here."""
    tree = ast.parse(_python_of_snippet())
    problems = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "problems")

    def branch_kind(test):
        if (isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.Eq)
                and _field(test.left) == "record" and isinstance(test.comparators[0], ast.Constant)):
            return test.comparators[0].value
        return None

    branches = {}
    chain = next(node for node in ast.walk(problems) if isinstance(node, ast.If) and branch_kind(node.test) == "decision")
    while True:
        branches[branch_kind(chain.test)] = chain.body
        rest = chain.orelse
        if len(rest) == 1 and isinstance(rest[0], ast.If) and branch_kind(rest[0].test):
            chain = rest[0]
            continue
        branches["reversal"] = rest
        break
    found = {}
    for kind, body in branches.items():
        for node in (n for statement in body for n in ast.walk(statement)):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], (ast.In, ast.Eq)):
                continue
            field, literal = _field(node.left), node.comparators[0]
            if field is None or field == "record":
                continue
            if isinstance(literal, ast.Constant):
                values = {literal.value}
            elif isinstance(literal, ast.Tuple) and all(isinstance(e, ast.Constant) for e in literal.elts):
                values = {e.value for e in literal.elts}
            else:
                continue
            found.setdefault((kind, field), set()).update(values)
    return found


def _field(node):
    """The field name in `r["name"]` or `r.get("name", ...)`, else None."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant)):
        return node.args[0].value
    return None


def test_the_snippets_value_sets_equal_the_tables():
    assert _snippet_value_sets() == _value_sets()


@pytest.mark.parametrize("kind, field", list(_value_sets()), ids="-".join)
def test_the_snippet_accepts_exactly_the_value_set_its_table_gives(snippet, kind, field):
    allowed = _value_sets()[(kind, field)]
    for value in allowed:
        assert snippet["problems"](_with(kind, field, value)) == [], value
    outside = {max(allowed) + "x", "x" + min(allowed), chr(ord(max(allowed)[0]) + 1), "wrong", "right", "D", ""}
    for value in outside - allowed:
        assert snippet["problems"](_with(kind, field, value)) != [], value


# --- the examples ------------------------------------------------------------

@pytest.mark.parametrize("path", [SCHEMA, SKILL], ids=["decisions.md", "SKILL.md"])
def test_each_file_has_one_valid_example_of_each_kind_all_in_json_fences(snippet, path):
    fenced, anywhere = _examples(path)
    assert sorted(anywhere) == sorted(fenced), "a record line sits outside a ```json fence"
    kinds = collections.Counter()
    for line in fenced:
        record = json.loads(line)
        kinds[record["record"]] += 1
        assert json.dumps(record, sort_keys=True) == line, "not in json.dumps(sort_keys=True) form"
        assert snippet["problems"](record) == []
        for (kind, field), allowed in _value_sets().items():
            if kind == record["record"] and field in record:
                assert record[field] in allowed, (kind, field, "not in its table's set")
    assert kinds == {"decision": 1, "outcome": 1, "reversal": 1}


# --- what the snippet refuses ------------------------------------------------

def _example(kind):
    fenced, _ = _examples(SCHEMA)
    return next(json.loads(line) for line in fenced if json.loads(line)["record"] == kind)


def _without(record, name):
    return {k: v for k, v in record.items() if k != name}


def _broken():
    d, o, r = _example("decision"), _example("outcome"), _example("reversal")
    no_summary = [{"label": "a"}, d["options"][1]]
    return {
        "not an object": ([1, 2], "record:"),
        "unknown kind": ({**o, "record": "verdict"}, "record:"),
        "decision missing falsifier": (_without(d, "falsifier"), "missing"),
        "outcome missing source": (_without(o, "source"), "missing"),
        "reversal missing actor_type": (_without(r, "actor_type"), "missing"),
        "ts with a space": ({**d, "ts": "2026-11-16 10:02:11Z"}, "ts:"),
        "ts with fractions": ({**d, "ts": "2026-11-16T10:02:11.5Z"}, "ts:"),
        "ts unpadded": ({**d, "ts": "2026-1-6T1:2:3Z"}, "ts:"),
        "ts not a real date": ({**d, "ts": "2026-02-30T10:00:00Z"}, "ts:"),
        "ts not text": ({**d, "ts": 20261116}, "ts:"),
        "empty question": ({**d, "question": " "}, "question:"),
        "empty sha": ({**d, "sha": ""}, "sha:"),
        "empty bead": ({**d, "bead": ""}, "bead:"),
        "empty rationale": ({**d, "rationale": ""}, "rationale:"),
        "blank undo": ({**d, "undo": "  "}, "undo:"),
        "empty falsifier": ({**d, "falsifier": ""}, "falsifier:"),
        "falsifier not text": ({**d, "falsifier": None}, "falsifier:"),
        "one option": ({**d, "options": d["options"][:1]}, "options:"),
        "repeated label": ({**d, "options": [d["options"][0], d["options"][0]]}, "options:"),
        "option without summary": ({**d, "options": no_summary}, "options:"),
        "options not a list": ({**d, "options": {"a": "x"}}, "options:"),
        "value not a label": ({**d, "value": "z"}, "value:"),
        "value escalate": ({**d, "options": [*d["options"], {"label": "escalate", "summary": "ask"}],
                            "value": "escalate"}, "value:"),
        "confidence above 1": ({**d, "confidence": 1.5}, "confidence:"),
        "confidence below 0": ({**d, "confidence": -0.1}, "confidence:"),
        "confidence a boolean": ({**d, "confidence": True}, "confidence:"),
        "confidence text": ({**d, "confidence": "0.8"}, "confidence:"),
        "class Z": ({**d, "class": "Z"}, "class:"),
        "class C reversible": ({**d, "class": "C", "autonomous": False}, "reversible:"),
        "class C autonomous": ({**d, "class": "C", "reversible": False}, "autonomous:"),
        "class A not reversible": ({**d, "reversible": False}, "reversible:"),
        "autonomous as 1": ({**d, "autonomous": 1}, "autonomous:"),
        "id of another bead": ({**d, "id": "d-cm-xyz-1"}, "id:"),
        "id n of 0": ({**d, "id": "d-cm-abc-0"}, "id:"),
        "id with a trailing newline": ({**d, "id": "d-cm-abc-1\n"}, "id:"),
        "id with the bead's dot unescaped": ({**d, "bead": "cm-rp1.2", "id": "d-cm-rp1x2-1"}, "id:"),
        "depends_on not a list": ({**d, "depends_on": "d-cm-abc-1"}, "depends_on:"),
        "depends_on holding a number": ({**d, "depends_on": ["d-cm-abc-1", 5]}, "depends_on:"),
        "outcome value": ({**o, "value": "right"}, "value:"),
        "outcome source": ({**o, "source": "grader"}, "source:"),
        "outcome reclassified_to": ({**o, "reclassified_to": "D"}, "reclassified_to:"),
        "outcome note not text": ({**o, "note": 3}, "note:"),
        "outcome empty decision_id": ({**o, "decision_id": ""}, "decision_id:"),
        "reversal type": ({**r, "type": "appeal"}, "type"),
        "reversal actor_type": ({**r, "actor_type": "service"}, "actor_type"),
        "reversal empty new_value": ({**r, "new_value": ""}, "new_value"),
        "reversal empty decision_id": ({**r, "decision_id": " "}, "decision_id:"),
        "reversal reason not text": ({**r, "reason": 3}, "reason"),
    }


@pytest.mark.parametrize("name", list(_broken()))
def test_the_snippet_refuses_each_broken_record(snippet, name):
    record, reason = _broken()[name]
    found = snippet["problems"](record)
    assert any(reason in problem for problem in found), found


def test_the_outcome_and_reversal_optional_fields_may_be_left_out(snippet):
    for kind, optional in (("outcome", ("note", "reclassified_to")), ("reversal", ("new_value", "reason"))):
        record = _example(kind)
        for name in optional:
            record = _without(record, name)
        assert snippet["problems"](record) == []


# --- the snippet, run as the agent runs it -----------------------------------

@pytest.fixture
def checkout(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".curamoir").mkdir()
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    return tmp_path


def _append(checkout, record, cwd=None, optimise=False):
    source = checkout / "record.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    script = _bash_block().replace(RECORD_LINE, f"record = json.loads(open({str(source)!r}).read())")
    env = {**os.environ, "PYTHONOPTIMIZE": "1"} if optimise else {k: v for k, v in os.environ.items()
                                                                   if k != "PYTHONOPTIMIZE"}
    return subprocess.run(["bash", "-c", script], cwd=cwd or checkout, env=env,
                          capture_output=True, text=True, timeout=60)


def _log(checkout):
    return checkout / ".curamoir" / "decisions.jsonl"


def test_it_writes_each_kind_as_one_sorted_line_from_a_subdirectory(checkout):
    expected = ""
    for kind in ("decision", "outcome", "reversal"):
        record = _example(kind)
        # Keys in reverse order, so only sort_keys can give the sorted line.
        unsorted = dict(reversed(list(record.items())))
        assert json.dumps(unsorted) != json.dumps(record, sort_keys=True)
        run = _append(checkout, unsorted, cwd=checkout / "sub" / "deep")
        assert run.returncode == 0, run.stderr
        expected += json.dumps(record, sort_keys=True) + "\n"
    assert _log(checkout).read_text(encoding="utf-8") == expected


def test_a_decision_may_be_appended_again_over_a_line_that_breaks_the_schema(checkout):
    record = _example("decision")
    broken = json.dumps(_without(record, "falsifier"), sort_keys=True) + "\n"
    _log(checkout).write_text(broken, encoding="utf-8")
    run = _append(checkout, record)
    assert run.returncode == 0, run.stderr
    assert _log(checkout).read_text(encoding="utf-8") == broken + json.dumps(record, sort_keys=True) + "\n"
    assert _append(checkout, record).returncode != 0


def test_run_twice_it_writes_one_decision_line(checkout):
    record = _example("decision")
    assert _append(checkout, record).returncode == 0
    again = _append(checkout, record)
    assert again.returncode != 0
    assert "already has a valid decision line" in again.stderr
    assert _log(checkout).read_text(encoding="utf-8").count("\n") == 1


def test_an_outcome_after_its_decision_is_not_taken_for_a_duplicate(checkout):
    assert _append(checkout, _example("decision")).returncode == 0
    assert _append(checkout, _example("outcome")).returncode == 0
    assert _append(checkout, _example("reversal")).returncode == 0


def test_a_torn_last_line_is_closed_and_its_decision_may_be_appended_again(checkout):
    record = _example("decision")
    torn = json.dumps(record, sort_keys=True)[:60]
    _log(checkout).write_text(torn, encoding="utf-8")
    run = _append(checkout, record)
    assert run.returncode == 0, run.stderr
    assert _log(checkout).read_text(encoding="utf-8") == torn + "\n" + json.dumps(record, sort_keys=True) + "\n"
    assert _append(checkout, record).returncode != 0


def test_a_refused_record_writes_nothing_even_under_python_optimise(checkout):
    record = {**_example("decision"), "confidence": 7}
    for optimise in (False, True):
        run = _append(checkout, record, optimise=optimise)
        assert run.returncode != 0
        assert "not written: confidence" in run.stderr
    assert not _log(checkout).exists()


# --- the skill activates only when asked -------------------------------------

def test_the_skill_description_limits_it_to_the_trial():
    front = re.match(r"---\n(.*?)\n---\n", SKILL.read_text(encoding="utf-8"), re.S)
    assert front, "SKILL.md starts with frontmatter"
    assert re.search(r"^name: decision-log$", front.group(1), re.M)
    description = " ".join(front.group(1).split("description:", 1)[1].split())
    assert description.startswith(">- ")
    # The normative sentence is the blockquote in decisions.md.
    quoted = re.findall(r"^> (Use ONLY when .*)$", SCHEMA.read_text(encoding="utf-8"), re.M)
    assert len(quoted) == 1, "decisions.md states the trigger sentence once"
    sentence = quoted[0]
    assert sentence.endswith("never otherwise.")
    assert description[len(">- "):].split("never otherwise.", 1)[0] + "never otherwise." == sentence


def test_the_skill_is_not_on_the_plugin_surface():
    assert not (ROOT / "skills" / "decision-log").exists()
