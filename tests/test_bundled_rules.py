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
`fixtures/sample-repo/PLANTED.json`, whose line numbers are generated from the
files and checked against them by tests/test_fixture_repo.py. They are kept
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
import shutil
from collections import Counter
from pathlib import Path

import pytest

from curamoir.lint import EXIT_VIOLATIONS, main, run_lint
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


def fixture_copy(tmp_path):
    repo = tmp_path / "sample"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    return install(repo, "python", "typescript")


def expected_findings():
    return sorted(
        (f["path"], f["line"], f["principle_id"], f["rule_id"])
        for f in PLANTED["bundled_rule_findings"]
    )


# --- the format: one loader, both languages ---------------------------------

@pytest.mark.parametrize("language", sorted(LANGUAGE_DIRECTORIES))
def test_every_bundled_rule_loads_through_the_real_parser(language):
    files = rule_files(language)
    assert files, f"no bundled {language} rules"
    for path in files:
        rule = parse_rule(path.read_text(encoding="utf-8"), source=path.name,
                          known_principles=starter_ids())
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
        return [
            parse_rule(p.read_text(encoding="utf-8"), p.name, starter_ids())
            for p in rule_files(language)
        ]

    python = [r for r in ast_grep_rules("python") if r.matcher == "ast-grep"]
    typescript = [r for r in ast_grep_rules("typescript") if r.matcher == "ast-grep"]

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
            rule = parse_rule(path.read_text(encoding="utf-8"), path.name, starter_ids())
            assert rule.fix.strip(), path.name


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


def test_the_typescript_plant_is_in_a_file_ast_grep_reads_as_typescript():
    """cm-dpd: `languages: [typescript]` does not take in .mts/.cts, so the
    plant must not rely on those suffixes."""
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
    found = sorted((v.path, v.line, v.principle_id, v.rule_id) for v in result.violations)
    assert found == expected_findings()


@needs_ast_grep
@pytest.mark.parametrize("language", sorted(LANGUAGE_DIRECTORIES))
def test_each_language_alone_finds_its_own_plants(tmp_path, language):
    """Installed alone, a language's rules find their plants and nothing of the
    other's — the scoping is part of what makes the second language a proof."""
    repo = tmp_path / "sample"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    install(repo, language)
    own = {p.stem for p in rule_files(language)}

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    found = sorted((v.path, v.line, v.principle_id, v.rule_id) for v in result.violations)
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

#: What the bundled rules find in cúramóir's own `src/`, pinned by file and
#: rule (not line, so an unrelated edit does not break this) so that a NEW
#: finding fails here and is looked at rather than silently accepted.
#:
#: All four are `print(..., file=sys.stderr)` from code that runs as a process
#: entry point: the guardrail gateway's and the job runner's last-resort
#: messages when the ledger itself cannot be written (logging is not set up
#: there and stderr is the channel Claude Code shows the user), and
#: curamoir-lint's own CLI error line. They are inside the CLI exception
#: `structured-logging` states, but not inside the rule's filename exclusions
#: (`cli.py`, `__main__.py`), which is the honest limit of a path-based
#: exemption. A repo adopting the rule would exclude those files or route
#: them through a logger; cúramóir has not installed the examples into its own
#: `.curamoir/rules/`, so they fail nothing here.
OWN_SRC_FINDINGS = Counter({
    ("src/curamoir/guards/gateway.py", "no-print"): 2,
    ("src/curamoir/job_run.py", "no-print"): 1,
    ("src/curamoir/lint.py", "no-print"): 1,
})


@needs_ast_grep
def test_what_the_bundled_rules_find_in_curamoirs_own_src(tmp_path):
    repo = tmp_path / "curamoir"
    shutil.copytree(ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
    install(repo, "python", "typescript")

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    assert result.skipped == ()
    assert Counter((v.path, v.rule_id) for v in result.violations) == OWN_SRC_FINDINGS
