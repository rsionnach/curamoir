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
  undo plain_text's `&#91;` escape of a placeholder's `[` (sweep_pr's
  module docstring, "Redaction"); sweep_pr only verifies it.
- plain_text(text): one_line(text) with Markdown's live syntax written as
  HTML entities, for text that is not in a code span (reasons, notes, the
  gate line): `&` as `&amp;` first (an entity the text already holds,
  `&#64;name`, would otherwise be decoded into a live mention), `<` as
  `&lt;` (inline HTML such as `<h3>` or `<details>` would render as
  structure no `#` heading check sees), `[` as `&#91;` (no link or
  image), and GitHub's autolinks, mentions and references broken: `://`
  as `&#58;//`, `www.` as `www&#46;`, `@` as `@&#8203;`, `#` before a digit
  as `#&#8203;`, `GH-` before a digit as `GH&#8203;-` (no live bare URL or
  e-mail address, no `@name` that notifies that user, no `#123` or
  `org/repo#123` that backlinks this PR from another).
- plain_lines(text): plain_text for multi-line text (the grade section):
  redacted whole, then each line escaped the same way, the lines kept.
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
    return _visible(redact(str(text)))


def plain_text(text):
    """one_line(text) for Markdown outside a code span, with every piece of
    live syntax written as an HTML entity (_inline). WHY: a tier-1 note is
    model text steered by the repository, and a reason or a path can be
    anything the repository holds; none of it may add structure, a link or
    a notification to the body a reviewer reads."""
    return _inline(one_line(text))


def plain_lines(text):
    """plain_text for multi-line text (the grade section): `text` redacted
    WHOLE (a private key with no END line must stop at the end of the text,
    not at the end of a line), then split into lines (str.splitlines, so
    `\\r\\n` and every other break end a line, never a visible `\\r`) and
    each line escaped as plain_text escapes it (_visible, then _inline),
    joined with `\\n`. A line that was a Markdown heading before escaping
    may not be one after (`#` then a tab becomes `#\\t`): a caller that
    refuses headings checks the text before it is escaped."""
    return "\n".join(_inline(_visible(line)) for line in redact(str(text)).splitlines())


#: `www.` in any case; the `www` keeps its case (_inline).
_WWW_DOT = re.compile(r"(?i)(www)\.")

#: `#` before a digit: an issue or PR reference, `#123` or `org/repo#123`.
_ISSUE_HASH = re.compile(r"#(?=[0-9])")

#: `GH-` before a digit, in any case: GitHub's other issue reference form.
_GH_DASH = re.compile(r"(?i)\b(gh)-(?=[0-9])")

#: `/issues/`, `/pull/`, `/pulls/` or `/discussions/` before a digit: an
#: issue or PR as a URL or a bare `org/repo/issues/N` path, which GitHub's
#: reference filter links (and backlinks) after decoding entities, so the
#: `://` escape does not stop it.
_ISSUE_PATH = re.compile(r"(?i)/(issues|pulls?|discussions)/(?=[0-9])")


def _inline(text):
    """`text` (already redacted and on one line) with Markdown's live
    syntax written as HTML entities, so it reads the same and does nothing.
    sweep_pr._rendered decodes exactly these entities: keep the two in
    step.

    - `&` is `&amp;`, FIRST: GitHub decodes an entity the text already
      holds before it looks for mentions and links, so `&#64;name`,
      `&commat;name` or `&#x40;name` would otherwise be a live mention
      and `me&#64;corp.example` a live mail link. Written `&amp;#64;`, it
      shows as the text `&#64;`. First, so the entities the rules below
      write are not themselves escaped.
    - `<` is `&lt;`: it can open no HTML element (a heading, a `<details>`,
      a comment).
    - `[` is `&#91;`: it can open no link or image (a reviewer should not
      be handed a live `[click](https://...)`). WHY an entity and not
      `\\[`: a backslash the text already holds would escape that
      backslash instead (`\\[click](...)` renders a live link), while
      nothing can escape an entity's `&`.
    - `://` is `&#58;//`, and `www.` (any case) is `www&#46;`: GitHub
      autolinks a bare `https://...` or `www....` with no brackets at all.
      Only `:` before `//` is touched, so `token: x` reads as written.
    - `@` is `@&#8203;` (a zero-width space after it): GitHub autolinks a
      bare e-mail address, and `@name` is a mention that NOTIFIES that
      user (or team), so model text could ping anyone on GitHub from the
      sweep PR. WHY not `&#64;`: GitHub finds mentions in the rendered
      text, after entities are decoded, so `&#64;name` still notifies;
      `@`, a zero-width space, `name` is no mention (Renovate's escape).
    - `#` before a digit is `#&#8203;`, and `GH-` before a digit (any case)
      is `GH&#8203;-`, and `/issues/` (or `/pull/`, `/discussions/`) before
      a digit gets `&#8203;` before its last `/`: `#123`, `org/repo#123`,
      `GH-123` and an issue URL or `org/repo/issues/N` path are live issue
      references, and a cross-repository one plants a "mentioned this pull
      request" backlink in another repository's timeline. Done before any
      rule writes `&#...;`, whose `#` is before a digit too. A commit SHA
      is left as it is: it links, but notifies no one and backlinks
      nothing."""
    text = text.replace("&", "&amp;")
    text = _GH_DASH.sub(r"\1&#8203;-", _ISSUE_HASH.sub("#&#8203;", text))
    text = _ISSUE_PATH.sub(r"/\1&#8203;/", text)
    text = text.replace("<", "&lt;").replace("[", "&#91;").replace("://", "&#58;//")
    return _WWW_DOT.sub(r"\1&#46;", text).replace("@", "@&#8203;")


def _visible(text):
    """`text` with each character _escaped shows as a visible escape."""
    return "".join(_escaped(char) for char in text)


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
