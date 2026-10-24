"""Markdown helpers for the text cúramóir writes into GitHub (the sweep PR
body: curamoir.sweep_pr, tier1.tier1_pr_lines). Pure. A module of its own
because sweep_pr imports tier1 and tier1 needs these helpers too: kept in
either, they would make an import cycle. Named `pr_markdown`, not
`markdown`, so it is not mistaken for the PyPI package.

- one_line(text): `text` REDACTED (curamoir.redaction), then with every
  character that could break a line or hide itself shown as a visible
  escape (`\\n`, `\\u202e`): every
  str.splitlines() break, and every Unicode Cc (control) and Cf (format:
  bidi overrides, zero-width characters) character. WHY: git allows a line
  break in a file name, and a path like `x.py\\n\\n### Test gate\\n...`
  would otherwise start a forged section of the body; a bidi override would
  make a path read as another. Every piece of repository or agent text the
  body interpolates goes through it (code_span does, and plain_text does
  for the reasons, notes and gate line). WHY redact first, here: most
  redaction rules start at a word boundary (`\\bghp_`, `\\bAKIA`,
  `\\bBearer`), and an escape turns the separator before a secret into
  word characters (a line break becomes backslash-n, U+200B becomes
  backslash-u200b), so a body redacted only after escaping would leak the
  token. Redacting inside one_line means no caller can get the order
  wrong. The composed body is NOT redacted again as a whole: that would
  undo plain_text's `\\[` escape of a placeholder (sweep_pr's module
  docstring, "Redaction"); sweep_pr only verifies it.
- plain_text(text): one_line(text) with `<` written `&lt;`, for text that
  is not in a code span (reasons, notes, the gate line): inline HTML such
  as `<h3>` or `<details>` would otherwise render as structure GitHub
  shows but no `#` heading check sees.
- code_span(text): one_line(`text`) as a CommonMark code span that shows
  it exactly, whatever backticks it holds. A path, a rule id or a reason
  is repository or agent text; one with a backtick in it would otherwise
  close the span early and turn the rest of the line into Markdown.
- location(path, line, end_line): where a fix is, as `path` in a code
  span and then ` line N` or ` lines N-M`. WHY never `path:N`: redaction's
  generic rule (curamoir.redaction) reads a file name holding a secret
  keyword (`token_store.py`, `secrets.py`) as a NAME and `:` as its
  separator, and replaces what follows (`120-125`) with the placeholder,
  erasing the location and leaving the code span open. A path is never
  followed directly by `:` or `=` here.
- inert_comments(text): `text` with every `<!--` broken up, so text from the
  repository cannot open an HTML comment that hides the rest of the body.

Syntax that imports on 3.9, like sweep.py.
"""

import re
import unicodedata

from curamoir.redaction import redact

_BACKTICKS = re.compile(r"`+")

#: What an HTML comment opener becomes: visible, and no longer an opener.
INERT_COMMENT = "< !--"

#: Every character str.splitlines() breaks a line at.
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")

#: The escapes Python's repr uses, kept for the common three.
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def one_line(text):
    """`text` redacted, then with each line break, control (Cc) and format
    (Cf) character replaced by a visible escape: `\\n`, `\\r`, `\\t`,
    else `\\xNN`, `\\uNNNN` or `\\UNNNNNNNN`. Redaction comes first
    (module docstring): escaping first would hide secrets from it."""
    return "".join(_escaped(char) for char in redact(str(text)))


def plain_text(text):
    """one_line(text) for Markdown outside a code span: `<` becomes `&lt;`,
    so the text can open no HTML element (a heading, a `<details>`, a
    comment), and `[` becomes `&#91;`, so it can open no link or image: a
    tier-1 note is model text steered by the repository, and a reviewer
    should not be handed a live `[click](https://...)` in the body. WHY an
    entity and not `\\[`: a backslash the text already holds would escape
    that backslash instead (`\\[click](...)` renders a live link), while
    nothing can escape an entity's `&`."""
    return one_line(text).replace("<", "&lt;").replace("[", "&#91;")


def _escaped(char):
    if char not in _LINE_BREAKS and unicodedata.category(char) not in ("Cc", "Cf"):
        return char
    if char in _NAMED:
        return _NAMED[char]
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def code_span(text):
    """`text` in a code span: a fence of one backtick more than its longest
    run of backticks, padded with a space each side when it starts or ends
    with a backtick or with a space (CommonMark strips one space from each
    side of a span that has both, so the padding round-trips)."""
    text = one_line(text)
    longest = max((len(run) for run in _BACKTICKS.findall(text)), default=0)
    fence = "`" * (longest + 1)
    if text and (text[0] in "` " or text[-1] in "` "):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def location(path, line, end_line=None):
    """`path` (a code span) and its line or line range (module docstring).
    ValueError unless the lines are whole numbers, 1 or more, with `line`
    not past `end_line`."""
    for number in (line,) if end_line is None else (line, end_line):
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError(f"a line number is a whole number of 1 or more, not {number!r}")
    if end_line is not None and line > end_line:
        raise ValueError(f"a line range runs forwards, not {line}-{end_line}")
    if end_line is None or end_line == line:
        return f"{code_span(path)} line {line}"
    return f"{code_span(path)} lines {line}-{end_line}"


def inert_comments(text):
    """`text` with every `<!--` replaced by INERT_COMMENT."""
    return text.replace("<!--", INERT_COMMENT)
