"""R5-R30 and §11 "Mutation block": a brief's mutation list, read and applied.

A brief carries one fenced block whose info string is ``r5-mutations``; its
content (as CommonMark defines a fenced block's content) is a YAML list of
entries, each naming a place in the code, a mutation to make there, and the
test selection that must go red when it is made:

    - path: src/opensrm/parser/v2.py
      locate: {lines: [118, 131]}          # or {anchor: "def classify("}
      mutation: invert_condition           # or drop_argument, return_constant,
                                           # delete_statement, swap_operator,
                                           # or {replace: {from: "", to: ""}}
      expect_red: "python -m pytest tests/test_parser_v2.py::test_x -q"
      behaviour: "a judgment SLO maps to its own type"   # optional

This module is the pure half: extracting the block, refusing an entry the
contract does not allow, finding the entry's region and making its mutation
in a string. Running the selection is gate/mutations.py's.

**Refusals** (MutationListError), the user's rulings of 2026-11-24: no block,
two blocks or an empty list; an entry key outside the schema (``behavior`` is
refused naming ``behaviour``, the field it was meant to be); a path that is
not plainly repo-relative; a ``locate`` that is neither form; an unknown
mutation; one of the five named operators on a file that is not Python (only
``replace`` is text, the rest read the syntax tree).

**Applying** (d-cm-8ap.19-6..9). ``lines`` is an inclusive 1-based range;
``anchor`` is the one line containing the text, extended to the end of the
statement that starts on it (an anchor on a ``def`` takes the function). A
named operator acts on the first matching node inside the region, by editing
the source text at the node's position, so nothing else in the file changes.
A locate that matches nothing (or an anchor that matches twice), an operator
with nothing to act on in the region, and a ``replace`` whose text is not
there exactly once all mean the entry and the code have drifted: the caller
reports the entry STALE (R5-R31).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from curamoir.config import RefusedYAMLError, safe_load_unique

INFO_STRING = "r5-mutations"

#: The five operators that read Python's syntax tree, and the text one.
OPERATORS = ("invert_condition", "drop_argument", "return_constant", "delete_statement",
             "swap_operator")
REPLACE = "replace"

ENTRY_KEYS = ("path", "locate", "mutation", "expect_red")
OPTIONAL_KEYS = ("behaviour",)
#: Spellings refused with the name they were meant to be.
MISSPELT = {"behavior": "behaviour"}

PYTHON_SUFFIXES = (".py", ".pyi")

_OPENING = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


class MutationListError(ValueError):
    """The brief's mutation list is not one the contract allows."""


@dataclass(frozen=True)
class Entry:
    """One entry, validated. ``locate`` is ("lines", (first, last)) or
    ("anchor", text); ``mutation`` is an operator name or ("replace",
    from, to)."""

    index: int
    path: str
    locate: tuple
    mutation: object
    expect_red: str
    behaviour: str | None = None

    def describe_mutation(self) -> str:
        if isinstance(self.mutation, tuple):
            return f"replace {self.mutation[1]!r} with {self.mutation[2]!r}"
        return self.mutation

    def describe_locate(self) -> str:
        kind, value = self.locate
        return f"lines {value[0]}-{value[1]}" if kind == "lines" else f"anchor {value!r}"


def extract_block(markdown: str) -> str:
    """The content of the brief's one ``r5-mutations`` fenced block, as
    CommonMark defines it: the lines between the opening fence and a
    closing fence of the same character at least as long, each with up to
    the opening fence's indentation removed, ending at the end of the
    document if the fence is never closed. MutationListError for none, or
    for more than one."""
    lines = markdown.splitlines(keepends=True)
    blocks, i = [], 0
    while i < len(lines):
        opening = _OPENING.match(lines[i].rstrip("\r\n"))
        if not opening:
            i += 1
            continue
        indent, fence, info = len(opening.group(1)), opening.group(2), opening.group(3)
        if fence[0] == "`" and "`" in info:
            i += 1      # not a fence (CommonMark: a backtick fence's info has no backtick)
            continue
        closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        content, j = [], i + 1
        while j < len(lines) and not closing.match(lines[j].rstrip("\r\n")):
            content.append(_unindent(lines[j], indent))
            j += 1
        words = info.strip().split()
        if words and words[0] == INFO_STRING:
            blocks.append("".join(content))
        i = j + 1
    if not blocks:
        raise MutationListError(f"the brief has no fenced `{INFO_STRING}` block")
    if len(blocks) > 1:
        raise MutationListError(f"the brief has {len(blocks)} `{INFO_STRING}` blocks; "
                                "give one, holding every entry")
    return blocks[0]


def _unindent(line: str, indent: int) -> str:
    removed = 0
    while removed < indent and line[removed:removed + 1] == " ":
        removed += 1
    return line[removed:]


def parse_entries(block: str) -> tuple:
    """The block's entries, each validated, in order. MutationListError
    names the entry (1-based) and the field for anything refused."""
    # Imported here: the CLI imports this module and must start without
    # PyYAML (scripts/curamoir picks an interpreter that may lack it).
    import yaml

    try:
        data = safe_load_unique(block)
    except (RefusedYAMLError, ValueError, yaml.YAMLError) as error:
        raise MutationListError(f"the `{INFO_STRING}` block is not valid YAML: {error}") \
            from error
    if not isinstance(data, list) or not data:
        raise MutationListError(f"the `{INFO_STRING}` block must be a non-empty list of "
                                "entries")
    return tuple(_entry(n, raw) for n, raw in enumerate(data, start=1))


def _entry(n: int, raw) -> Entry:
    where = f"entry {n}"
    if not isinstance(raw, dict):
        raise MutationListError(f"{where} is not a mapping")
    for key in raw:
        if key in MISSPELT:
            raise MutationListError(f"{where} has `{key}`; the field is "
                                    f"`{MISSPELT[key]}`")
        if key not in ENTRY_KEYS + OPTIONAL_KEYS:
            raise MutationListError(f"{where} has `{key}`, which is not a field; the fields "
                                    f"are {', '.join(ENTRY_KEYS + OPTIONAL_KEYS)}")
    missing = [key for key in ENTRY_KEYS if key not in raw]
    if missing:
        raise MutationListError(f"{where} is missing {', '.join(missing)}")
    path = raw["path"]
    if not _plain_path(path):
        raise MutationListError(f"{where}: path {path!r} is not a plain repo-relative path")
    locate = _locate(where, raw["locate"])
    mutation = _mutation(where, raw["mutation"])
    if not isinstance(mutation, tuple) and not path.endswith(PYTHON_SUFFIXES):
        raise MutationListError(f"{where}: `{mutation}` reads Python's syntax tree, and "
                                f"{path} is not Python; use `replace` for other files")
    expect_red = raw["expect_red"]
    if not (isinstance(expect_red, str) and expect_red.strip()):
        raise MutationListError(f"{where}: expect_red is the test command, non-empty text")
    behaviour = raw.get("behaviour")
    if behaviour is not None and not (isinstance(behaviour, str) and behaviour.strip()):
        raise MutationListError(f"{where}: behaviour is non-empty text when given")
    return Entry(n, path, locate, mutation, expect_red, behaviour)


def _plain_path(path) -> bool:
    if not (isinstance(path, str) and path.strip()) or path.startswith("/") or "\\" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _locate(where: str, raw) -> tuple:
    if isinstance(raw, dict) and set(raw) == {"lines"}:
        lines = raw["lines"]
        if (isinstance(lines, list) and len(lines) == 2
                and all(type(n) is int and n >= 1 for n in lines) and lines[0] <= lines[1]):
            return ("lines", (lines[0], lines[1]))
        raise MutationListError(f"{where}: locate lines is [first, last], 1-based, "
                                "first <= last")
    if isinstance(raw, dict) and set(raw) == {"anchor"}:
        if isinstance(raw["anchor"], str) and raw["anchor"].strip() \
                and "\n" not in raw["anchor"]:
            return ("anchor", raw["anchor"])
        raise MutationListError(f"{where}: locate anchor is one line of text")
    raise MutationListError(f"{where}: locate is {{lines: [first, last]}} or "
                            "{anchor: \"text\"}")


def _mutation(where: str, raw):
    if raw in OPERATORS:
        return raw
    if isinstance(raw, dict) and set(raw) == {REPLACE}:
        spec = raw[REPLACE]
        if isinstance(spec, dict) and set(spec) == {"from", "to"} \
                and isinstance(spec["from"], str) and spec["from"] \
                and isinstance(spec["to"], str) and spec["from"] != spec["to"]:
            return (REPLACE, spec["from"], spec["to"])
        raise MutationListError(f"{where}: replace is {{from: \"...\", to: \"...\"}}, "
                                "from non-empty and different from to")
    raise MutationListError(f"{where}: mutation {raw!r} is not one of "
                            f"{', '.join(OPERATORS)} or {{replace: {{from, to}}}}")


# --- the region and the mutation ---------------------------------------------

class Stale(Exception):
    """The entry no longer fits the code: the caller reports it STALE."""


def region(source: str, entry: Entry) -> tuple:
    """(first, last), the entry's region in ``source``, 1-based and
    inclusive. Stale when it matches nothing or is ambiguous."""
    lines = [line.rstrip("\r\n") for line in _lines(source)]
    kind, value = entry.locate
    if kind == "lines":
        first, last = value
        if last > len(lines):
            raise Stale(f"lines {first}-{last} run past the end of {entry.path} "
                        f"({len(lines)} lines)")
        return first, last
    hits = [n for n, line in enumerate(lines, start=1) if value in line]
    if len(hits) != 1:
        raise Stale(f"anchor {value!r} is on {len(hits)} lines of {entry.path}, not one")
    first = hits[0]
    if entry.path.endswith(PYTHON_SUFFIXES):
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            raise Stale(f"{entry.path} does not parse ({error})") from error
        # The anchor may be on a decorated def's own line or its first
        # decorator's: either starts the statement.
        starting = [node for node in ast.walk(tree)
                    if isinstance(node, ast.stmt) and first in (node.lineno, _first_line(node))]
        if starting:
            return first, max(node.end_lineno for node in starting)
    return first, first


def mutate(source: str, entry: Entry, first: int, last: int) -> str:
    """``source`` with the entry's mutation made inside lines first..last.
    Stale when the region holds nothing for it."""
    if isinstance(entry.mutation, tuple):
        return _replace(source, entry, first, last)
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise Stale(f"{entry.path} does not parse ({error})") from error
    nodes = sorted((node for node in ast.walk(tree)
                    if hasattr(node, "lineno") and first <= node.lineno
                    and node.end_lineno <= last),
                   key=lambda node: (node.lineno, node.col_offset))
    edit = _OPERATIONS[entry.mutation](nodes)
    if edit is None:
        raise Stale(f"`{entry.mutation}` finds nothing to act on in {entry.path} "
                    f"lines {first}-{last}")
    node, text = edit
    return _splice(source, node, text)


def _first_line(node) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno] + [d.lineno for d in decorators])


def _segment(node, source):
    return ast.get_source_segment(source, node, padded=False)


def _invert_condition(nodes):
    for node in nodes:
        test = getattr(node, "test", None)
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)) and test is not None:
            return test, "not ({})"
    return None


def _drop_argument(nodes):
    for node in nodes:
        if isinstance(node, ast.Call) and (node.args or node.keywords):
            return node, "drop"
    return None


def _return_constant(nodes):
    for node in nodes:
        if isinstance(node, ast.Return) and node.value is not None \
                and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            return node, "return None"
    return None


def _delete_statement(nodes):
    for node in nodes:
        if isinstance(node, ast.stmt) and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node, "pass"
    return None


#: Operator swaps, both ways.
_SWAPS = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.GtE: "<", ast.Gt: "<=",
          ast.LtE: ">", ast.In: "not in", ast.NotIn: "in", ast.Is: "is not",
          ast.IsNot: "is", ast.Add: "-", ast.Sub: "+", ast.Mult: "/", ast.Div: "*",
          ast.And: "or", ast.Or: "and"}


def _swap_operator(nodes):
    for node in nodes:
        if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                and type(node.ops[0]) in _SWAPS:
            return node, ("compare", _SWAPS[type(node.ops[0])])
        if isinstance(node, ast.BinOp) and type(node.op) in _SWAPS:
            return node, ("binop", _SWAPS[type(node.op)])
        if isinstance(node, ast.BoolOp) and type(node.op) in _SWAPS:
            return node, ("boolop", _SWAPS[type(node.op)])
    return None


_OPERATIONS = {"invert_condition": _invert_condition, "drop_argument": _drop_argument,
               "return_constant": _return_constant, "delete_statement": _delete_statement,
               "swap_operator": _swap_operator}


def _splice(source: str, node, how) -> str:
    """``source`` with ``node``'s text replaced as ``how`` says."""
    original = _segment(node, source)
    if how == "not ({})":
        new = f"not ({original})"
    elif how == "drop":
        new = _without_last_argument(node, source)
    elif how in ("return None", "pass"):
        new = how
    else:
        kind, operator = how
        new = _swapped(node, source, kind, operator)
    starts = _offsets(source)
    start = starts[node.lineno - 1] + _byte_col(source, node.lineno, node.col_offset)
    end = starts[node.end_lineno - 1] + _byte_col(source, node.end_lineno, node.end_col_offset)
    return source[:start] + new + source[end:]


def _lines(source: str) -> list:
    """`source`'s lines with their endings, split on "\r\n", "\r" or "\n"
    as Python's tokenizer counts lines (the file is read with its own
    endings kept; str.splitlines would also split on a form feed and on
    Unicode line separators, which can sit in a comment or a string)."""
    parts = re.split(r"(\r\n|\r|\n)", source)
    out = [parts[i] + parts[i + 1] for i in range(0, len(parts) - 1, 2)]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _offsets(source: str) -> list:
    """The character offset where each line starts."""
    starts, total = [], 0
    for line in _lines(source):
        starts.append(total)
        total += len(line)
    starts.append(total)
    return starts


def _byte_col(source: str, lineno: int, col: int) -> int:
    """ast's column is in UTF-8 bytes; the character column on that line."""
    line = _lines(source)[lineno - 1]
    return len(line.encode("utf-8")[:col].decode("utf-8", "replace"))


def _without_last_argument(call, source: str) -> str:
    func = _segment(call.func, source)
    parts = [_segment(a, source) for a in call.args] + \
        [(f"{k.arg}={_segment(k.value, source)}" if k.arg else f"**{_segment(k.value, source)}")
         for k in call.keywords]
    return f"{func}({', '.join(parts[:-1])})"


def _swapped(node, source: str, kind: str, operator: str) -> str:
    if kind == "compare":
        # Parenthesised as the other branches are: a node's segment does
        # not include its own parentheses, so `(a or b) == c` would
        # otherwise come back as `a or b != c`.
        return f"({_segment(node.left, source)}) {operator} ({_segment(node.comparators[0], source)})"
    if kind == "binop":
        return f"({_segment(node.left, source)}) {operator} ({_segment(node.right, source)})"
    return f" {operator} ".join(f"({_segment(v, source)})" for v in node.values)


def _replace(source: str, entry: Entry, first: int, last: int) -> str:
    _, old, new = entry.mutation
    starts = _offsets(source)
    start, end = starts[first - 1], starts[last]
    text = source[start:end]
    count = text.count(old)
    if count != 1:
        raise Stale(f"replace text {old!r} occurs {count} times in {entry.path} lines "
                    f"{first}-{last}, not once")
    return source[:start] + text.replace(old, new, 1) + source[end:]
