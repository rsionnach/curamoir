"""E2-R6: the bundled example rules in `examples/`.

"At least three working Python rules and at least one working TypeScript rule
run against the fixtures and find their planted violations", and "the same rule
format loads for both bundled languages".

"Working" is asserted the strict way: the real loader, the real runner and the
real ast-grep, over a copy of the fixture, must report EXACTLY the findings the
fixture's manifest records for the bundled rules — set equality on path, line,
principle and rule. "At least one match" would pass a pattern that also fires
on half the file.

**Fixture provenance.** The expected findings are `bundled_rule_findings` in
`fixtures/sample-repo/PLANTED.json`, whose line numbers are checked against
the files by tests/test_fixture_repo.py. Beside them, `bundled_rule_non_findings`
records code a rule must NOT match, so an over-broad pattern fails too. They are kept
out of the manifest's `violations` list on purpose: E3's acceptance criterion
is exactly "5 planted violations across 3 principles", and that list is E3's.

**Why the copy gets the starter principles.** The fixture's own
`principles.md` is the three-principle subset E3 needs, and two bundled rules
cite starter principles outside it (`typed-errors`, `tests-assert-behaviour`).
A rule citing an undefined principle is refused at load — correctly — so the
copy is set up the way `/curamoir:init` sets up a repo: with
`templates/principles.md`. That is the repo the examples are written for.
"""

import json
import re
import shutil
from collections import Counter
from pathlib import Path

import pytest

from curamoir.lint import EXIT_ERROR, EXIT_VIOLATIONS, main, run_lint
from curamoir.principles import parse_principles
from curamoir.rules import AST_GREP_LANGUAGES, load_rules, parse_rule

# The shared marker, so these skip and fail exactly where test_lint's do
# (see test_ast_grep_is_installed_where_it_is_required there).
from tests.test_lint import needs_ast_grep

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
FIXTURE = ROOT / "fixtures" / "sample-repo"
STARTER = ROOT / "templates" / "principles.md"
PLANTED = json.loads((FIXTURE / "PLANTED.json").read_text(encoding="utf-8"))

LANGUAGE_DIRECTORIES = {"python": EXAMPLES / "python", "typescript": EXAMPLES / "typescript"}


def rule_files(language):
    return sorted(LANGUAGE_DIRECTORIES[language].glob("*.yaml"))


def parse_bundled(path):
    """A bundled rule file through the real parser, against the starter
    principles it is written for."""
    return parse_rule(path.read_text(encoding="utf-8"), source=path.name,
                      known_principles=starter_ids())


def starter_ids():
    return {p.id for p in parse_principles(STARTER.read_text(encoding="utf-8")).principles}


def install(repo, *languages):
    """Copy the bundled rules into `repo/.curamoir/rules/`, flat, as the
    README tells a user to, and seed the starter principles as init does."""
    rules = repo / ".curamoir" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STARTER, repo / ".curamoir" / "principles.md")
    for language in languages:
        for path in rule_files(language):
            shutil.copyfile(path, rules / path.name)
    return repo


def fixture_copy(tmp_path, *languages):
    """A copy of the sample repo with `languages`' rules installed — both
    bundled languages unless named."""
    repo = tmp_path / "sample"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    return install(repo, *(languages or sorted(LANGUAGE_DIRECTORIES)))


def expected_findings():
    return sorted(
        (f["path"], f["line"], f["principle_id"], f["rule_id"])
        for f in PLANTED["bundled_rule_findings"]
    )


def non_findings():
    """`bundled_rule_non_findings`, as (path, line, rule) — no principle,
    because a non-finding is not a violation of one."""
    return sorted((f["path"], f["line"], f["rule_id"])
                  for f in PLANTED["bundled_rule_non_findings"])


def found_findings(result):
    """`result`'s violations in the same shape as `expected_findings()`."""
    return sorted((v.path, v.line, v.principle_id, v.rule_id) for v in result.violations)


# --- the format: one loader, both languages ---------------------------------

@pytest.mark.parametrize("language", sorted(LANGUAGE_DIRECTORIES))
def test_every_bundled_rule_loads_through_the_real_parser(language):
    files = rule_files(language)
    assert files, f"no bundled {language} rules"
    for path in files:
        rule = parse_bundled(path)
        # The file is named for its rule, so a copied directory cannot hold
        # two files that disagree about which rule they are.
        assert path.stem == rule.id
        # Scoped to its own language: a Python rule that also searched .ts
        # files would make the TypeScript proof a claim, not a demonstration.
        assert rule.languages == (language,), rule


def test_the_spec_minimums_are_met_with_ast_grep_rules():
    """E2-R6's counts, and DEC-3's matcher. A regex rule (the escape hatch)
    would not count toward either."""
    def ast_grep_rules(language):
        rules = (parse_bundled(p) for p in rule_files(language))
        return [r for r in rules if r.matcher == "ast-grep"]

    python = ast_grep_rules("python")
    typescript = ast_grep_rules("typescript")

    assert len(python) >= 3, [r.id for r in python]
    assert len(typescript) >= 1, [r.id for r in typescript]


def test_both_languages_use_the_same_fields():
    """'The same rule format loads for both bundled languages.' The format is
    the same if a rule in either language is written with the same keys; a
    TypeScript rule needing a field the Python ones do not would be a second
    format."""
    import yaml

    def keys(language):
        return {
            frozenset(yaml.safe_load(p.read_text(encoding="utf-8")))
            for p in rule_files(language)
        }

    assert keys("python") == keys("typescript")


def test_every_bundled_rule_carries_a_fix():
    """`fix` is optional in the format, but it is the field that stops an agent
    retrying the same thing; an example that omits it teaches omitting it."""
    for language in LANGUAGE_DIRECTORIES:
        for path in rule_files(language):
            assert parse_bundled(path).fix.strip(), path.name


def test_python_and_typescript_rules_install_together(tmp_path):
    """The README says copy both directories' files into one rules directory;
    that only works if no two bundled rules share an id."""
    repo = install(tmp_path, "python", "typescript")

    rules = load_rules(repo)

    assert len(rules) == len(rule_files("python")) + len(rule_files("typescript"))


def test_the_manifest_names_only_bundled_rules():
    """So an expected finding cannot cite a rule that does not exist and pass
    by never being looked for."""
    bundled = {p.stem for language in LANGUAGE_DIRECTORIES for p in rule_files(language)}

    assert {f["rule_id"] for f in PLANTED["bundled_rule_findings"]} == bundled
    assert {f["rule_id"] for f in PLANTED["bundled_rule_non_findings"]} <= bundled


def readme_severities():
    """The severity column of examples/README.md's table, by rule id (the
    file stem in the first column)."""
    rows = re.findall(r"^\| `(?:python|typescript)/([\w-]+)\.yaml` \|[^|]*\| `(\w+)` \|",
                      (EXAMPLES / "README.md").read_text(encoding="utf-8"), re.MULTILINE)
    return dict(rows)


#: The severity split each rule file's opening comment argues for: the two
#: mechanical, almost-never-intended output calls fail the lint; the two that
#: want judgement (a design change, or an approximate path exclusion) only
#: report. Literal here so that a rule and the README drifting together, in
#: the same wrong direction, still fails.
DOCUMENTED_SEVERITIES = {
    "no-print": "error",
    "no-console-log": "error",
    "no-raise-runtime-error": "warning",
    "no-assert-mock-called": "warning",
}


def test_each_bundled_rule_has_its_documented_severity():
    """The README's table and each rule's comment say which rules fail the
    lint at the default `fail_on: error`; a rule whose `severity` disagreed
    would fail (or stop failing) a user's lint without the docs saying so."""
    shipped = {
        rule.id: rule.severity
        for language in LANGUAGE_DIRECTORIES
        for rule in (parse_bundled(p) for p in rule_files(language))
    }

    assert readme_severities() == DOCUMENTED_SEVERITIES
    assert shipped == DOCUMENTED_SEVERITIES


def test_the_typescript_plant_is_in_a_file_ast_grep_reads_as_typescript():
    """`languages: [typescript]` does not take in .mts/.cts (bead cm-dpd), so
    the plant must not rely on those suffixes."""
    typescript = [f for f in PLANTED["bundled_rule_findings"]
                  if f["rule_id"] in {p.stem for p in rule_files("typescript")}]

    assert typescript
    for finding in typescript:
        assert AST_GREP_LANGUAGES[Path(finding["path"]).suffix] == "typescript"


# --- the acceptance criterion: real runner, real ast-grep, exact findings ---

@needs_ast_grep
def test_the_bundled_rules_find_exactly_their_planted_violations(tmp_path):
    repo = fixture_copy(tmp_path)

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    assert result.skipped == ()
    assert found_findings(result) == expected_findings()
    # Implied by the equality, but said outright: an over-broad pattern (say
    # `console.$M(...)` for `console.log`) fails on a named line.
    found = {(path, line, rule) for path, line, _, rule in found_findings(result)}
    assert non_findings()
    assert found.isdisjoint(non_findings())


@needs_ast_grep
def test_no_print_matches_every_argument_layout(tmp_path):
    """The fixture's `print` plants each take one argument, and the fixture
    cannot gain plants without changing E3's count, so the layouts the rule
    file promises are checked here on a snippet: no arguments, several, and
    the `file=sys.stderr` keyword."""
    repo = install(tmp_path / "snippet", "python")
    (repo / "app.py").write_text(
        "import sys\n"
        "print()\n"
        "print(\"a\", \"b\", file=sys.stderr)\n"
        "logger.print(\"not the builtin\")\n",
        encoding="utf-8",
    )

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    assert result.skipped == ()
    assert sorted((v.path, v.line, v.rule_id) for v in result.violations) == [
        ("app.py", 2, "no-print"),
        ("app.py", 3, "no-print"),
    ]


@needs_ast_grep
@pytest.mark.parametrize("language", sorted(LANGUAGE_DIRECTORIES))
def test_each_language_alone_finds_its_own_plants(tmp_path, language):
    """Installed alone, a language's rules find their plants and nothing of the
    other's — the scoping is part of what makes the second language a proof."""
    repo = fixture_copy(tmp_path, language)
    own = {p.stem for p in rule_files(language)}

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    found = found_findings(result)
    assert found == [f for f in expected_findings() if f[3] in own]
    assert found, f"the {language} rules found nothing"


@needs_ast_grep
def test_the_cli_exits_1_on_the_planted_violations(tmp_path, capsys):
    repo = fixture_copy(tmp_path)

    code = main(["--repo", str(repo), "--format", "json"])
    report = json.loads(capsys.readouterr().out)

    assert code == EXIT_VIOLATIONS
    assert report["complete"] is True
    assert len(report["violations"]) == len(PLANTED["bundled_rule_findings"])


# --- the examples against cúramóir's own code --------------------------------

#: What the bundled rules find in cúramóir's own `src/` and `tests/`, pinned
#: by file and
#: rule (not line, so an unrelated edit does not break this) so that a NEW
#: finding fails here and is looked at rather than silently accepted.
#:
#: `tests/` contributes nothing, and that is the point of copying it: it
#: raises RuntimeError on purpose in many files (test doubles simulating a
#: failure), so `no-raise-runtime-error`'s `exclude` list is what keeps it at
#: zero. Neither of the other Python rules fires there either: cúramóir's tests
#: assert on behaviour, not `.called`, and do not print.
#:
#: All six are `print(..., file=sys.stderr)` from code that runs as a process
#: entry point: the guardrail gateway's and the job runner's last-resort
#: messages when the ledger itself cannot be written, job_guard's when there
#: is no repository root to find a ledger under and when the supervisor lock
#: cannot be released (the job line is already written; logging is not set up
#: there and stderr is the channel Claude Code shows the user), and
#: curamoir-lint's own CLI error line. They are inside the CLI exception
#: `structured-logging` states, but not inside the rule's filename exclusions
#: (`cli.py`, `__main__.py`), which is the honest limit of a path-based
#: exemption. A repo adopting the rule would exclude those files or route
#: them through a logger; cúramóir has not installed the examples into its own
#: `.curamoir/rules/`, so they fail nothing here.
OWN_SRC_FINDINGS = Counter({
    ("src/curamoir/guards/gateway.py", "no-print"): 2,
    ("src/curamoir/job_guard.py", "no-print"): 2,
    ("src/curamoir/job_run.py", "no-print"): 1,
    ("src/curamoir/lint.py", "no-print"): 1,
})


@needs_ast_grep
def test_what_the_bundled_rules_find_in_curamoirs_own_code(tmp_path):
    repo = tmp_path / "curamoir"
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache")
    for directory in ("src", "tests"):
        shutil.copytree(ROOT / directory, repo / directory, ignore=ignore)
    install(repo, "python", "typescript")
    # The zero for tests/ below means something only while tests/ has
    # RuntimeErrors for the exclusions to keep out.
    assert any("raise RuntimeError(" in p.read_text(encoding="utf-8")
               for p in (repo / "tests").rglob("test_*.py"))

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    assert result.skipped == ()
    assert Counter((v.path, v.rule_id) for v in result.violations) == OWN_SRC_FINDINGS


# --- what ships in examples/ -------------------------------------------------

def test_examples_holds_only_the_tested_language_directories():
    """Every test above walks LANGUAGE_DIRECTORIES and globs `*.yaml`, so a new
    language directory, or a rule saved as `.yml` or `.YAML`, would ship
    without any of them looking at it. Pin the layout so that cannot happen
    quietly: a new directory or suffix has to be added here, and so tested."""
    def shipped(paths):
        # Finder's .DS_Store is never committed and never a rule; anything
        # else unexpected is.
        return [p for p in paths if p.name != ".DS_Store"]

    assert {p.name for p in shipped(EXAMPLES.iterdir())} == {*LANGUAGE_DIRECTORIES, "README.md"}
    for directory in LANGUAGE_DIRECTORIES.values():
        assert directory.is_dir(), directory
        # Exact-case `.yaml` only: on a case-insensitive filesystem `*.yaml`
        # would also glob `.YAML`, so compare the suffix itself.
        strays = [p.name for p in shipped(directory.rglob("*")) if p.suffix != ".yaml"]
        assert strays == [], f"{directory.name}: {strays}"


def test_install_is_refused_where_a_cited_principle_is_missing(tmp_path, capsys):
    """The README's promise: delete `typed-errors` from your principles and
    `no-raise-runtime-error.yaml` is refused, rather than linting while
    counting its fixes towards nothing. The whole `## typed-errors` section
    goes, as a user deleting the principle would, so the rest still parses —
    the refusal must be about the missing id, not a broken file."""
    repo = install(tmp_path, "python")
    principles = repo / ".curamoir" / "principles.md"
    text = principles.read_text(encoding="utf-8")
    start = text.index("## typed-errors\n")
    end = text.index("\n## ", start + 1) + 1
    principles.write_text(text[:start] + text[end:], encoding="utf-8")
    remaining = {p.id for p in parse_principles(principles.read_text(encoding="utf-8")).principles}
    assert remaining == starter_ids() - {"typed-errors"}

    code = main(["--repo", str(repo), "--format", "json"])
    err = capsys.readouterr().err

    assert code == EXIT_ERROR
    assert "no-raise-runtime-error.yaml" in err
    assert "typed-errors" in err
