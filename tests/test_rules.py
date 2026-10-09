"""E2-R3: the rule file format and its schema.

A rule is the link between a principle and a line of code, and that link is the
integrity constraint the whole promotion ladder rests on: E4 promotes a
principle when its violations keep being fixed, and it can only count those if
every rule names exactly one principle that actually exists. So the acceptance
criterion here — an invalid principle id fails validation with a clear message —
is not input hygiene. A rule citing a dead id lints happily and contributes its
fixes to nothing, so the ladder silently stops working for that principle.

DEC-3 settled the matcher question: `ast-grep` is primary, `regex` is a declared
escape hatch for what no AST pattern can express — file naming like `foo_v2.py`,
prose in a commit message, a value in a config file. The schema must load both,
which is why `matcher` is a field rather than an assumption.

The format is validated here and executed by E2-R4. Keeping those apart is
deliberate: a rule with a typo should fail when it is loaded, naming the file
and the field, not halfway through a sweep.
"""

import textwrap

import pytest

from curamoir.rules import RuleError, load_rules, parse_rule

PRINCIPLES = """\
# Golden principles

## structured-logging

- **id:** `structured-logging`
- **title:** Structured logging only
- **rule:** Emit diagnostics through the configured logger, never `print`.
- **why:** A print carries no level and no logger name, so it cannot be filtered.
- **good:** `logger.info("done")`
- **bad:** `print("done")`
- **tier:** principle
- **promoted_on:** null
"""

RULE = textwrap.dedent("""\
    id: no-bare-print
    principle: structured-logging
    severity: error
    matcher: regex
    pattern: '^\\s*print\\('
    languages: [python]
    message: "{match} writes to stdout with no level or logger name"
    fix: "logger.info(...), or logger.debug(...) while debugging"
""")


def write(tmp_path, rule_text, name="no-bare-print.yaml", principles=PRINCIPLES):
    base = tmp_path / ".curamoir"
    (base / "rules").mkdir(parents=True, exist_ok=True)
    (base / "principles.md").write_text(principles, encoding="utf-8")
    (base / "rules" / name).write_text(rule_text, encoding="utf-8")
    return tmp_path


# --- the fields E2-R3 requires -------------------------------------------

def test_a_well_formed_rule_parses(tmp_path):
    rule = load_rules(write(tmp_path, RULE))[0]

    assert rule.id == "no-bare-print"
    assert rule.principle == "structured-logging"
    assert rule.severity == "error"
    assert rule.matcher == "regex"
    assert rule.pattern == r"^\s*print\("
    assert rule.languages == ("python",)
    assert "{match}" in rule.message
    assert rule.fix


@pytest.mark.parametrize("field", ["id", "principle", "severity", "matcher", "pattern", "message"])
def test_a_missing_required_field_is_reported_by_name(tmp_path, field):
    text = "\n".join(
        line for line in RULE.splitlines() if not line.startswith(f"{field}:")
    ) + "\n"

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    assert field in str(caught.value)
    assert "no-bare-print.yaml" in str(caught.value), "name the file to open"


# --- the acceptance criterion: exactly one principle, and it must exist ---

def test_a_rule_citing_an_unknown_principle_is_refused(tmp_path):
    """E2-R3's acceptance criterion. This is the ladder's integrity constraint:
    a rule citing a dead id lints happily and contributes its fixes to nothing,
    so E4 never promotes that principle and nothing reports why."""
    text = RULE.replace("principle: structured-logging", "principle: structured-loggin")

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    message = str(caught.value)
    assert "structured-loggin" in message
    assert "structured-logging" in message, "name the ids that DO exist"


def test_a_rule_citing_several_principles_is_refused(tmp_path):
    """"Exactly one" is the requirement. A rule covering two principles makes
    every fix it produces ambiguous for E3-R5's per-principle counts."""
    text = RULE.replace(
        "principle: structured-logging",
        "principle: [structured-logging, errors-carry-context]",
    )

    with pytest.raises(RuleError, match="exactly one"):
        load_rules(write(tmp_path, text))


def test_rules_load_when_there_are_no_principles_to_check_against(tmp_path):
    """A repo mid-setup has rules and an empty principles file. That must fail
    loudly on the id, not crash — the message is the whole point."""
    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, RULE, principles="# Golden principles\n"))

    assert "structured-logging" in str(caught.value)


# --- the two matchers DEC-3 settled --------------------------------------

@pytest.mark.parametrize("matcher", ["ast-grep", "regex"])
def test_both_matchers_load(tmp_path, matcher):
    text = RULE.replace("matcher: regex", f"matcher: {matcher}")

    assert load_rules(write(tmp_path, text))[0].matcher == matcher


def test_an_unknown_matcher_is_refused_and_names_the_two(tmp_path):
    text = RULE.replace("matcher: regex", "matcher: grep")

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    assert all(name in str(caught.value) for name in ("ast-grep", "regex"))


def test_a_regex_that_does_not_compile_is_refused_at_load_time(tmp_path):
    """Caught here rather than mid-sweep: a sweep that dies on rule 7 of 11 has
    already spent the budget for six."""
    text = RULE.replace(r"pattern: '^\s*print\('", "pattern: '[unclosed'")

    with pytest.raises(RuleError, match="pattern"):
        load_rules(write(tmp_path, text))


# --- severity, which decides what fails CI -------------------------------

@pytest.mark.parametrize("severity", ["error", "warning", "info"])
def test_the_three_severities_load(tmp_path, severity):
    text = RULE.replace("severity: error", f"severity: {severity}")

    assert load_rules(write(tmp_path, text))[0].severity == severity


def test_an_unknown_severity_is_refused(tmp_path):
    """E2-R4 exits non-zero on any violation at severity >= configured, so an
    unrecognised severity means an unorderable comparison and a rule that
    either always or never fails the build."""
    text = RULE.replace("severity: error", "severity: critical")

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    assert all(name in str(caught.value) for name in ("error", "warning", "info"))


# --- the message template, which renders E2-R4's output ------------------

def test_a_message_citing_an_unknown_variable_is_refused(tmp_path):
    """The template is rendered per violation during a sweep. An unknown
    variable there is a KeyError in the middle of the run, so the variables are
    a closed set checked when the rule loads."""
    text = RULE.replace(
        'message: "{match} writes to stdout with no level or logger name"',
        'message: "{snippet} is wrong"',
    )

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    assert "snippet" in str(caught.value)
    assert "{match}" in str(caught.value), "list the variables that do exist"


def test_a_message_can_use_every_documented_variable(tmp_path):
    from curamoir.rules import MESSAGE_VARIABLES

    template = " ".join("{" + name + "}" for name in sorted(MESSAGE_VARIABLES))
    text = RULE.replace(
        'message: "{match} writes to stdout with no level or logger name"',
        f'message: "{template}"',
    )

    rule = load_rules(write(tmp_path, text))[0]

    assert rule.render(path="a.py", line=3, col=1, match="print(1)")


# --- E2-R6: the same format for both bundled languages -------------------

def test_the_same_format_loads_for_both_bundled_languages(tmp_path):
    """E2-R3's second acceptance criterion, and the reason `languages` exists:
    a format that only worked for Python would make the abstraction a claim."""
    python_rule = RULE
    typescript_rule = textwrap.dedent("""\
        id: no-console-log
        principle: structured-logging
        severity: error
        matcher: regex
        pattern: 'console\\.log\\('
        languages: [typescript]
        message: "{match} writes to the console with no level"
        fix: "the project logger"
    """)
    repo = write(tmp_path, python_rule)
    write(repo, typescript_rule, name="no-console-log.yaml")

    rules = {rule.id: rule for rule in load_rules(repo)}

    assert rules["no-bare-print"].languages == ("python",)
    assert rules["no-console-log"].languages == ("typescript",)


def test_a_rule_resolves_its_languages_to_file_patterns(tmp_path):
    """What E2-R4 needs to know which files to read."""
    rule = load_rules(write(tmp_path, RULE))[0]

    assert any(pattern.endswith(".py") for pattern in rule.file_patterns())


def test_an_unknown_language_is_refused_and_lists_the_known_ones(tmp_path):
    text = RULE.replace("languages: [python]", "languages: [cobol]")

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    assert "cobol" in str(caught.value)
    assert "python" in str(caught.value)


def test_explicit_file_globs_override_language_inference(tmp_path):
    """The escape hatch DEC-3 implies: `no-file-variant-proliferation` matches
    on a FILENAME, which is not a language at all."""
    text = textwrap.dedent("""\
        id: no-version-suffixed-files
        principle: structured-logging
        severity: warning
        matcher: regex
        pattern: '_v[0-9]+\\.(py|ts)$'
        files: ["**/*.py", "**/*.ts"]
        target: path
        message: "{path} is a parallel variant of another module"
        fix: "edit the original and let git keep the old behaviour"
    """)

    rule = load_rules(write(tmp_path, text, name="no-variants.yaml"))[0]

    assert rule.target == "path"
    assert rule.file_patterns() == ("**/*.py", "**/*.ts")


def test_an_ast_grep_rule_cannot_target_a_path(tmp_path):
    """ast-grep matches inside a file's syntax tree, and a filename is not in
    it. Before E2-R4 this loaded fine and would have run as a rule that can
    never fire — a lint that quietly checks nothing — so it is refused here,
    naming the matcher that can."""
    text = RULE.replace("matcher: regex", "matcher: ast-grep") + "target: path\n"

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    message = str(caught.value)
    assert "no-bare-print.yaml" in message
    assert "target" in message
    assert "regex" in message


@pytest.mark.parametrize("language", ["markdown", "toml"])
def test_an_ast_grep_rule_over_a_language_with_no_grammar_is_refused(tmp_path, language):
    """Before: it loaded, ran, searched nothing (the runner had no `--lang` for
    `.md` or `.toml`), and was reported as checked — exit 0 on nothing. Refused
    at load like `target: path`, naming the matcher that can do it."""
    text = RULE.replace("matcher: regex", "matcher: ast-grep").replace(
        "languages: [python]", f"languages: [python, {language}]")

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    message = str(caught.value)
    assert "no-bare-print.yaml" in message
    assert language in message
    assert "regex" in message


@pytest.mark.parametrize("language", ["markdown", "toml"])
def test_a_regex_rule_over_prose_languages_still_loads(tmp_path, language):
    text = RULE.replace("languages: [python]", f"languages: [{language}]")

    assert load_rules(write(tmp_path, text))[0].languages == (language,)


def test_an_unscoped_ast_grep_rule_defaults_to_languages_it_can_parse(tmp_path):
    """"No scope" means every language cúramóir names, and for an ast-grep rule
    that has to mean every one ast-grep can parse, or the runner would find
    `.md` files it cannot search and fail the rule."""
    text = RULE.replace("matcher: regex", "matcher: ast-grep").replace(
        "languages: [python]\n", "")

    patterns = load_rules(write(tmp_path, text))[0].file_patterns()

    assert "**/*.py" in patterns
    assert not any(p.endswith((".md", ".toml")) for p in patterns)


# --- globs ---------------------------------------------------------------

@pytest.mark.parametrize(("pattern", "path", "matches"), [
    ("src/[ab].py", "src/a.py", True),
    ("src/[ab].py", "src/c.py", False),
    ("src/[!ab].py", "src/c.py", True),
    ("src/[!ab].py", "src/a.py", False),
    ("src/[a-c].py", "src/b.py", True),
    # A class is one character of one path segment, as in any glob.
    ("src[!x]a.py", "src/a.py", False),
    ("src[/]a.py", "src/a.py", False),
    # `[[]` is the way to say a literal `[`, as with fnmatch.
    ("app/[[]id].tsx", "app/[id].tsx", True),
    ("app/[[]id].tsx", "app/i.tsx", False),
    # An unclosed `[` is literal, as with fnmatch.
    ("a[b.py", "a[b.py", True),
    ("**/*.py", "setup.py", True),
    ("**/*.py", "a/b/c.py", True),
    ("*.py", "a/b.py", False),
])
def test_globs_support_character_classes(pattern, path, matches):
    from curamoir.rules import glob_regex

    assert bool(glob_regex(pattern).fullmatch(path)) is matches


def test_a_glob_that_cannot_compile_is_refused_at_load_time(tmp_path):
    text = RULE.replace("languages: [python]", 'files: ["src/[z-a].py"]')

    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, text))

    assert "[z-a]" in str(caught.value)


# --- rule ids are references too ----------------------------------------

def test_two_rules_with_the_same_id_are_refused(tmp_path):
    """A finding carries its rule id (E2-R4), and E3-R5 counts fixes per rule.
    Two rules with one id makes both numbers meaningless."""
    repo = write(tmp_path, RULE)
    write(repo, RULE, name="duplicate.yaml")

    with pytest.raises(RuleError, match="no-bare-print"):
        load_rules(repo)


def test_a_malformed_yaml_file_names_itself(tmp_path):
    with pytest.raises(RuleError) as caught:
        load_rules(write(tmp_path, "id: [unclosed\n", name="broken.yaml"))

    assert "broken.yaml" in str(caught.value)


def test_a_repo_with_no_rules_directory_loads_as_empty(tmp_path):
    """Every repo before E2-R6 ships rules, and what doctor counts."""
    assert load_rules(tmp_path) == []


def test_a_non_yaml_file_in_the_rules_directory_is_ignored(tmp_path):
    """Authors leave notes next to their rules."""
    repo = write(tmp_path, RULE)
    (repo / ".curamoir" / "rules" / "README.md").write_text("notes\n", encoding="utf-8")

    assert len(load_rules(repo)) == 1


# --- parse_rule on its own, for callers that have text not files ---------

def test_parse_rule_validates_without_touching_the_filesystem():
    rule = parse_rule(RULE, source="inline", known_principles={"structured-logging"})

    assert rule.id == "no-bare-print"


# --- doctor loads them, which is what the authoring doc promises ---------

def test_doctor_reports_a_broken_rule_with_the_file_named(tmp_path):
    """`docs/rules-authoring.md` says doctor loads every rule so that a broken
    one is found by asking rather than during an unattended run."""
    from curamoir.doctor import diagnose

    repo = write(tmp_path, RULE.replace("principle: structured-logging",
                                        "principle: nonexistent"))
    (repo / "pyproject.toml").write_text("", encoding="utf-8")

    finding = next(f for f in diagnose(repo) if f.check == "rules")

    assert finding.level == "error"
    assert "no-bare-print.yaml" in finding.detail
    assert "nonexistent" in finding.detail


def test_doctor_counts_loadable_rules_by_matcher(tmp_path):
    from curamoir.doctor import diagnose

    repo = write(tmp_path, RULE)
    (repo / "pyproject.toml").write_text("", encoding="utf-8")

    finding = next(f for f in diagnose(repo) if f.check == "rules")

    assert finding.level == "ok"
    assert "1 loaded" in finding.detail
    assert "regex: 1" in finding.detail


def test_doctor_says_so_when_nothing_is_mechanically_enforced(tmp_path):
    """A repo with principles and no rules is a working repo, not a broken one
    — every principle is enforced by review. But it is worth saying."""
    from curamoir.doctor import diagnose
    from curamoir.scaffold import init_repo

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)

    finding = next(f for f in diagnose(tmp_path) if f.check == "rules")

    assert finding.level == "warning"
    assert "rules-authoring" in (finding.fix or "")
