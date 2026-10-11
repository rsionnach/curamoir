"""E2-R4 + E2-R5: `curamoir-lint`, the runner that turns rules into a work list.

**Who reads the output.** A fresh agent — the sweep's (E3-R1) or anyone's — that
has not read `principles.md` and should not have to. So every violation carries
its own context: where (`path:line:col`), which rule and principle, the
principle's one-sentence rule, a link to the principle's `why`, and the fix. An
agent told only "no" retries the same thing or invents something worse; an
agent told what to do instead does that. The JSON form is a versioned contract
(SCHEMA_VERSION, TOP_LEVEL_FIELDS, RECORD_FIELDS), settled before any consumer
read it (cm-9jv), and its `fingerprint` lets a later epic tell a violation
introduced from one found again.

**What it is for.** A gate in CI and pre-commit, so its exit code is the real
output and its worst failure is passing after checking nothing — which looks
exactly like a clean repo. So every way a rule can fail to check exits 2; the
one deliberate exception is a repo with no rules at all, which exits 0 with a
note on stderr, because a fresh repo is not a failing one:

    0  every rule ran and nothing met the threshold
    1  every rule ran and at least one violation met the threshold
    2  the run cannot be trusted: a rule or the config did not load, ast-grep
       is needed and absent, a rule could not search every file in its scope
       (no grammar, skipped by ast-grep, unreadable), part of the repo could
       not be listed, ast-grep failed or hung, the run outlasted --timeout,
       or the report could not be written

2 outranks 1, because "incomplete" is the more fundamental fact: a run that
skipped half its rules and found one violation has not shown the other half
clean. When rules were skipped the partial report is still printed, so the work
it did find is not lost — including what a skipped rule found in the files it
did search; for any other runner error stdout is empty and the reason is on
stderr. (The one gap: if the interpreter cannot import this module
at all, Python itself exits 1. The wrapper picks the interpreter carefully —
cm-bzb — and this module keeps to syntax that imports on 3.9 for that reason.)

**Standalone (E2-R5).** No model, no network, no cúramóir install: the import
graph is this module, `rules`, `principles` and `config` — stdlib plus PyYAML,
which rule files need — and `scripts/curamoir-lint` finds `src/` beside itself.
A test runs it from a bare copy with site-packages disabled and another with
sockets and HTTP clients booby-trapped.

**Matchers** (DEC-3). ast-grep's own `--json` output is parsed, not re-derived;
cúramóir's contribution is the agent-readable fields layered on top. Both
matchers search the same file list, computed here from the rule's
`files`/`languages`/`exclude`, so scoping cannot differ by matcher and ast-grep
is never asked about a path that does not exist (it exits 1 for that AND for
"no matches", which would make an error indistinguishable from a clean run).

**Untrusted input.** In CI the repository is often an unreviewed PR, so nothing
in it may decide what the runner reads or executes. Four consequences: Python
never starts inside it, so nothing in it can shadow a module this imports (see
`scripts/curamoir-lint` and `_return_to_invocation_directory`); only regular
files are read, so neither a symlink nor a FIFO is (see `_repository_files`);
ast-grep never sees the repo's own `sgconfig.yml` (see `_ast_grep_batch`);
and ast-grep is never found through a relative PATH entry (see
`_which_ast_grep`). Anything in it that could stall the run — a regex that
backtracks on a committed line, a file ast-grep chokes on — is bounded by
`--timeout` (see `_time_limit`). And every string it supplies that reaches
stderr — a filename, a config key, ast-grep's echo of either — is printed
through `one_line`, so a committed name cannot forge a CI log line or redraw
the terminal (see `_note`).
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from curamoir import __version__
from curamoir.config import DEFAULTS, ConfigError, load_config_text, one_line, printable
from curamoir.principles import PrincipleError, anchor, load_principles

# AST_GREP_LANGUAGES lives in `rules` because `parse_rule` refuses an ast-grep
# rule over a language it lacks; the run uses it to pick each file's `--lang`.
from curamoir.rules import (
    ALWAYS_EXCLUDED,
    AST_GREP_INSTALL,
    AST_GREP_LANGUAGES,
    ROOT_EXCLUDED,
    SEVERITIES,
    RuleError,
    glob_regex,
    load_rules,
)

EXIT_CLEAN = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

#: The JSON report's `schema_version`. The sweep (E3-R1) and the promotion
#: ladder (E4-R4) parse the report, so its shape is a contract, and this number
#: is how a consumer knows which contract it is reading. Bump it for any change
#: a parser could misread, never for an additive one; the full policy is
#: docs/rules-authoring.md, "Versioning" under "JSON".
#:
#: It is 1, not 2, although the field was called `version` before cm-9jv: no
#: release or consumer ever read that shape, and a reader of `schema_version`
#: cannot mistake the old document for a v1, since the old one has no
#: `schema_version` key at all.
SCHEMA_VERSION = 1

#: The report's top-level fields, in this order: what kind of document it is
#: and what produced it first, the verdict next, the work list last because it
#: is the long part.
TOP_LEVEL_FIELDS = (
    "schema_version", "tools", "fail_on", "complete", "rules_checked", "rules_skipped",
    "notes", "violations",
)

#: Every violation record has exactly these fields, in this order. Location
#: first because it is what an editor or agent opens, the end of the match
#: beside it; the principle's context after the message because it explains
#: the message; the fingerprint last because it is derived from the rest.
RECORD_FIELDS = (
    "location", "path", "line", "col", "end_line", "end_col", "severity", "rule_id",
    "principle_id", "principle_rule", "message", "fix", "fix_source", "why_link", "match",
    "fingerprint",
)

#: The `kind` of each entry in the report's `notes`, in the order they are
#: listed: facts about the run that are not a rule's result. An open
#: vocabulary — a consumer ignores a kind it does not know. The source of
#: truth: `report_notes` sorts by this order and refuses a kind not in it.
NOTE_KINDS = (
    "no_rules", "ast_grep_version_unknown", "symlink_not_followed", "not_regular_file",
)

#: Hex digits of sha256 kept in a fingerprint: 64 bits, so two different
#: violations in one repository's report collide with negligible probability
#: (about one in 10^10 at a hundred thousand violations), while the value
#: stays short enough to read in a diff or a PR body.
FINGERPRINT_LENGTH = 16

#: What a fingerprint ignores in the match: ASCII whitespace, every character
#: of it. Spelled out rather than `str.split()`, which also splits on Unicode
#: spaces and `\x1c`-`\x1f`, so the recipe can be reimplemented exactly.
FINGERPRINT_IGNORED = re.compile(r"[ \t\n\r\f\v]+")

#: Where the principles file is, relative to the repo, for `why_link`.
PRINCIPLES_PATH = ".curamoir/principles.md"

#: Files per ast-grep invocation. A large repo's file list would otherwise
#: exceed the OS argument-length limit, which fails the whole call.
AST_GREP_BATCH = 200

#: Set by scripts/curamoir-lint to the directory it was invoked from. The
#: wrapper starts Python in its own `src/` so that nothing in the linted repo
#: is on sys.path; `main` moves back here before resolving any path argument.
INVOKED_FROM_ENV = "CURAMOIR_LINT_INVOKED_FROM"

#: The UTF-8 byte order mark, which no editor counts as a column.
UTF8_BOM = b"\xef\xbb\xbf"

#: `--timeout`'s default, in seconds: the bound on a whole run. Ten minutes is
#: far above a healthy run (the fixture lints in under a second; a large repo
#: in tens of seconds) and well inside CI job limits, so hitting it means
#: something is stuck — and the gate then says so with exit 2, rather than
#: being killed by CI with no reason given, or hanging a pre-commit forever.
DEFAULT_TIMEOUT = 600

#: The largest `--timeout`, in seconds (about eleven days). Far beyond any real
#: run, and well inside what `setitimer` accepts: 1e300 overflowed it, which
#: surfaced as an internal error rather than as a usage error.
MAX_TIMEOUT = 1_000_000

#: The empty ast-grep config written into the scratch directory and passed as
#: `--config` (see `_ast_grep_batch`).
SCRATCH_SGCONFIG = "sgconfig.yml"

#: Lines ast-grep writes to stderr under `--inspect`, as opposed to messages.
INSPECT_PREFIX = "sg: "

#: The `--inspect=summary` line that says how many files were searched,
#: verbatim from ast-grep 0.50.0. `scannedFileCount` counts every file given,
#: including the skipped ones.
AST_GREP_SUMMARY = re.compile(
    r"sg: summary\|file: scannedFileCount=(\d+),skippedFileCount=(\d+)"
)

#: What `ast-grep --version` prints, verbatim from 0.50.0: `ast-grep 0.50.0`.
AST_GREP_VERSION = re.compile(r"ast-grep (\S+)")

#: The bound on `ast-grep --version`, in seconds, when `--timeout` allows
#: more. It answers at once when healthy, so a hang there would otherwise
#: spend the whole run's budget on a label and end in exit 2; with its own
#: short bound it degrades to a null version and a note, as documented.
AST_GREP_VERSION_TIMEOUT = 10

#: Above this size a file may be one ast-grep skips (0.50.0 searched a 3 MB
#: file and skipped a 4 MB one), so it is named as a likely cause.
AST_GREP_LIKELY_TOO_LARGE = 3_000_000

class LintError(Exception):
    """The run cannot be trusted. Always exit 2, with this message on stderr."""


class RuleIncomplete(Exception):
    """One rule could not search every file in its scope. The rule is skipped
    and named, not raised as a LintError: it cannot be called checked, but the
    other rules' work list is still worth printing. Which files exist is the
    repo's to choose, so one bad file in a PR must not blank the report.

    `matches` are what the rule found in the files it did search. They are
    real, so they are reported; the rule is still skipped, so the run still
    exits 2 — what it could not search has not been shown clean."""

    def __init__(self, reason, matches=()):
        super().__init__(reason)
        self.matches = list(matches)


class RunTimedOut(BaseException):
    """Raised by `--timeout`'s timer. A BaseException so that no
    `except Exception` on the way up — the internal-error handler included —
    can absorb it and let the run carry on past its bound."""


@dataclass(frozen=True)
class Violation:
    """One violation, with everything an agent needs to act on it.

    `line`/`col` are 1-based; `end_line`/`end_col` are 1-based and exclusive,
    the position just after the match's last character (SARIF's convention,
    and ast-grep's 0-based exclusive end plus one). A `target: path` match is
    in the filename, not the file, so it is the empty region at 1:1."""

    path: str
    line: int
    col: int
    end_line: int
    end_col: int
    severity: str
    rule_id: str
    principle_id: str
    principle_rule: str
    message: str
    fix: str
    fix_source: str
    why_link: str
    match: str
    fingerprint: str

    def record(self):
        """The JSON record: RECORD_FIELDS, in order."""
        values = dict(self.__dict__, location=f"{self.path}:{self.line}:{self.col}")
        return {name: values[name] for name in RECORD_FIELDS}


@dataclass(frozen=True)
class Skipped:
    """A rule that did not run, and why. Its presence makes the run incomplete."""

    rule_id: str
    reason: str


@dataclass(frozen=True)
class LintResult:
    violations: tuple = ()
    checked: tuple = ()
    skipped: tuple = ()
    #: Repo-relative paths of symlinks that were not followed. Reported on
    #: stderr and as `notes` in the JSON, not as violations: they are not a
    #: rule's result.
    symlinks: tuple = ()
    #: Repo-relative paths of entries that are neither files, directories nor
    #: symlinks (FIFOs, sockets, devices). Reported like symlinks, for the
    #: same reason: git cannot store them, so they are not repository content.
    not_regular: tuple = ()
    #: The version of the ast-grep that ran this run's ast-grep rules, as it
    #: prints it; None when none ran, or when it could not be read, in which
    #: case `ast_grep_version_problem` says why.
    ast_grep_version: Optional[str] = None
    ast_grep_version_problem: Optional[str] = None

    @property
    def complete(self):
        return not self.skipped


def run_lint(repo, ast_grep=None, ast_grep_timeout=DEFAULT_TIMEOUT):
    """Run every rule in `repo` and return what they found.

    `ast_grep` is the path to the ast-grep binary, or None when it is absent —
    passed in rather than looked up so a caller (or a test) decides what is
    installed. `ast_grep_timeout` bounds each ast-grep call, in seconds (None:
    unbounded). Raises LintError for anything that makes the result
    untrustworthy other than what one rule alone could not check, which skips
    that rule and is reported.
    """
    repo = Path(repo)
    if not repo.is_dir():
        raise LintError(f"{repo} is not a directory; pass the repository root with --repo")
    try:
        rules = load_rules(repo)
        principles = load_principles(repo)
    except (RuleError, PrincipleError) as invalid:
        raise LintError(str(invalid)) from invalid

    files, symlinks, not_regular = _repository_files(repo)
    found, checked, skipped = [], [], []
    # (version, problem) once asked; None until then.
    ast_grep_answer = None
    for rule in sorted(rules, key=lambda r: r.id):
        scoped = _scoped(files, rule)
        reason = _why_rule_cannot_run(rule, scoped, ast_grep)
        if reason is not None:
            skipped.append(reason)
            continue
        if rule.matcher == "ast-grep" and ast_grep_answer is None:
            # Asked only once an ast-grep rule is about to run: a report
            # names the version that produced its results, and a run of regex
            # rules neither uses ast-grep nor should depend on it answering.
            ast_grep_answer = _ast_grep_version(
                ast_grep, _version_probe_timeout(ast_grep_timeout))
        complete = True
        try:
            if rule.matcher == "ast-grep":
                matches = _ast_grep_matches(repo, rule, scoped, ast_grep, ast_grep_timeout)
            else:
                matches = _regex_matches(repo, rule, scoped)
        except RuleIncomplete as incomplete:
            skipped.append(Skipped(rule.id, str(incomplete)))
            matches, complete = incomplete.matches, False
        # load_rules has already resolved the id, so this cannot be None.
        principle = principles.by_id(rule.principle)
        for path, line, col, end_line, end_col, text in matches:
            found.append(dict(
                path=path, line=line, col=col, end_line=end_line, end_col=end_col,
                severity=rule.severity,
                rule_id=rule.id,
                principle_id=rule.principle,
                principle_rule=principle.rule,
                message=rule.render(path=path, line=line, col=col, match=text),
                # A rule may have no fix; the principle's `good` example is a
                # corrected form by definition, and an empty fix is the one
                # thing guaranteed to send the agent round again.
                fix=rule.fix or principle.good,
                fix_source="rule" if rule.fix else "principle",
                why_link=f"{PRINCIPLES_PATH}#{anchor(rule.principle)}",
                match=text,
            ))
        if complete:
            checked.append(rule.id)

    version, version_problem = ast_grep_answer or (None, None)
    return LintResult(_fingerprinted(found), tuple(checked), tuple(skipped), tuple(symlinks),
                      tuple(not_regular), version, version_problem)


def _fingerprinted(found):
    """`found` (one dict of Violation fields per match, fingerprint aside) as
    Violations in report order, each with its fingerprint.

    The order is fixed before any occurrence index is assigned because the
    index IS a position: "the Nth identical match in this file, in report
    order". Assigned in the order the matchers happened to report, the same
    tree could fingerprint differently between runs. Path, start and rule are
    the documented order; end and match only break the ties one rule can
    produce (two ast-grep matches starting at the same node).
    """
    ordered = sorted(found, key=lambda v: (v["path"], v["line"], v["col"], v["rule_id"],
                                           v["end_line"], v["end_col"], v["match"]))
    violations, occurrences = [], {}
    for fields in ordered:
        key = _fingerprint_key(fields["rule_id"], fields["path"], fields["match"])
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        violations.append(Violation(
            **fields,
            fingerprint=fingerprint(fields["rule_id"], fields["path"], fields["match"],
                                    occurrence),
        ))
    return tuple(violations)


def _fingerprint_key(rule_id, path, match):
    """What a fingerprint identifies a violation by, occurrence aside: each
    value as the JSON record shows it, so a consumer can recompute it from the
    record, and the match without its whitespace."""
    return printable(rule_id), printable(path), _normalised(printable(match))


def fingerprint(rule_id, path, match, occurrence):
    """A violation's identity across runs: the same violation in a later
    report has the same value, so E4-R4 can tell a violation introduced from
    one merely found again, and a second sweep (E3-R7) can tell it has seen
    one before.

    The recipe, which docs/rules-authoring.md states and a golden test pins:
    the four fields `rule_id`, `path`, the match with every ASCII whitespace
    character removed, and `occurrence` in decimal — each as it appears in the
    JSON record, so a consumer can recompute it from the record — are each
    written as `<UTF-8 byte length>:<UTF-8 bytes>,`, concatenated, hashed
    with sha256, and the first FINGERPRINT_LENGTH hex digits kept. Length
    prefixes, not a separator, so no field's content can shift into another.

    What it survives: the violation moving to another line or column (code
    inserted above it), and whitespace-only reformatting of the matched text
    (re-indenting, re-wrapping, `f(a,b)` to `f(a, b)`). What changes it: a
    different rule, path or non-whitespace match text — so renaming or moving
    the file is a new fingerprint, as is any edit inside the match.

    `occurrence` tells identical matches in one file apart: the 0-based
    index of this one among the violations with the same rule, path and
    normalised match, in report order. Its limit: deleting an earlier identical
    match shifts every later one down, so the last of them reads as gone and
    the others as unchanged, though it was the first that went. Location
    cannot break the tie instead without losing the survival of line moves.
    """
    fields = (*_fingerprint_key(rule_id, path, match), str(occurrence))
    canonical = b"".join(
        b"%d:%s," % (len(encoded), encoded)
        for encoded in (field.encode("utf-8") for field in fields)
    )
    return hashlib.sha256(canonical).hexdigest()[:FINGERPRINT_LENGTH]


def _normalised(match):
    return FINGERPRINT_IGNORED.sub("", match)


def _ast_grep_version(binary, timeout):
    """(version, problem): what `ast-grep --version` says, e.g. "0.50.0", or
    None and why it could not be read.

    Never a LintError. The version is provenance, not evidence: every ast-grep
    call's own output is checked — its exit code, its `--inspect` summary, its
    JSON — whatever version it claims, so a label that cannot be read does not
    make the results less trustworthy, and turning a clean run into exit 2
    over it would fail the gate on something that is not a finding. An
    ast-grep that cannot run at all fails on the rule's own call just after.

    Run like every other call (see `_ast_grep_batch`): from a scratch
    directory outside the repository, in its own process group, but bounded
    by `_version_probe_timeout`, so a hang here costs seconds and a note
    rather than the run. RunTimedOut is a BaseException and passes through.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="curamoir-lint-") as scratch:
            returncode, stdout, stderr = _run_ast_grep([binary, "--version"], scratch, timeout)
    except subprocess.TimeoutExpired:
        return None, f"`ast-grep --version` did not finish within {timeout:g} seconds"
    except OSError as failure:
        return None, f"could not run `ast-grep --version` ({binary}): {failure}"
    printed = AST_GREP_VERSION.fullmatch(stdout.strip())
    if returncode == 0 and printed is not None:
        return printed.group(1), None
    # Name the stream quoted: a version on stderr is still not one on stdout,
    # and a reader should not be told it was "printed" where it was not.
    if stdout.strip():
        said = f"printed {stdout.strip()[:200]!r} on stdout"
    else:
        said = f"printed nothing on stdout; stderr: {stderr.strip()[:200]!r}"
    return None, f"`ast-grep --version` exited {returncode} and {said}"


def _version_probe_timeout(ast_grep_timeout):
    """AST_GREP_VERSION_TIMEOUT, or `ast_grep_timeout` when that is shorter.

    Its limit: the run's whole budget is not tracked here, so with
    `--timeout` at or under AST_GREP_VERSION_TIMEOUT a hanging probe can
    still use all of it, and the run ends in exit 2 as for any other hang.
    """
    if ast_grep_timeout is None:
        return AST_GREP_VERSION_TIMEOUT
    return min(AST_GREP_VERSION_TIMEOUT, ast_grep_timeout)


def _why_rule_cannot_run(rule, scoped, ast_grep):
    """A Skipped saying why `rule` cannot honestly run over `scoped`, or None.

    Both reasons are ast-grep's: it is absent, or the rule's scope includes a
    file it has no grammar for. Either way the rule is skipped and named, as
    for RuleIncomplete, which covers what only running the rule can find out.
    """
    if rule.matcher != "ast-grep":
        return None
    if ast_grep is None:
        return Skipped(
            rule.id,
            f"ast-grep is not installed, so this ast-grep rule did not run. "
            f"Install it: {AST_GREP_INSTALL}",
        )
    unsearchable = [path for path in scoped if _ast_grep_language(path) is None]
    if not unsearchable:
        return None
    shown = ", ".join(unsearchable[:5])
    more = f" and {len(unsearchable) - 5} more" if len(unsearchable) > 5 else ""
    return Skipped(
        rule.id,
        f"its scope includes files ast-grep has no grammar for here "
        f"({shown}{more}), so it did not run. Narrow `files:` or add "
        "them to `exclude:`; for prose or config, write a `regex` rule.",
    )


def _at_or_above(severity, fail_on):
    """Whether a violation of `severity` meets the `fail_on` threshold."""
    return SEVERITIES.index(severity) >= SEVERITIES.index(fail_on)


def exit_code(result, fail_on):
    """The gate. See the module docstring for why 2 outranks 1."""
    if not result.complete:
        return EXIT_ERROR
    if any(_at_or_above(v.severity, fail_on) for v in result.violations):
        return EXIT_VIOLATIONS
    return EXIT_CLEAN


def format_json(result, fail_on):
    """The report, in TOP_LEVEL_FIELDS order. Strings pass through `printable`
    and nothing else: exact, with control characters as JSON escapes, except
    a filename that is not UTF-8, whose bad bytes are shown escaped."""
    document = {
        "schema_version": SCHEMA_VERSION,
        # The tools whose behaviour the results depend on. Only ast-grep's
        # version varies by machine in a way that can change a result.
        "tools": {"ast-grep": _printable_or_none(result.ast_grep_version)},
        "fail_on": fail_on,
        "complete": result.complete,
        "rules_checked": [printable(rule_id) for rule_id in result.checked],
        "rules_skipped": [{"rule_id": printable(s.rule_id), "reason": printable(s.reason)}
                          for s in result.skipped],
        "notes": [{name: printable(value) for name, value in note.items()}
                  for note in report_notes(result)],
        "violations": [
            {name: printable(value) if isinstance(value, str) else value
             for name, value in v.record().items()}
            for v in result.violations
        ],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def report_notes(result):
    """The run's notes, as the JSON lists them: facts that are not a rule's
    result but that a reader of the report should know — the same facts the
    stderr notes give, in NOTE_KINDS order and then path order.

    Sorted by NOTE_KINDS rather than trusted to the order of the statements
    below, so the documented order cannot drift from the emitted one. The sort
    is stable, so within a kind the paths keep the walk's sorted order. A kind
    missing from NOTE_KINDS is a bug here, raised rather than emitted: it
    would be a note no documentation describes."""
    found = []
    if not result.checked and not result.skipped:
        found.append({"kind": "no_rules"})
    if result.ast_grep_version_problem:
        found.append({"kind": "ast_grep_version_unknown",
                      "reason": result.ast_grep_version_problem})
    found += [{"kind": "symlink_not_followed", "path": path} for path in result.symlinks]
    found += [{"kind": "not_regular_file", "path": path} for path in result.not_regular]
    unknown = sorted({note["kind"] for note in found} - set(NOTE_KINDS))
    if unknown:
        raise ValueError(f"note kind(s) {unknown} are not in NOTE_KINDS")
    return sorted(found, key=lambda note: NOTE_KINDS.index(note["kind"]))


def _printable_or_none(text):
    return None if text is None else printable(text)


def format_human(result, fail_on):
    """`path:line:col: severity [principle/rule] message`, then the context.

    The first line is the shape compilers use, so editors and terminals that
    linkify locations do it here too. Every field is shown on one line, with
    control characters escaped: a match spanning lines, rendered into the
    message, must not forge a `fix:` line an agent would read as the runner's.
    """
    lines = []
    for v in result.violations:
        lines.append(one_line(f"{v.path}:{v.line}:{v.col}: {v.severity} "
                              f"[{v.principle_id}/{v.rule_id}] {v.message}"))
        lines.append(f"    rule: {one_line(v.principle_rule)}")
        lines.append(f"    why:  {one_line(v.why_link)}")
        lines.append(f"    fix:  {one_line(v.fix)}")
    failing = sum(1 for v in result.violations if _at_or_above(v.severity, fail_on))
    noun = "violation" if len(result.violations) == 1 else "violations"
    # A skipped rule's violations are listed too (see RuleIncomplete), so the
    # count of rules that ran is not the count the violations came from.
    skipped = f"; {len(result.skipped)} rule(s) skipped" if result.skipped else ""
    lines.append(
        f"{len(result.violations)} {noun} from {len(result.checked)} rule(s){skipped}; "
        f"{failing} at or above {fail_on!r}"
    )
    return "\n".join(lines) + "\n"


def _note(message):
    """Write one line to stderr. Every stderr line goes through here, because
    nearly every message quotes the repository — a filename, a config key,
    ast-grep's echo of a path — and `one_line` escapes it, so a name holding a
    newline or an escape sequence cannot forge a CI log line or redraw the
    terminal."""
    print(f"curamoir-lint: {one_line(message)}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="curamoir-lint",
        # Options are a gate's contract: `--re` must not quietly mean `--repo`,
        # nor change meaning when a later option shares its prefix.
        allow_abbrev=False,
        description=(
            "Run .curamoir/rules/*.yaml against a repository. Exit 0 clean, 1 "
            "violations at or above the threshold, 2 the run cannot be trusted."
        ),
    )
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument(
        "--format", choices=("human", "json"), default="human",
        help="human: one block per violation, for a person or an editor (the "
             "default); json: the machine-readable report, a versioned contract "
             "described in docs/rules-authoring.md",
    )
    parser.add_argument(
        "--fail-on", choices=SEVERITIES, default=None,
        help="lowest severity that fails the run (default: rules.fail_on, else error)",
    )
    parser.add_argument(
        "--timeout", type=_seconds, default=DEFAULT_TIMEOUT, metavar="SECONDS",
        help=f"exit 2 if the run takes longer (default: {DEFAULT_TIMEOUT}; 0: no bound)",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {__version__} (JSON schema {SCHEMA_VERSION})",
        help="print the tool version and the JSON schema_version, and exit 0",
    )
    args = parser.parse_args(argv)
    if args.repo == "":
        # An empty path is the working directory to `Path`, which is not what
        # anyone who typed `--repo "$UNSET_VARIABLE"` meant to lint.
        parser.error("--repo must not be empty")

    try:
        with _time_limit(args.timeout):
            _return_to_invocation_directory()
            fail_on = args.fail_on or _configured_threshold(Path(args.repo))
            result = run_lint(args.repo, ast_grep=_which_ast_grep(),
                              ast_grep_timeout=args.timeout or None)
            render = format_json if args.format == "json" else format_human
            report = render(result, fail_on)
        # Written after the timer is cancelled, so an alarm cannot cut the
        # report off half-way (leaving truncated JSON where an error run
        # promises nothing), and still inside the guard, so a report that
        # cannot be written is a 2 like any other failure, not Python's 1.
        # The cost: a reader that stops reading blocks this write unbounded.
        sys.stdout.write(report)
        sys.stdout.flush()
    except RunTimedOut:
        _note(
            f"the run did not finish within {args.timeout:g} seconds "
            "(--timeout), so nothing it found can be trusted. A rule whose regex "
            "backtracks on some line, or a very large repository, are the likely "
            "causes; raise --timeout for the second."
        )
        return EXIT_ERROR
    except LintError as failure:
        _note(str(failure))
        return EXIT_ERROR
    except BrokenPipeError:
        # The reader went away (`| head`). Point stdout at /dev/null so the
        # flush Python attempts at shutdown does not print a traceback, and
        # exit 2: whoever wanted the result did not get it.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        _note("stdout was closed before the report was written")
        return EXIT_ERROR
    except UnicodeEncodeError as unencodable:
        _note(
            f"the report could not be written in stdout's encoding "
            f"({unencodable.encoding}); set PYTHONIOENCODING=utf-8"
        )
        return EXIT_ERROR
    except Exception as unexpected:
        # Python exits 1 on an uncaught exception, which is this tool's code for
        # "violations found". A crash must not read as a lint result.
        _note(f"internal error: {unexpected!r}")
        return EXIT_ERROR

    if not result.checked and not result.skipped:
        _note(
            "no rules in .curamoir/rules/, so nothing was checked. "
            "See docs/rules-authoring.md to write one."
        )
    if result.ast_grep_version_problem:
        _note(f"could not read ast-grep's version, so the report names none: "
              f"{result.ast_grep_version_problem}")
    for skipped in result.skipped:
        _note(f"skipped {skipped.rule_id}: {skipped.reason}")
    if result.symlinks:
        # Names only, never targets or contents. Not an incomplete run: a
        # symlink is not repository content, and its target, if it is in the
        # repo, is linted at its own path.
        _note(f"did not follow {len(result.symlinks)} symlink(s): "
              + ", ".join(result.symlinks))
    if result.not_regular:
        # Not an incomplete run either: git cannot store a FIFO, socket or
        # device, so none is repository content — and opening a FIFO blocks.
        _note(f"did not read {len(result.not_regular)} item(s) that are not "
              "regular files: " + ", ".join(result.not_regular))
    return exit_code(result, fail_on)


def _seconds(text):
    """`--timeout`'s value: a number of seconds from 0 to MAX_TIMEOUT."""
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not (math.isfinite(value) and 0 <= value <= MAX_TIMEOUT):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number of seconds from 0 to {MAX_TIMEOUT}")
    return value


@contextmanager
def _time_limit(seconds):
    """Raise RunTimedOut in this thread if the block runs longer than `seconds`.

    A signal is the only bound there is on a rule's regex: Python's regex
    engine cannot be cancelled from inside, and it checks for signals as it
    backtracks. So this is POSIX-only (SIGALRM, via setitimer) and main-thread
    only. Elsewhere — Windows, or `main` called from a worker thread — the run
    has no overall bound and only each ast-grep call is limited, by its
    subprocess timeout. An alarm arriving during an ast-grep call kills the
    child's whole process group on its way out (see `_run_ast_grep`).
    """
    usable = (
        seconds
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not usable:
        yield
        return

    def expire(signum, frame):
        raise RunTimedOut()

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _return_to_invocation_directory():
    """Move to where scripts/curamoir-lint was invoked, if it says.

    The wrapper starts Python in its own `src/` so that `python -m` does not
    put the linted repo at sys.path[0] (see the wrapper). Moving back is safe
    only if no sys.path entry is relative, since a relative entry would now
    resolve inside the repo for any later import (PyYAML is imported lazily).
    `-m` makes its own entry absolute, but `''` or a relative PYTHONPATH entry
    under `-S` stays relative — so every entry is pinned to where Python
    started first. The variable is removed so ast-grep does not inherit it.
    """
    invoked_from = os.environ.pop(INVOKED_FROM_ENV, None)
    if not invoked_from:
        return
    sys.path[:] = [os.path.abspath(entry) for entry in sys.path]
    try:
        os.chdir(invoked_from)
    except OSError as failure:
        raise LintError(f"cannot return to {invoked_from}: {failure}") from failure


def _configured_threshold(repo):
    """`rules.fail_on` from the repo's config, or the default.

    Strict where `repo_config.effective_config` is tolerant: a hook should keep
    working on a broken config, but a gate silently reverting to the default
    threshold is a gate loosened without anybody being told.
    """
    path = repo / ".curamoir" / "curamoir.yaml"
    if not os.path.lexists(path):
        return DEFAULTS["rules"]["fail_on"]
    if not path.is_file():
        # A directory, a FIFO or a dangling symlink: something is there, and
        # whatever threshold it was meant to set cannot be read.
        raise LintError(f"{path} exists but is not a readable file, so the threshold "
                        "it may set cannot be read")
    try:
        config = load_config_text(path.read_text(encoding="utf-8"))
    except ConfigError as invalid:
        # One line, like every stderr note; ConfigError's own str lists the
        # problems one per line.
        raise LintError(f"{path}: invalid: " + "; ".join(invalid.problems)) from invalid
    except OSError as invalid:
        raise LintError(f"{path}: {invalid}") from invalid
    except Exception as invalid:  # a YAML syntax error is PyYAML's own type
        raise LintError(f"{path}: not valid YAML: {invalid}") from invalid
    return config["rules"]["fail_on"]


def _which_ast_grep():
    """The ast-grep binary on PATH, considering absolute entries only.

    A relative entry (`.`, `bin`, or the empty entry, which means `.`) is
    resolved against the working directory, which is usually the repository
    being linted — so it would let a PR supply the binary its own rules are
    checked with. Only `ast-grep`, never `sg`: on Linux `sg` is usually
    shadow-utils' set-group command, and running it with a pattern would be a
    surprise.
    """
    entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    return shutil.which("ast-grep", path=os.pathsep.join(e for e in entries if os.path.isabs(e)))


def _repository_files(repo):
    """(files, symlinks, not_regular) under `repo`, each sorted repo-relative
    POSIX paths.

    A plain walk rather than `git ls-files`, so the runner works on an export,
    a CI checkout without history, or a directory that is not a repo at all.
    ALWAYS_EXCLUDED is pruned at any depth and ROOT_EXCLUDED at the root,
    whatever a rule says; .gitignore is not read, so a generated directory
    outside those lists is searched.

    Only regular files are linted. Symlinks are returned apart: `match` quotes
    matched text, so a PR committing `leak.py -> ~/.ssh/id_rsa` would otherwise
    have CI print whatever of the target a rule matched; and ast-grep reads a
    symlink it is named. os.walk already declines to descend a directory
    symlink but does list file symlinks, so every entry is lstat'ed here.
    FIFOs, sockets and devices are returned apart too: opening a FIFO blocks
    until something writes to it, which in CI is never. Only the walk is
    guarded: the repo root itself may be a symlink (the caller chose it), and
    an entry swapped between this walk and the read is out of scope — a CI
    checkout does not change under the runner.

    A directory that cannot be listed is a LintError, not a pruned subtree:
    every file under it would go unchecked by every rule.
    """
    found, symlinks, not_regular, unreadable = [], [], [], []
    for directory, subdirectories, names in os.walk(repo, onerror=unreadable.append):
        base = Path(directory).relative_to(repo)
        kept = []
        for name in sorted(subdirectories):
            if name in ALWAYS_EXCLUDED or (base == Path(".") and name in ROOT_EXCLUDED):
                continue
            kind = _kind(os.path.join(directory, name), unreadable)
            if kind == stat.S_IFDIR:
                kept.append(name)
            elif kind == stat.S_IFLNK:
                symlinks.append((base / name).as_posix())
        subdirectories[:] = kept
        for name in names:
            kind = _kind(os.path.join(directory, name), unreadable)
            if kind == stat.S_IFREG:
                found.append((base / name).as_posix())
            elif kind == stat.S_IFLNK:
                symlinks.append((base / name).as_posix())
            elif kind is not None:
                not_regular.append((base / name).as_posix())
    if unreadable:
        shown = "; ".join(
            f"{os.path.relpath(failure.filename, repo) if failure.filename else repo}: "
            f"{failure.strerror or failure}"
            for failure in unreadable[:5]
        )
        more = f" and {len(unreadable) - 5} more" if len(unreadable) > 5 else ""
        raise LintError(
            f"could not list part of the repository ({shown}{more}), so no rule "
            "could check the files there. Make it readable, or lint from a "
            "checkout that is."
        )
    return sorted(found), sorted(symlinks), sorted(not_regular)


def _kind(path, unreadable):
    """The file type bits of `path`, not following a symlink; None (and the
    error recorded in `unreadable`) if it cannot be stat'ed."""
    try:
        return stat.S_IFMT(os.lstat(path).st_mode)
    except OSError as failure:
        unreadable.append(failure)
        return None


def _scoped(files, rule):
    """The files `rule` applies to: its patterns, minus its exclusions.

    For an ast-grep rule, also minus any file whose whole name is a suffix
    (`.py`): it fully matches `**/*.py`, but has no suffix of its own, so it
    would read as a file with no grammar and skip the rule — exit 2, at the
    will of any commit. Leaving it out loses nothing: named explicitly under
    `--lang=python`, ast-grep 0.50.0 does not search it either. A regex rule
    keeps it, since a regex can search anything.
    """
    include = [glob_regex(pattern) for pattern in rule.file_patterns()]
    exclude = [glob_regex(pattern) for pattern in rule.exclude]
    return [
        path for path in files
        if any(p.fullmatch(path) for p in include)
        and not any(p.fullmatch(path) for p in exclude)
        and not (rule.matcher == "ast-grep" and _is_bare_suffix(path))
    ]


def _is_bare_suffix(path):
    """Whether the file's whole name is a mapped suffix, such as `.py`."""
    return Path(path).name in AST_GREP_LANGUAGES


def _ast_grep_language(path):
    """The `--lang` for `path`, or None when ast-grep has no grammar for it."""
    return AST_GREP_LANGUAGES.get(Path(path).suffix)


def _regex_matches(repo, rule, files):
    """(path, line, col, end_line, end_col, text) for every regex match,
    1-based, the end exclusive."""
    compiled = rule.compiled()
    if rule.target == "path":
        # A filename has no line; 1:1 is the file itself, which is what an
        # editor should open.
        matches = []
        for path in files:
            found = compiled.search(path)
            if found:
                # The empty region at 1:1: the match is in the name, not in
                # any span of the file's text an editor could select.
                matches.append((path, 1, 1, 1, 1, found.group(0)))
        return matches
    matches, unreadable = [], []
    for path in files:
        try:
            lines = _lines(repo, path)
        except OSError as failure:
            unreadable.append(f"{path} ({failure.strerror or failure})")
            continue
        for number, line in enumerate(lines, start=1):
            for found in compiled.finditer(line):
                # One line, without its newline, is all a match can see (and
                # a pattern needing a newline is refused at load), so it ends
                # on the line it starts on. `end() + 1`: 1-based, exclusive.
                matches.append((path, number, found.start() + 1, number, found.end() + 1,
                                found.group(0)))
    if unreadable:
        raise RuleIncomplete(
            f"it could not read {len(unreadable)} file(s) in its scope "
            f"({', '.join(unreadable)}), so it did not check them. Make them "
            "readable, or add them to `exclude:`.",
            matches,
        )
    return matches


def _lines(repo, path):
    """The file's lines as an editor and ast-grep number them.

    Only `\n` ends a line. `read_text` would turn a lone `\r` into a break
    (universal newlines) and `str.splitlines` also breaks on form feed,
    vertical tab, `\x1c`-`\x1e`, U+0085, U+2028 and U+2029 — so a form feed on
    its own line, or a U+2028 in a string literal, put every later violation
    on the wrong line. A trailing `\r` is dropped from each line, so CRLF
    files read as their lines; a lone `\r` stays a character. `utf-8-sig`
    drops a leading BOM, which no editor shows as a column. Columns are
    1-based characters (code points), as ast-grep's are. Raises OSError for a
    file that cannot be read.
    """
    # `replace`: one undecodable byte should not hide the rest of the file
    # from the rule, and must not crash the run.
    with open(repo / path, encoding="utf-8-sig", errors="replace", newline="") as handle:
        text = handle.read()
    lines = text.split("\n")
    if lines[-1] == "":
        # After a final newline (or in an empty file) there is no further line.
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _ast_grep_matches(repo, rule, files, binary, timeout):
    """(path, line, col, end_line, end_col, text) from ast-grep's own JSON,
    converted to 1-based (the end stays exclusive).

    Exit 0 is matches and exit 1 is none — like grep — and anything else is a
    failure. But exit codes alone are not enough: ast-grep exits 1 for a path
    that does not exist and 0 for a pattern it could not parse (with a warning
    naming an "ERROR node"), and both would otherwise read as a clean run. So
    "ERROR" in a stderr message is a failure too, with ast-grep's own words
    quoted. And ast-grep skips, silently, a file it cannot read, cannot decode
    as UTF-8 or finds too large (all verified, 0.50.0): `[]`, exit 1, nothing
    on stderr. Only `--inspect=summary` counts them, so it is always asked for
    and a skipped file makes the rule incomplete (RuleIncomplete).

    Files are grouped by language so `--lang` can be explicit (see
    AST_GREP_LANGUAGES). `run_lint` has already skipped any rule whose scope
    includes a file with no mapping, so every file here has one.

    Empty files are not passed: ast-grep 0.50.0 counts an empty file as
    skipped (verified: an empty `__init__.py`), and it can hold no match, so
    leaving it out keeps "skipped" meaning "not searched".
    """
    by_language = {}
    for path in files:
        if not _is_empty(repo / path):
            by_language.setdefault(_ast_grep_language(path), []).append(path)
    matches, unsearched = [], []
    with tempfile.TemporaryDirectory(prefix="curamoir-lint-") as scratch:
        (Path(scratch) / SCRATCH_SGCONFIG).write_text("ruleDirs: []\n", encoding="utf-8")
        for language, paths in sorted(by_language.items()):
            for start in range(0, len(paths), AST_GREP_BATCH):
                found, reason = _ast_grep_batch(
                    repo, rule, binary, language, paths[start:start + AST_GREP_BATCH],
                    scratch=scratch, timeout=timeout,
                )
                matches.extend(found)
                if reason:
                    unsearched.append(reason)
    if unsearched:
        # Every batch is still run: what the others find is reported too.
        raise RuleIncomplete(
            "; ".join(unsearched) + ", so this rule did not search its whole scope. "
            "ast-grep skips files it cannot read, cannot decode as UTF-8, or finds "
            "too large: fix them, or add them to `exclude:`.",
            matches,
        )
    return matches


def _is_empty(path):
    """Whether `path` is a file of zero bytes. One that cannot be stat'ed is
    not known to be empty, so it is searched, and ast-grep's skip names it."""
    try:
        return os.stat(path).st_size == 0
    except OSError:
        return False


def _ast_grep_batch(repo, rule, binary, language, batch, scratch, timeout):
    """(matches, unsearched) from one ast-grep run over files of one language:
    (path, line, col, end_line, end_col, text) for each match, and why not
    every file was searched, or None.

    ast-grep must never read the repo's own configuration. It looks for
    `sgconfig.yml` in its working directory and every ancestor, and `run`
    honours what it finds. Verified against 0.50.0, with the file in the cwd:
    `languageGlobs: {javascript: ['*.py']}` made a `--lang=python` search
    return `[]`, exit 1 — a rule silently checking nothing; an unparseable file
    failed the run (exit 8); and `customLanguages` with a `libraryPath` was
    only skipped because 0.50.0 ignores native libraries unless given
    `--custom-languages allow` — a version without that guard loads them,
    which is a PR running native code in CI. Two defences, each sufficient on
    0.50.0 and kept together because neither depends on the other:

    - `--config` names cúramóir's own empty config (SCRATCH_SGCONFIG).
      Verified: with it, an unparseable `sgconfig.yml` in the cwd is not read.
    - The cwd is `scratch`, a fresh temp directory outside the repo, so
      ancestor discovery finds nothing of the repo's; files are therefore
      named absolutely. Verified: from a neutral cwd the same unparseable
      repo config is not read. ast-grep echoes each path as given, and they
      are converted back to repo-relative POSIX paths below.
    """
    root = repo.resolve()
    command = [
        binary, "run", f"--config={Path(scratch) / SCRATCH_SGCONFIG}",
        f"--pattern={rule.pattern}",
        f"--lang={language}", "--json=compact", "--inspect=summary", "--",
        *(str(root / path) for path in batch),
    ]
    try:
        returncode, stdout, stderr = _run_ast_grep(command, scratch, timeout)
    except subprocess.TimeoutExpired as hung:
        raise LintError(
            f"rule {rule.id!r}: ast-grep did not finish within {timeout:g} seconds on "
            f"{len(batch)} {language} file(s), from {batch[0]} to {batch[-1]}"
        ) from hung
    except OSError as failure:
        raise LintError(f"rule {rule.id!r}: could not run ast-grep ({binary}): {failure}") \
            from failure
    inspect, messages = [], []
    for line in stderr.splitlines():
        (inspect if line.startswith(INSPECT_PREFIX) else messages).append(line)
    # Inspect lines echo paths the runner does not choose (the scratch
    # directory's), so only ast-grep's own messages are searched for ERROR.
    message = "\n".join(messages).strip()
    if returncode not in (0, 1) or "ERROR" in message:
        raise LintError(
            f"rule {rule.id!r} ({rule.source}): ast-grep failed "
            f"(exit {returncode}): {message or 'no message'}. "
            "If the pattern is the problem, the ast-grep playground shows how "
            "it parses: https://ast-grep.github.io/playground.html"
        )
    unsearched = _unsearched(root, rule, batch, inspect)
    matches = []
    for record in _ast_grep_records(rule, stdout):
        path = _repo_relative(root, rule, record["file"])
        start, end = record["range"]["start"], record["range"]["end"]
        line, col = start["line"] + 1, start["column"] + 1
        # ast-grep's end is 0-based and exclusive, in characters (verified,
        # 0.50.0: `print("h\u00e9llo")` at column 0 ends at column 14), so plus
        # one is the 1-based exclusive end the regex matcher reports.
        end_line, end_col = end["line"] + 1, end["column"] + 1
        if line == 1 and col > 1 and _starts_with_bom(root / path):
            # ast-grep 0.50.0 counts a UTF-8 BOM as the first character of line
            # 1 (verified: `\ufeffprint(1)` is reported at column 1, 0-based),
            # where editors and the regex matcher do not. Corrected so both
            # matchers, and the editor, agree. This assumes 0.50.0's behaviour;
            # a version that stopped counting the BOM would be over-corrected,
            # which the real-binary test of matching locations would catch.
            # The end shares the shift when it is on line 1 too.
            col -= 1
            if end_line == 1:
                end_col -= 1
        matches.append((path, line, col, end_line, end_col, record["text"]))
    return matches, unsearched


def _run_ast_grep(command, cwd, timeout):
    """(returncode, stdout, stderr) of one ast-grep call, raising
    subprocess.TimeoutExpired after `timeout` seconds.

    ast-grep runs in a session of its own, and any exception here — the
    timeout, `--timeout`'s RunTimedOut, a Ctrl-C — kills its whole process
    group. `subprocess.run` kills only the child, and an ast-grep installed as
    a wrapper script (npm's, say) does its work in a grandchild, which would
    run on after the gate had exited. The limit: an alarm landing inside
    `Popen` itself, between the fork and its return, still leaves the child,
    as nothing here yet knows its pid. Windows has no process groups; there
    only the child is killed.
    """
    with subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            _kill_process_group(process)
            raise
    return process.returncode, stdout, stderr


def _kill_process_group(process):
    if hasattr(os, "killpg"):
        # With start_new_session the child leads its own group, whose id is
        # its pid. OSError: the group is already gone, so nothing is left.
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()
    process.wait()


def _unsearched(root, rule, batch, inspect):
    """Why ast-grep's summary says it did not search every file in `batch`, or
    None if it did; LintError if there is no summary to go on."""
    summaries = [found for found in map(AST_GREP_SUMMARY.fullmatch, inspect) if found]
    if len(summaries) != 1:
        raise LintError(
            f"rule {rule.id!r}: ast-grep did not report how many files it searched "
            "(`--inspect=summary`), so a file it skipped would read as one with no "
            "matches. curamoir-lint is verified against ast-grep 0.50.0."
        )
    scanned, skipped = (int(count) for count in summaries[0].groups())
    unsearched = skipped + max(0, len(batch) - scanned)
    if not unsearched:
        return None
    named = _likely_skipped(root, batch, unsearched)
    if len(named) < unsearched:
        named.append("ast-grep does not say which" + (" others" if named else ""))
    return (f"ast-grep skipped {unsearched} of the {len(batch)} file(s) it was given "
            f"({'; '.join(named)})")


def _likely_skipped(root, batch, unsearched):
    """The files in `batch` ast-grep would skip, as far as can be told from
    outside, each with the reason. Read only once a skip has been reported.

    A file that cannot be read or decoded is certainly skipped; one over
    AST_GREP_LIKELY_TOO_LARGE only possibly, so sizes are named only when the
    certain ones do not account for all `unsearched` — otherwise a large file
    ast-grep did search would be named as a suspect."""
    certain, large = [], []
    for path in batch:
        try:
            content = (root / path).read_bytes()
        except OSError as failure:
            certain.append(f"{path}: unreadable ({failure.strerror or failure})")
            continue
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            certain.append(f"{path}: not UTF-8")
            continue
        if len(content) > AST_GREP_LIKELY_TOO_LARGE:
            large.append(f"{path}: {len(content)} bytes, possibly too large")
    return certain if len(certain) >= unsearched else certain + large


def _ast_grep_records(rule, stdout):
    """ast-grep's `--json` output as a list of match records, each checked for
    the fields read from it. ast-grep 0.50.0 prints `[]` when nothing
    matched, so empty output, or JSON of another shape, is a failure rather
    than "no matches"."""
    if not stdout.strip():
        raise LintError(f"rule {rule.id!r}: ast-grep produced no output, not even `[]`")
    try:
        records = json.loads(stdout)
    except ValueError as unparseable:
        raise LintError(
            f"rule {rule.id!r}: ast-grep's output was not JSON: {stdout[:200]!r}"
        ) from unparseable
    if not isinstance(records, list) or not all(map(_is_match_record, records)):
        raise LintError(
            f"rule {rule.id!r}: ast-grep's output was not a list of matches: "
            f"{stdout[:200]!r}"
        )
    return records


def _is_match_record(record):
    """Whether `record` has the fields read from it, of the right types, with
    an end that is not before its start."""
    try:
        start, end = record["range"]["start"], record["range"]["end"]
        return (isinstance(record["file"], str) and isinstance(record["text"], str)
                and all(isinstance(point[key], int) and not isinstance(point[key], bool)
                        and point[key] >= 0
                        for point in (start, end) for key in ("line", "column"))
                and (end["line"], end["column"]) >= (start["line"], start["column"]))
    except (KeyError, TypeError):
        return False


def _starts_with_bom(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(len(UTF8_BOM)) == UTF8_BOM
    except OSError:
        # It was just searched; if it has gone since, the column is the least
        # of what is wrong, and ast-grep's own is the best left.
        return False


def _repo_relative(root, rule, reported):
    """ast-grep's absolute path for a match, as a repo-relative POSIX path. A
    path that is not under the repo is an error rather than a dropped match:
    dropping it would read as one violation fewer, i.e. as cleaner."""
    try:
        return Path(reported).relative_to(root).as_posix()
    except ValueError as stray:
        raise LintError(
            f"rule {rule.id!r}: ast-grep reported a match in {reported}, "
            f"which is not under {root}"
        ) from stray


if __name__ == "__main__":
    sys.exit(main())
