"""E2-R1: `principles.md` — the root of the promotion ladder, parsed.

This file is the source of truth for every sweep, and it is human-authored.
Those two facts set the whole design.

**Why each principle keeps its raw text.** E2-R1 requires that parsing and
re-serialising be byte-identical, and `/curamoir:promote` and `/curamoir:demote`
rewrite the file (E4-R6). A parse-then-re-emit design cannot give you both: to
re-emit you must choose a canonical layout, and imposing one on a file somebody
edits by hand turns every promotion into a whole-file diff. So a `Principle`
carries the exact lines it was parsed from, `serialise_principles` reassembles
those spans, and byte-identical holds by construction. A promotion replaces one
line inside one span and leaves every other byte alone.

**Why the id is validated so hard.** It is a reference, not a label: rules cite
it (E2-R3), the ledger counts fixes against it (E3-R5), findings carry it
(E5-R1), and E2-R4's output links to its anchor. So a duplicate id, or a heading
that disagrees with the `id:` field, is not untidiness — it makes every
downstream lookup resolve to something arbitrary while still appearing to work.
Those are refused here, where there is a line number to report.

**Why an unrecognised field is kept.** E2-R1 names eight required fields and
says nothing about others. The bundled fixture carries a `source:` field
recording where its wording came from, which is exactly the kind of thing an
author should be able to add. Unknown fields are preserved through a rewrite;
rejecting them would make the format the parser's rather than the author's.

Format, by example — the heading is the anchor, the bullets are the fields, and
a value may wrap onto indented continuation lines:

    ## structured-logging

    - **id:** `structured-logging`
    - **title:** Structured logging only
    - **rule:** Emit diagnostics through the module logger, never `print`.
    - **why:** A print has no level and no logger name, so it cannot be
      filtered, routed or correlated.
    - **good:** `logger.info("done")`
    - **bad:** `print("done")`
    - **tier:** principle
    - **promoted_on:** null
"""

import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Optional

#: E2-R1's eight, in the order the spec lists them. Order matters only for the
#: error message, which reports the first missing field a reader should add.
REQUIRED_FIELDS = ("id", "title", "rule", "why", "good", "bad", "tier", "promoted_on")

#: E2-R1's tier vocabulary, and E4's ladder in ascending strictness: a
#: principle is advice, a rule is linted, a guard blocks.
TIERS = ("principle", "rule", "guard")

#: `## <id>` opens a principle. Anything before the first one is preamble and is
#: preserved verbatim — it is where an author explains the file.
_HEADING = re.compile(r"^##\s+(\S+)\s*$")

#: `- **field:** value`, tolerating the spacing a hand produces.
_FIELD = re.compile(r"^-\s+\*\*(?P<name>[A-Za-z_][A-Za-z0-9_]*):\*\*\s*(?P<value>.*)$")


class PrincipleError(Exception):
    """`principles.md` could not be read as principles.

    Every message names the principle and the field, because E2-R1's acceptance
    criterion is that a missing field is reported by name — the difference
    between a one-line fix and a trip back to the specification.
    """


@dataclass(frozen=True)
class Principle:
    """One principle, and the exact text it came from.

    `lines` is the span this was parsed from, including its heading and any
    blank lines inside it. It is what `serialise_principles` writes back, so a
    value read out of a field here is a convenience for consumers while the
    text remains authoritative.
    """

    id: str
    title: str
    rule: str
    why: str
    good: str
    bad: str
    tier: str
    promoted_on: Optional[str]
    #: Fields the author added that E2-R1 does not require, such as `source`.
    extra: dict = field(default_factory=dict)
    #: The verbatim source span, which `serialise_principles` reassembles.
    lines: tuple = ()
    #: 1-based line number of the heading, for error messages.
    line_number: int = 0


@dataclass(frozen=True)
class PrinciplesFile:
    """A parsed `principles.md`: the preamble, the principles, and the trailer.

    Holding the preamble and trailer verbatim is what lets an author keep prose
    above and below the principles — the bundled fixture has several paragraphs
    explaining its own provenance — without a rewrite eating it.
    """

    preamble: tuple = ()
    principles: tuple = ()
    trailer: tuple = ()
    #: True when the source had no final newline, so a rewrite does not add one.
    ends_with_newline: bool = True

    def by_id(self, principle_id):
        """The principle with `principle_id`, or None."""
        for principle in self.principles:
            if principle.id == principle_id:
                return principle
        return None

    def with_field(self, principle_id, name, value):
        """This file with one field of one principle set to `value`.

        The unit `/curamoir:promote` and `/curamoir:demote` work in. Only the
        matching `- **name:**` line changes; every other byte of the file,
        including the rest of that principle, is carried through untouched.
        """
        target = self.by_id(principle_id)
        if target is None:
            raise PrincipleError(
                f"no principle with id {principle_id!r} in this file; "
                f"it defines: {', '.join(p.id for p in self.principles) or 'none'}"
            )
        rewritten = []
        for principle in self.principles:
            if principle.id != principle_id:
                rewritten.append(principle)
                continue
            rewritten.append(_with_line_rewritten(principle, name, value))
        return replace(self, principles=tuple(rewritten))


def _with_line_rewritten(principle, name, value):
    """`principle` with its `name` field line replaced, raw text and all."""
    lines = []
    replaced = False
    for line in principle.lines:
        match = _FIELD.match(line)
        if match and match.group("name") == name and not replaced:
            lines.append(f"- **{name}:** {value}")
            replaced = True
            continue
        lines.append(line)
    if not replaced:
        # Appended rather than refused: promoting a principle that has never
        # carried a `promoted_on` is the ordinary case, not an error.
        lines.append(f"- **{name}:** {value}")
    updated = replace(principle, lines=tuple(lines))
    return replace(updated, **_coerce({**_fields_of(lines), "id": principle.id},
                                      principle.id, principle.line_number))


def parse_principles(text):
    """Parse `text` as a principles file. Raises PrincipleError if it is not one.

    An empty file, or one with prose and no principles, is valid and parses to
    zero principles — that is what `/curamoir:init` writes before Epic 2 ships a
    starter set, and reporting it as an error would make a fresh repo look
    broken.
    """
    source = text or ""
    lines = source.split("\n")
    ends_with_newline = source.endswith("\n") or source == ""
    if ends_with_newline and lines and lines[-1] == "":
        lines = lines[:-1]

    preamble, blocks = _split_into_blocks(lines)
    principles = []
    seen = {}
    for heading_id, block, line_number in blocks:
        principle = _principle_from(heading_id, block, line_number)
        if principle.id in seen:
            raise PrincipleError(
                f"principle id {principle.id!r} is defined twice, on lines "
                f"{seen[principle.id]} and {line_number}. Rules cite an id "
                "(E2-R3), the ledger counts fixes against it (E3-R5) and "
                "findings carry it (E5-R1), so one id must mean one principle."
            )
        seen[principle.id] = line_number
        principles.append(principle)

    return PrinciplesFile(
        preamble=tuple(preamble),
        principles=tuple(principles),
        trailer=(),
        ends_with_newline=ends_with_newline,
    )


def serialise_principles(parsed):
    """`parsed` back to text. Byte-identical to the source when unmodified."""
    lines = list(parsed.preamble)
    for principle in parsed.principles:
        lines.extend(principle.lines)
    lines.extend(parsed.trailer)
    text = "\n".join(lines)
    return text + "\n" if parsed.ends_with_newline else text


def load_principles(repo):
    """The repo's `.curamoir/principles.md`, or an empty file if absent.

    Absent is not an error: a repo initialised before Epic 2 ships has no
    starter set, and `/curamoir:doctor` reports the count rather than failing.
    """
    path = Path(repo) / ".curamoir" / "principles.md"
    if not path.is_file():
        return PrinciplesFile()
    return parse_principles(path.read_text(encoding="utf-8"))


def _split_into_blocks(lines):
    """(preamble, [(heading_id, block_lines, line_number)]).

    A principle's block runs to the line before the next `## ` heading, so any
    blank lines and prose inside it belong to it and survive a rewrite.
    """
    preamble, blocks = [], []
    current = None
    for index, line in enumerate(lines, start=1):
        heading = _HEADING.match(line)
        if heading:
            if current is not None:
                blocks.append(current)
            current = (heading.group(1), [line], index)
            continue
        if current is None:
            preamble.append(line)
        else:
            current[1].append(line)
    if current is not None:
        blocks.append(current)
    return preamble, [(name, tuple(block), number) for name, block, number in blocks]


def _fields_of(lines):
    """`{name: value}` for the `- **name:** value` bullets in `lines`.

    A value may wrap onto indented continuation lines, because these are
    sentences a person reads in an editor. Continuations are joined with a
    single space, which is what the wrapped prose means.
    """
    found = {}
    name = None
    parts = []
    for line in lines:
        match = _FIELD.match(line)
        if match:
            if name is not None:
                found[name] = " ".join(parts).strip()
            name = match.group("name")
            parts = [match.group("value").strip()]
            continue
        if name is not None and line.startswith((" ", "\t")) and line.strip():
            parts.append(line.strip())
            continue
        if name is not None and not line.strip():
            found[name] = " ".join(parts).strip()
            name, parts = None, []
    if name is not None:
        found[name] = " ".join(parts).strip()
    return found


def _principle_from(heading_id, block, line_number):
    values = _fields_of(block)

    missing = [name for name in REQUIRED_FIELDS if not values.get(name)]
    if missing:
        raise PrincipleError(
            f"principle {heading_id!r} (line {line_number}) is missing required "
            f"field(s): {', '.join(missing)}. E2-R1 requires "
            f"{', '.join(REQUIRED_FIELDS)}."
        )

    declared = _unwrap_code(values["id"])
    if declared != heading_id:
        raise PrincipleError(
            f"the heading `## {heading_id}` on line {line_number} disagrees with "
            f"its id field {declared!r}. The heading is the anchor E2-R4 links "
            "to and the field is what rules cite, so they must be the same."
        )

    coerced = _coerce(values, declared, line_number)
    extra = {
        name: value for name, value in values.items()
        if name not in REQUIRED_FIELDS
    }
    return Principle(lines=tuple(block), line_number=line_number, extra=extra, **coerced)


def _coerce(values, declared, line_number):
    """The eight required fields, validated and typed."""
    tier = values["tier"].strip()
    if tier not in TIERS:
        raise PrincipleError(
            f"principle {declared!r} (line {line_number}) has tier {tier!r}; "
            f"E2-R1 allows only: {', '.join(TIERS)}."
        )

    return {
        "id": declared,
        "title": values["title"],
        "rule": values["rule"],
        "why": values["why"],
        "good": values["good"],
        "bad": values["bad"],
        "tier": tier,
        "promoted_on": _promoted_on(values["promoted_on"], declared, line_number),
    }


def _promoted_on(raw, declared, line_number):
    """An ISO date, or None for `null`.

    Validated rather than passed through because E4 computes promotion windows
    from it: an unparseable date there is a silently wrong window, not a
    formatting nit.
    """
    value = raw.strip().strip("`")
    if value.lower() in ("null", "none", "~"):
        return None
    try:
        date.fromisoformat(value)
    except ValueError as invalid:
        raise PrincipleError(
            f"principle {declared!r} (line {line_number}) has promoted_on "
            f"{value!r}, which is not an ISO date (YYYY-MM-DD) or `null`: "
            f"{invalid}"
        ) from invalid
    return value


def _unwrap_code(value):
    """`` `slug` `` or `slug` — the fixture backticks its id, and a hand would."""
    return value.strip().strip("`").strip()
