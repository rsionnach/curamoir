"""E2-R4 + E2-R5: `curamoir-lint`, the runner that turns rules into a work list.

**Who reads the output.** A fresh agent — the sweep's (E3-R1) or anyone's — that
has not read `principles.md` and should not have to. So every violation carries
its own context: where (`path:line:col`), which rule and principle, the
principle's one-sentence rule, a link to the principle's `why`, and the fix. An
agent told only "no" retries the same thing or invents something worse; an
agent told what to do instead does that.

**What it is for.** A gate in CI and pre-commit, so its exit code is the real
output and its worst failure is passing after checking nothing — which looks
exactly like a clean repo. So every way a rule can fail to check exits 2; the
one deliberate exception is a repo with no rules at all, which exits 0 with a
note on stderr, because a fresh repo is not a failing one:

    0  every rule ran and nothing met the threshold
    1  every rule ran and at least one violation met the threshold
    2  the run cannot be trusted: a rule or the config did not load, ast-grep
       is needed and absent, or ast-grep failed

2 outranks 1, because "incomplete" is the more fundamental fact: a run that
skipped half its rules and found one violation has not shown the other half
clean. When rules were skipped the partial report is still printed, so the work
it did find is not lost; for any other runner error stdout is empty and the
reason is on stderr. (The one gap: if the interpreter cannot import this module
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
in it may decide what the runner reads or executes. Two consequences: symlinks
are not followed (see `_repository_files`), and ast-grep never sees the repo's
own `sgconfig.yml` (see `_ast_grep_batch`).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from curamoir.config import DEFAULTS, ConfigError, load_config_text
from curamoir.principles import PrincipleError, load_principles
from curamoir.rules import ALWAYS_EXCLUDED, SEVERITIES, RuleError, load_rules

EXIT_CLEAN = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

#: Bumped only for a change a parser would notice: a field removed, renamed or
#: retyped. The sweep (E3-R1) parses this, so the shape is a contract.
SCHEMA_VERSION = 1

#: Every violation record has exactly these fields, in this order. Location
#: first because it is what an editor or agent opens; the principle's context
#: after the message because it explains the message.
RECORD_FIELDS = (
    "location", "path", "line", "col", "severity", "rule_id", "principle_id",
    "principle_rule", "message", "fix", "why_link", "match",
)

#: Where the principles file is, relative to the repo, for `why_link`.
PRINCIPLES_PATH = ".curamoir/principles.md"

#: Files per ast-grep invocation. A large repo's file list would otherwise
#: exceed the OS argument-length limit, which fails the whole call.
AST_GREP_BATCH = 200

#: File suffix to ast-grep `--lang`. The language is passed explicitly because
#: without it ast-grep treats a pattern it cannot parse as one that matches
#: nothing — exit 1, empty output, no warning (observed, 0.50.0) — which is a
#: rule silently checking nothing. With `--lang` it warns about the "ERROR
#: node", and that is caught below. Markdown and TOML have no entry: ast-grep
#: has no grammar for TOML, and a rule over prose wants `regex` anyway.
AST_GREP_LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".go": "go", ".rs": "rust",
    ".yaml": "yaml", ".yml": "yaml",
}

AST_GREP_INSTALL = (
    "pip install ast-grep-cli  (or npm i -g @ast-grep/cli, or brew install ast-grep)"
)


class LintError(Exception):
    """The run cannot be trusted. Always exit 2, with this message on stderr."""


@dataclass(frozen=True)
class Violation:
    """One violation, with everything an agent needs to act on it."""

    path: str
    line: int
    col: int
    severity: str
    rule_id: str
    principle_id: str
    principle_rule: str
    message: str
    fix: str
    why_link: str
    match: str

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
    #: stderr, not in the JSON: they are not a rule's result, and leaving them
    #: out keeps the schema (and its version) unchanged.
    symlinks: tuple = ()

    @property
    def complete(self):
        return not self.skipped


def run_lint(repo, ast_grep=None):
    """Run every rule in `repo` and return what they found.

    `ast_grep` is the path to the ast-grep binary, or None when it is absent —
    passed in rather than looked up so a caller (or a test) decides what is
    installed. Raises LintError for anything that makes the result untrustworthy
    other than a missing ast-grep, which skips its rules and is reported.
    """
    repo = Path(repo)
    if not repo.is_dir():
        raise LintError(f"{repo} is not a directory; pass the repository root with --repo")
    try:
        rules = load_rules(repo)
        principles = load_principles(repo)
    except (RuleError, PrincipleError) as invalid:
        raise LintError(str(invalid)) from invalid

    files, symlinks = _repository_files(repo)
    violations, checked, skipped = [], [], []
    for rule in sorted(rules, key=lambda r: r.id):
        if rule.matcher == "ast-grep" and ast_grep is None:
            skipped.append(Skipped(
                rule.id,
                f"ast-grep is not installed, so this ast-grep rule did not run. "
                f"Install it: {AST_GREP_INSTALL}",
            ))
            continue
        # load_rules has already resolved the id, so this cannot be None.
        principle = principles.by_id(rule.principle)
        scoped = _scoped(files, rule)
        if rule.matcher == "ast-grep":
            matches = _ast_grep_matches(repo, rule, scoped, ast_grep)
        else:
            matches = _regex_matches(repo, rule, scoped)
        for path, line, col, text in matches:
            violations.append(Violation(
                path=path, line=line, col=col,
                severity=rule.severity,
                rule_id=rule.id,
                principle_id=rule.principle,
                principle_rule=principle.rule,
                message=rule.render(path=path, line=line, col=col, match=text),
                # A rule may have no fix; the principle's `good` example is a
                # corrected form by definition, and an empty fix is the one
                # thing guaranteed to send the agent round again.
                fix=rule.fix or principle.good,
                why_link=f"{PRINCIPLES_PATH}#{_anchor(rule.principle)}",
                match=text,
            ))
        checked.append(rule.id)

    violations.sort(key=lambda v: (v.path, v.line, v.col, v.rule_id))
    return LintResult(tuple(violations), tuple(checked), tuple(skipped), tuple(symlinks))


def exit_code(result, fail_on):
    """The gate. See the module docstring for why 2 outranks 1."""
    if not result.complete:
        return EXIT_ERROR
    floor = SEVERITIES.index(fail_on)
    if any(SEVERITIES.index(v.severity) >= floor for v in result.violations):
        return EXIT_VIOLATIONS
    return EXIT_CLEAN


def format_json(result, fail_on):
    document = {
        "version": SCHEMA_VERSION,
        "fail_on": fail_on,
        "complete": result.complete,
        "rules_checked": list(result.checked),
        "rules_skipped": [{"rule_id": s.rule_id, "reason": s.reason} for s in result.skipped],
        "violations": [v.record() for v in result.violations],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def format_human(result, fail_on):
    """`path:line:col: severity [principle/rule] message`, then the context.

    The first line is the shape compilers use, so editors and terminals that
    linkify locations do it here too.
    """
    lines = []
    for v in result.violations:
        lines.append(f"{v.path}:{v.line}:{v.col}: {v.severity} "
                     f"[{v.principle_id}/{v.rule_id}] {v.message}")
        lines.append(f"    rule: {v.principle_rule}")
        lines.append(f"    why:  {v.why_link}")
        lines.append(f"    fix:  {v.fix}")
    failing = sum(
        1 for v in result.violations
        if SEVERITIES.index(v.severity) >= SEVERITIES.index(fail_on)
    )
    noun = "violation" if len(result.violations) == 1 else "violations"
    lines.append(
        f"{len(result.violations)} {noun} from {len(result.checked)} rule(s); "
        f"{failing} at or above {fail_on!r}"
    )
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="curamoir-lint",
        description=(
            "Run .curamoir/rules/*.yaml against a repository. Exit 0 clean, 1 "
            "violations at or above the threshold, 2 the run cannot be trusted."
        ),
    )
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument(
        "--fail-on", choices=SEVERITIES, default=None,
        help="lowest severity that fails the run (default: rules.fail_on, else error)",
    )
    args = parser.parse_args(argv)

    try:
        fail_on = args.fail_on or _configured_threshold(Path(args.repo))
        result = run_lint(args.repo, ast_grep=_which_ast_grep())
    except LintError as failure:
        print(f"curamoir-lint: {failure}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as unexpected:
        # Python exits 1 on an uncaught exception, which is this tool's code for
        # "violations found". A crash must not read as a lint result.
        print(f"curamoir-lint: internal error: {unexpected!r}", file=sys.stderr)
        return EXIT_ERROR

    render = format_json if args.format == "json" else format_human
    sys.stdout.write(render(result, fail_on))
    if not result.checked and not result.skipped:
        print(
            "curamoir-lint: no rules in .curamoir/rules/, so nothing was checked. "
            "See docs/rules-authoring.md to write one.",
            file=sys.stderr,
        )
    for skipped in result.skipped:
        print(f"curamoir-lint: skipped {skipped.rule_id}: {skipped.reason}", file=sys.stderr)
    if result.symlinks:
        # Names only, never targets or contents. Not an incomplete run: a
        # symlink is not repository content, and its target, if it is in the
        # repo, is linted at its own path.
        print(
            f"curamoir-lint: did not follow {len(result.symlinks)} symlink(s): "
            + ", ".join(result.symlinks),
            file=sys.stderr,
        )
    return exit_code(result, fail_on)


def _configured_threshold(repo):
    """`rules.fail_on` from the repo's config, or the default.

    Strict where `repo_config.effective_config` is tolerant: a hook should keep
    working on a broken config, but a gate silently reverting to the default
    threshold is a gate loosened without anybody being told.
    """
    path = repo / ".curamoir" / "curamoir.yaml"
    if not path.is_file():
        return DEFAULTS["rules"]["fail_on"]
    try:
        config = load_config_text(path.read_text(encoding="utf-8"))
    except (ConfigError, OSError) as invalid:
        raise LintError(f"{path}: {invalid}") from invalid
    except Exception as invalid:  # a YAML syntax error is PyYAML's own type
        raise LintError(f"{path}: not valid YAML: {invalid}") from invalid
    return config["rules"]["fail_on"]


def _which_ast_grep():
    # Only `ast-grep`, never `sg`: on Linux `sg` is usually shadow-utils'
    # set-group command, and running it with a pattern would be a surprise.
    return shutil.which("ast-grep")


def _repository_files(repo):
    """(files, symlinks) under `repo`, each sorted repo-relative POSIX paths.

    A plain walk rather than `git ls-files`, so the runner works on an export,
    a CI checkout without history, or a directory that is not a repo at all.
    ALWAYS_EXCLUDED is pruned whatever a rule says; .gitignore is not read, so
    a generated directory outside that list is searched.

    Symlinks are returned apart and never linted. `match` quotes matched text,
    so a PR committing `leak.py -> ~/.ssh/id_rsa` would otherwise have CI print
    whatever of the target a rule matched; and ast-grep reads a symlink it is
    named. os.walk already declines to descend a directory symlink but does
    list file symlinks, so both kinds are filtered here explicitly. Only the
    walk is guarded: the repo root itself may be a symlink (the caller chose
    it), and a symlink swapped in between this walk and the read is out of
    scope — a CI checkout does not change under the runner.
    """
    found, symlinks = [], []
    for directory, subdirectories, names in os.walk(repo):
        base = Path(directory).relative_to(repo)
        kept = []
        for name in sorted(subdirectories):
            if name in ALWAYS_EXCLUDED:
                continue
            if os.path.islink(os.path.join(directory, name)):
                symlinks.append((base / name).as_posix())
            else:
                kept.append(name)
        subdirectories[:] = kept
        for name in names:
            if os.path.islink(os.path.join(directory, name)):
                symlinks.append((base / name).as_posix())
            else:
                found.append((base / name).as_posix())
    return sorted(found), sorted(symlinks)


def _scoped(files, rule):
    """The files `rule` applies to: its patterns, minus its exclusions."""
    include = [_glob(pattern) for pattern in rule.file_patterns()]
    exclude = [_glob(pattern) for pattern in rule.exclude]
    return [
        path for path in files
        if any(p.fullmatch(path) for p in include)
        and not any(p.fullmatch(path) for p in exclude)
    ]


def _glob(pattern):
    """A glob as a regex over POSIX paths, with `**/` matching zero or more
    directories. `fnmatch` has no `**`, so `**/*.py` would miss `setup.py` at
    the root — a whole class of file silently unlinted."""
    out = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out))


def _regex_matches(repo, rule, files):
    """(path, line, col, text) for every regex match, 1-based."""
    compiled = rule.compiled()
    if rule.target == "path":
        # A filename has no line; 1:1 is the file itself, which is what an
        # editor should open.
        matches = []
        for path in files:
            found = compiled.search(path)
            if found:
                matches.append((path, 1, 1, found.group(0)))
        return matches
    matches = []
    for path in files:
        try:
            # `replace`: one undecodable byte should not hide the rest of the
            # file from the rule, and must not crash the run.
            text = (repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError as unreadable:
            raise LintError(f"rule {rule.id!r} could not read {path}: {unreadable}") \
                from unreadable
        for number, line in enumerate(text.splitlines(), start=1):
            for found in compiled.finditer(line):
                matches.append((path, number, found.start() + 1, found.group(0)))
    return matches


def _ast_grep_matches(repo, rule, files, binary):
    """(path, line, col, text) from ast-grep's own JSON, converted to 1-based.

    Exit 0 is matches and exit 1 is none — like grep — and anything else is a
    failure. But exit codes alone are not enough: ast-grep exits 1 for a path
    that does not exist and 0 for a pattern it could not parse (with a warning
    naming an "ERROR node"), and both would otherwise read as a clean run. So
    "ERROR" on stderr is a failure too, with ast-grep's own words quoted.

    Files are grouped by language so `--lang` can be explicit (see
    AST_GREP_LANGUAGES); a scoped file whose suffix has no ast-grep grammar is
    not searched by an ast-grep rule, since no AST pattern can match it.
    """
    by_language = {}
    for path in files:
        language = AST_GREP_LANGUAGES.get(Path(path).suffix)
        if language is not None:
            by_language.setdefault(language, []).append(path)
    matches = []
    with tempfile.TemporaryDirectory(prefix="curamoir-lint-") as scratch:
        config = Path(scratch) / "sgconfig.yml"
        config.write_text("ruleDirs: []\n", encoding="utf-8")
        for language, paths in sorted(by_language.items()):
            for start in range(0, len(paths), AST_GREP_BATCH):
                matches.extend(_ast_grep_batch(
                    repo, rule, binary, language, paths[start:start + AST_GREP_BATCH],
                    scratch=scratch, config=config,
                ))
    return matches


def _ast_grep_batch(repo, rule, binary, language, batch, scratch, config):
    """(path, line, col, text) from one ast-grep run over files of one language.

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

    - `--config` names cúramóir's own empty config (`scratch/sgconfig.yml`).
      Verified: with it, an unparseable `sgconfig.yml` in the cwd is not read.
    - The cwd is `scratch`, a fresh temp directory outside the repo, so
      ancestor discovery finds nothing of the repo's; files are therefore
      named absolutely. Verified: from a neutral cwd the same unparseable
      repo config is not read. ast-grep echoes each path as given, and they
      are converted back to repo-relative POSIX paths below.
    """
    root = repo.resolve()
    command = [
        binary, "run", f"--config={config}", f"--pattern={rule.pattern}",
        f"--lang={language}", "--json=compact", "--",
        *(str(root / path) for path in batch),
    ]
    try:
        completed = subprocess.run(
            command, cwd=scratch, capture_output=True, text=True, check=False,
        )
    except OSError as failure:
        raise LintError(f"rule {rule.id!r}: could not run ast-grep ({binary}): {failure}") \
            from failure
    stderr = completed.stderr.strip()
    if completed.returncode not in (0, 1) or "ERROR" in stderr:
        raise LintError(
            f"rule {rule.id!r} ({rule.source}): ast-grep failed "
            f"(exit {completed.returncode}): {stderr or 'no message'}. "
            "If the pattern is the problem, the ast-grep playground shows how "
            "it parses: https://ast-grep.github.io/playground.html"
        )
    try:
        records = json.loads(completed.stdout or "[]")
    except ValueError as unparseable:
        raise LintError(
            f"rule {rule.id!r}: ast-grep's output was not JSON: {completed.stdout[:200]!r}"
        ) from unparseable
    return [
        (
            _repo_relative(root, rule, record["file"]),
            record["range"]["start"]["line"] + 1,
            record["range"]["start"]["column"] + 1,
            record["text"],
        )
        for record in records
    ]


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


def _anchor(heading):
    """The fragment a Markdown renderer gives `## <heading>`: lowercased, with
    anything but word characters, spaces and hyphens dropped, spaces to hyphens.
    Principle ids are slugs already, so this is usually the id unchanged."""
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return slug.replace(" ", "-")


if __name__ == "__main__":
    sys.exit(main())
