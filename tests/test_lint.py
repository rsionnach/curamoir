"""E2-R4 + E2-R5: `curamoir-lint`, the runner that turns rules into a work list.

"Agent-readable" is the requirement, and it is a stronger one than it sounds:
the output is handed to a fresh agent that has NOT read `principles.md`, so the
one-sentence rule, the link to the principle's `why` and the fix all travel with
every violation. These tests therefore assert on the exact records the runner
emits, not on whether it "found something".

The other half is that it is a GATE. A lint gate that passes because it checked
nothing is the worst failure it can have — it looks exactly like a clean repo —
so every way of checking nothing (no ast-grep, ast-grep erroring, a rule that
does not load, a config that does not parse) is asserted to exit non-zero.

**Fixture provenance.** The planted violations come from
`fixtures/sample-repo/PLANTED.json`, which is generated from the files. The
rules below are TEST rules written for this file, not the bundled rules (those
are E2-R6, cm-766.5): they are regex so that the acceptance test runs on a
machine without ast-grep. The ast-grep JSON replayed by the stub binary was
captured verbatim from ast-grep 0.50.0 run against the fixture on 2026-10-09:

    ast-grep run --pattern 'print($$$ARGS)' --json=compact -- <fixture files>

and the real binary is exercised directly whenever it is installed.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from curamoir.lint import (
    EXIT_CLEAN,
    EXIT_ERROR,
    EXIT_VIOLATIONS,
    RECORD_FIELDS,
    LintError,
    main,
    run_lint,
)
from curamoir.principles import load_principles

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "sample-repo"
WRAPPER = ROOT / "scripts" / "curamoir-lint"
PLANTED = json.loads((FIXTURE / "PLANTED.json").read_text(encoding="utf-8"))

# --- test rules, one per principle the fixture plants a violation of ---------

NO_BARE_PRINT = textwrap.dedent("""\
    id: no-bare-print
    principle: structured-logging
    severity: error
    matcher: regex
    pattern: '(?<![\\w.])print\\('
    languages: [python]
    message: "{match} writes to stdout with no level or logger name"
    fix: "logger.info(...), or logger.debug(...) while debugging"
""")

NO_UNCHAINED_RAISE = textwrap.dedent("""\
    id: no-unchained-raise
    principle: errors-carry-context
    severity: warning
    matcher: regex
    pattern: '\\braise \\w+\\([^)]*\\)(?!\\s*from\\b)'
    languages: [python]
    message: "{match} discards the exception it replaces"
    fix: "raise NewError(f\\"what failed: {input}\\") from error"
""")

# No `fix:` on purpose: the runner must fall back to the principle's `good`
# example rather than hand the agent an empty remediation.
NO_VARIANT_MODULE = textwrap.dedent("""\
    id: no-variant-module
    principle: no-file-variant-proliferation
    severity: error
    matcher: regex
    pattern: '_(v[0-9]+|new|old)\\.py$'
    files: ["**/*.py"]
    target: path
    message: "{path} is a parallel variant of another module"
""")

AST_GREP_PRINT = textwrap.dedent("""\
    id: no-bare-print-ast
    principle: structured-logging
    severity: error
    matcher: ast-grep
    pattern: 'print($$$ARGS)'
    languages: [python]
    message: "{match} writes to stdout with no level or logger name"
    fix: "logger.info(...)"
""")

# Two records, verbatim from ast-grep 0.50.0 (see the module docstring).
RECORDED_AST_GREP_OUTPUT = (
    '[{"text":"print(\\"calculating line total\\")","range":{"byteOffset":{"start":263,"end":294},'
    '"start":{"line":14,"column":4},"end":{"line":14,"column":35}},"file":"src/sample/core/billing.py",'
    '"lines":"    print(\\"calculating line total\\")  # PLANTED: structured-logging",'
    '"charCount":{"leading":4,"trailing":31},"language":"Python","metaVariables":{"single":{},'
    '"multi":{"ARGS":[{"text":"\\"calculating line total\\"","range":{"byteOffset":{"start":269,"end":293},'
    '"start":{"line":14,"column":10},"end":{"line":14,"column":34}}}]},"transformed":{}}},'
    '{"text":"print(f\\"request failed: {exception}\\")","range":{"byteOffset":{"start":467,"end":504},'
    '"start":{"line":21,"column":4},"end":{"line":21,"column":41}},"file":"src/sample/web/handlers.py",'
    '"lines":"    print(f\\"request failed: {exception}\\")  # PLANTED: structured-logging",'
    '"charCount":{"leading":4,"trailing":31},"language":"Python","metaVariables":{"single":{},'
    '"multi":{"ARGS":[{"text":"f\\"request failed: {exception}\\"","range":{"byteOffset":{"start":473,"end":503},'
    '"start":{"line":21,"column":10},"end":{"line":21,"column":40}}}]},"transformed":{}}}]'
)


def sample_repo(tmp_path, *rules, config=None):
    """A copy of the fixture with `rules` installed, so nothing here edits it."""
    repo = tmp_path / "sample"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    directory = repo / ".curamoir" / "rules"
    directory.mkdir(parents=True, exist_ok=True)
    for text in rules:
        rule_id = text.split("\n", 1)[0].split(":", 1)[1].strip()
        (directory / f"{rule_id}.yaml").write_text(text, encoding="utf-8")
    if config is not None:
        (repo / ".curamoir" / "curamoir.yaml").write_text(config, encoding="utf-8")
    return repo


def lint(repo, *args, capsys):
    """`main` in-process: (exit code, stdout, stderr)."""
    code = main(["--repo", str(repo), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def fake_ast_grep(tmp_path, stdout="", stderr="", exit_code=0, log=None):
    """An executable standing in for ast-grep, for the paths the real binary
    cannot be made to take on demand (and for machines without it). With `log`,
    each call appends its argv and working directory there as a JSON line."""
    script = tmp_path / "bin" / "ast-grep"
    script.parent.mkdir(parents=True, exist_ok=True)
    record = (
        f"open({str(log)!r}, 'a').write(json.dumps("
        "{'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        if log is not None else ""
    )
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"{record}"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


ALL_REGEX_RULES = (NO_BARE_PRINT, NO_UNCHAINED_RAISE, NO_VARIANT_MODULE)


def recorded_output_for(repo):
    """RECORDED_AST_GREP_OUTPUT as ast-grep prints it for THIS copy of the
    fixture. The recording was made with repo-relative paths; the runner now
    passes absolute ones (see `_ast_grep_batch`), and ast-grep echoes a path
    exactly as it was given (checked against 0.50.0), so the only edit is to
    prefix the repo root onto `file`."""
    root = json.dumps(str(Path(repo).resolve()))[1:-1]
    return RECORDED_AST_GREP_OUTPUT.replace('"file":"src/', f'"file":"{root}/src/')


# --- acceptance: one record per planted violation, every field present -------

def test_json_output_has_one_record_per_planted_violation(tmp_path, capsys):
    """E2's acceptance criterion, verbatim: `--format json` on a fixture repo
    with planted violations returns one record per violation with all required
    fields. Compared as a set against PLANTED.json, so a missed violation and
    an invented one both fail."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    code, out, _ = lint(repo, "--format", "json", capsys=capsys)

    records = json.loads(out)["violations"]
    found = sorted((r["path"], r["line"], r["principle_id"]) for r in records)
    planted = sorted((v["path"], v["line"], v["principle_id"]) for v in PLANTED["violations"])
    assert found == planted
    assert code == EXIT_VIOLATIONS


def test_every_record_carries_exactly_the_documented_fields(tmp_path, capsys):
    """The JSON is the sweep's work list (E3-R1), so its shape is a contract: the
    exact field set and order, not "at least these". A field silently added or
    renamed breaks a parser somewhere else."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    for record in json.loads(out)["violations"]:
        assert tuple(record) == RECORD_FIELDS
        for name in RECORD_FIELDS:
            assert record[name] not in ("", None), f"{name} is empty in {record}"


def test_the_record_carries_the_principle_so_the_agent_need_not_read_it(tmp_path, capsys):
    """The point of agent-readable: rule sentence, why-link and fix travel with
    the violation. Asserted on the exact values for one planted line."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    principle = load_principles(repo).by_id("structured-logging")

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    record = next(
        r for r in json.loads(out)["violations"]
        if r["path"] == "src/sample/core/billing.py" and r["line"] == 15
    )
    assert record == {
        "location": "src/sample/core/billing.py:15:5",
        "path": "src/sample/core/billing.py",
        "line": 15,
        "col": 5,
        "severity": "error",
        "rule_id": "no-bare-print",
        "principle_id": "structured-logging",
        "principle_rule": principle.rule,
        "message": "print( writes to stdout with no level or logger name",
        "fix": "logger.info(...), or logger.debug(...) while debugging",
        "why_link": ".curamoir/principles.md#structured-logging",
        "match": "print(",
    }


def test_the_why_link_resolves_to_the_principles_heading(tmp_path, capsys):
    """The link is only useful if it lands somewhere. The anchor is the
    principle's `## <id>` heading, as a Markdown renderer slugs it."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    headings = (repo / ".curamoir" / "principles.md").read_text(encoding="utf-8")
    for record in json.loads(out)["violations"]:
        target, anchor = record["why_link"].split("#")
        assert (repo / target).is_file()
        assert f"\n## {anchor}\n" in headings


def test_a_rule_with_no_fix_falls_back_to_the_principles_good_example(tmp_path, capsys):
    """`fix` is optional in a rule because some principles have no mechanical
    remediation — but an agent handed an empty fix retries the same thing. The
    principle's `good` example is a corrected form by definition."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    good = load_principles(repo).by_id("no-file-variant-proliferation").good

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    record = next(r for r in json.loads(out)["violations"] if r["rule_id"] == "no-variant-module")
    assert record["fix"] == good
    assert record["location"] == "src/sample/web/handlers_v2.py:1:1"
    assert record["message"] == "src/sample/web/handlers_v2.py is a parallel variant of another module"


def test_the_json_document_has_a_stable_top_level_shape(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert tuple(document) == (
        "version", "fail_on", "complete", "rules_checked", "rules_skipped", "violations",
    )
    assert document["version"] == 1
    assert document["fail_on"] == "error"
    assert document["complete"] is True
    assert document["rules_checked"] == ["no-bare-print", "no-unchained-raise", "no-variant-module"]
    assert document["rules_skipped"] == []


def test_violations_are_ordered_by_location_so_runs_diff_cleanly(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, first, _ = lint(repo, "--format", "json", capsys=capsys)
    _, second, _ = lint(repo, "--format", "json", capsys=capsys)

    records = json.loads(first)["violations"]
    keys = [(r["path"], r["line"], r["col"], r["rule_id"]) for r in records]
    assert keys == sorted(keys)
    assert first == second


def test_human_output_leads_with_path_line_col_and_carries_the_same_facts(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    principle = load_principles(repo).by_id("structured-logging")

    _, out, _ = lint(repo, capsys=capsys)

    lines = out.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("src/sample/core/billing.py:15:5:"))
    block = "\n".join(lines[head:head + 4])
    assert "error" in lines[head]
    assert "structured-logging" in lines[head]
    assert "no-bare-print" in lines[head]
    assert principle.rule in block
    assert ".curamoir/principles.md#structured-logging" in block
    assert "logger.info(...)" in block
    assert "5 violations" in out


# --- the exit code is the gate ------------------------------------------------

def test_exit_is_non_zero_when_a_violation_meets_the_default_threshold(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT)

    code, _, _ = lint(repo, capsys=capsys)

    assert code == EXIT_VIOLATIONS == 1


def test_exit_is_zero_when_every_violation_is_below_the_threshold(tmp_path, capsys):
    """no-unchained-raise is a `warning` and the default threshold is `error`,
    so its two violations are reported but do not fail the gate."""
    repo = sample_repo(tmp_path, NO_UNCHAINED_RAISE)

    code, out, _ = lint(repo, "--format", "json", capsys=capsys)

    assert len(json.loads(out)["violations"]) == 2
    assert code == EXIT_CLEAN == 0


def test_the_threshold_flag_lowers_the_bar(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_UNCHAINED_RAISE)

    code, _, _ = lint(repo, "--fail-on", "warning", capsys=capsys)

    assert code == EXIT_VIOLATIONS


def test_the_threshold_is_read_from_curamoir_yaml(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_UNCHAINED_RAISE, config="rules:\n  fail_on: warning\n")

    code, out, _ = lint(repo, "--format", "json", capsys=capsys)

    assert json.loads(out)["fail_on"] == "warning"
    assert code == EXIT_VIOLATIONS


def test_the_flag_wins_over_the_config(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT, config="rules:\n  fail_on: info\n")

    code, out, _ = lint(repo, "--format", "json", "--fail-on", "error", capsys=capsys)

    assert json.loads(out)["fail_on"] == "error"
    assert code == EXIT_VIOLATIONS


def test_a_repo_with_rules_and_no_violations_exits_zero(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT)
    for path in (repo / "src").rglob("*.py"):
        path.write_text(path.read_text(encoding="utf-8").replace("print(", "logger.info("),
                        encoding="utf-8")

    code, out, _ = lint(repo, "--format", "json", capsys=capsys)

    assert json.loads(out)["violations"] == []
    assert code == EXIT_CLEAN


def test_excluded_paths_and_always_excluded_directories_are_not_searched(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT.replace(
        "languages: [python]", 'languages: [python]\nexclude: ["**/web/*.py"]'))
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "x.py").write_text("print('vendored')\n", encoding="utf-8")

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    assert [r["path"] for r in json.loads(out)["violations"]] == ["src/sample/core/billing.py"]


def test_a_root_level_file_matches_a_double_star_glob(tmp_path, capsys):
    """`**/*.py` must match `setup.py` at the root, not only files in a
    subdirectory — fnmatch gets this wrong, which is why the runner does not
    use it."""
    repo = sample_repo(tmp_path, NO_BARE_PRINT)
    (repo / "setup.py").write_text("print('root')\n", encoding="utf-8")

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    assert "setup.py" in [r["path"] for r in json.loads(out)["violations"]]


def test_a_symlink_is_not_followed_so_its_target_cannot_leak_into_the_output(tmp_path, capsys):
    """Security. `match` quotes the matched text, so a committed symlink
    `leak.py -> ~/.ssh/id_rsa` (or to an env file) in an untrusted PR would
    otherwise have CI print whatever of the target a rule matched. A symlink is
    not repository content — git stores only its target path — so it is not
    linted, neither the file kind nor the directory kind. Its NAME is reported
    on stderr, so the skip is not silent; the target's contents never are."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.env").write_text('print("TOKEN=hunter2")\n', encoding="utf-8")
    (outside / "tree").mkdir()
    (outside / "tree" / "module.py").write_text('print("TOKEN=hunter3")\n', encoding="utf-8")
    # The rule quotes the whole call, so a followed symlink would put the
    # target's text in `match`, as a real rule's broader pattern could.
    repo = sample_repo(tmp_path, NO_BARE_PRINT.replace("print\\('", "print\\(.*'"))
    (repo / "src" / "leak.py").symlink_to(outside / "secret.env")
    (repo / "src" / "linked").symlink_to(outside / "tree", target_is_directory=True)

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    assert "hunter" not in out and "hunter" not in err
    paths = {r["path"] for r in json.loads(out)["violations"]}
    assert paths == {"src/sample/core/billing.py", "src/sample/web/handlers.py"}
    assert "src/leak.py" in err and "src/linked" in err
    assert code == EXIT_VIOLATIONS


def test_symlinks_are_not_handed_to_ast_grep_either(tmp_path):
    """ast-grep reads what it is given, symlink or not (checked: 0.50.0
    matches through an explicitly named symlinked file), so the runner's file
    list is the only place to stop it."""
    outside = tmp_path / "outside.py"
    outside.write_text('print("TOKEN=hunter2")\n', encoding="utf-8")
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    (repo / "src" / "leak.py").symlink_to(outside)
    log = tmp_path / "ast-grep-calls.jsonl"

    run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout="[]", exit_code=1, log=log))

    argv = [arg for line in log.read_text().splitlines() for arg in json.loads(line)["argv"]]
    assert not any(arg.endswith("leak.py") for arg in argv)


# --- checking nothing is never reported as clean ------------------------------

def test_an_ast_grep_rule_without_ast_grep_is_skipped_loudly_and_fails(tmp_path, capsys,
                                                                        monkeypatch):
    """rules-authoring.md promised: skip with a warning rather than silently
    passing. The regex rules still run and are reported, so the work list is not
    lost — but the run is marked incomplete and exits 2, because a gate that
    passes on a machine missing its matcher passes everywhere it is missing."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    repo = sample_repo(tmp_path, NO_UNCHAINED_RAISE, AST_GREP_PRINT)

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert code == EXIT_ERROR == 2
    assert document["complete"] is False
    assert document["rules_checked"] == ["no-unchained-raise"]
    assert [s["rule_id"] for s in document["rules_skipped"]] == ["no-bare-print-ast"]
    assert "ast-grep" in document["rules_skipped"][0]["reason"]
    assert len(document["violations"]) == 2
    assert "no-bare-print-ast" in err
    assert "pip install ast-grep-cli" in err


def test_ast_grep_matches_are_converted_to_one_based_locations(tmp_path):
    """ast-grep reports 0-based lines and columns; editors, compilers and
    PLANTED.json are 1-based. Replayed from recorded real output so the
    conversion is pinned on machines without the binary."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    result = run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=recorded_output_for(repo),
                                                   exit_code=0))

    assert [(v.path, v.line, v.col, v.match) for v in result.violations] == [
        ("src/sample/core/billing.py", 15, 5, 'print("calculating line total")'),
        ("src/sample/web/handlers.py", 22, 5, 'print(f"request failed: {exception}")'),
    ]


def test_ast_grep_finding_nothing_is_clean(tmp_path):
    """ast-grep exits 1 when nothing matches, like grep. That is not an error."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    result = run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout="[]", exit_code=1))

    assert result.violations == ()
    assert result.complete


@pytest.mark.parametrize(("stdout", "stderr", "exit_code"), [
    ("[]", "", 2),
    ("[]", "ERROR: src/x.py: No such file or directory (os error 2)\n", 1),
    ("[]", "Warning: Pattern contains an ERROR node and may cause unexpected results.\n", 0),
    ("not json", "", 0),
])
def test_ast_grep_failing_is_a_runner_error_naming_the_rule(tmp_path, stdout, stderr, exit_code):
    """Each of these was observed or is documented, and each would otherwise read
    as "no matches": ast-grep exits 1 for a missing file AND for no matches, and
    exits 0 for a pattern it could not parse. So stderr is read, and an ERROR
    there fails the run with the rule named."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    with pytest.raises(LintError) as caught:
        run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=stdout, stderr=stderr,
                                              exit_code=exit_code))

    assert "no-bare-print-ast" in str(caught.value)


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is not installed")
def test_the_real_ast_grep_finds_the_planted_prints(tmp_path):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    planted = sorted((v["path"], v["line"]) for v in PLANTED["violations"]
                     if v["principle_id"] == "structured-logging")
    assert sorted((v.path, v.line) for v in result.violations) == planted


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is not installed")
def test_the_real_ast_grep_rejecting_a_pattern_fails_the_run(tmp_path):
    repo = sample_repo(tmp_path, AST_GREP_PRINT.replace("print($$$ARGS)", "print($$$ARGS"))

    with pytest.raises(LintError):
        run_lint(repo, ast_grep=shutil.which("ast-grep"))


def test_ast_grep_never_runs_where_it_could_find_the_repos_sgconfig(tmp_path):
    """Security. ast-grep looks for `sgconfig.yml` in its working directory and
    every ancestor, and `run` honours it (checked against 0.50.0): its
    `languageGlobs` can make a rule silently match nothing, a broken one fails
    the run, and its `customLanguages` name a native library that ast-grep
    versions without the `--custom-languages` opt-in load. A PR can add that
    file, so the runner points `--config` at an empty config of its own and
    runs from a scratch directory outside the repo, naming files absolutely."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    log = tmp_path / "ast-grep-calls.jsonl"

    run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout="[]", exit_code=1, log=log))

    root = repo.resolve()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls
    for call in calls:
        cwd = Path(call["cwd"]).resolve()
        assert root != cwd and root not in cwd.parents
        config = [arg.split("=", 1)[1] for arg in call["argv"] if arg.startswith("--config=")]
        assert len(config) == 1
        assert root not in Path(config[0]).resolve().parents
        files = call["argv"][call["argv"].index("--") + 1:]
        assert files and all(Path(f).is_absolute() and root in Path(f).parents for f in files)


def test_an_ast_grep_path_outside_the_repo_is_a_runner_error(tmp_path):
    """The runner names files absolutely and converts ast-grep's paths back.
    A path that does not convert is not dropped, which would read as clean."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    stray = RECORDED_AST_GREP_OUTPUT.replace('"file":"src/', '"file":"/elsewhere/src/')

    with pytest.raises(LintError) as caught:
        run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=stray, exit_code=0))

    assert "/elsewhere/src/sample/core/billing.py" in str(caught.value)


HOSTILE_SGCONFIGS = {
    # Observed at b3e7d87 with ast-grep 0.50.0: `.py` files re-mapped to
    # JavaScript are skipped under `--lang=python`, so the rule exits 1 —
    # "no matches" — and the gate passes having checked nothing.
    "languageGlobs": "ruleDirs: []\nlanguageGlobs:\n  javascript: ['*.py']\n",
    # Observed: ast-grep exits 8 ("Cannot parse configuration"), failing a
    # run that a PR could thereby block at will.
    "unparseable": "this: [is: not valid\n",
}


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is not installed")
@pytest.mark.parametrize("config", sorted(HOSTILE_SGCONFIGS))
def test_the_real_ast_grep_ignores_an_sgconfig_in_the_repo(tmp_path, config):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    (repo / "sgconfig.yml").write_text(HOSTILE_SGCONFIGS[config], encoding="utf-8")
    (repo / "src" / "sgconfig.yml").write_text(HOSTILE_SGCONFIGS[config], encoding="utf-8")

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    planted = sorted((v["path"], v["line"]) for v in PLANTED["violations"]
                     if v["principle_id"] == "structured-logging")
    assert sorted((v.path, v.line) for v in result.violations) == planted


def test_a_rule_citing_an_unknown_principle_is_a_runner_error(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT.replace("structured-logging", "no-such-principle"))

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    assert code == EXIT_ERROR
    assert out == ""
    assert "no-such-principle" in err


def test_rules_without_a_principles_file_are_a_runner_error(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT)
    (repo / ".curamoir" / "principles.md").unlink()

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    assert "structured-logging" in err


def test_an_invalid_config_is_a_runner_error_not_the_defaults(tmp_path, capsys):
    """Elsewhere a malformed config falls back to defaults so a hook keeps
    working. Not here: someone who set `fail_on: warning` and then broke the
    file would have their gate loosened without being told."""
    repo = sample_repo(tmp_path, NO_BARE_PRINT, config="rules:\n  fail_on: eror\n")

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    assert "rules.fail_on" in err


def test_no_rules_exits_zero_and_says_nothing_was_checked(tmp_path, capsys):
    """A fresh repo has no rules, and failing it would be hostile. But a pass
    that checked nothing must say so — on stderr, so the JSON stays clean."""
    repo = sample_repo(tmp_path)

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert code == EXIT_CLEAN
    assert document["rules_checked"] == []
    assert document["violations"] == []
    assert "no rules" in err


def test_a_missing_repo_is_a_runner_error(tmp_path, capsys):
    code, _, err = lint(tmp_path / "nowhere", capsys=capsys)

    assert code == EXIT_ERROR
    assert "nowhere" in err


# --- E2-R5: standalone, no network, no model ---------------------------------

def _vendored_copy(tmp_path):
    """scripts/curamoir-lint and src/, copied somewhere that is not the plugin
    root and not installed — what CI gets from `git clone`."""
    vendored = tmp_path / "vendored"
    (vendored / "scripts").mkdir(parents=True)
    shutil.copy2(WRAPPER, vendored / "scripts" / "curamoir-lint")
    shutil.copytree(ROOT / "src", vendored / "src",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return vendored / "scripts" / "curamoir-lint"


def _shims(tmp_path, *flags, without_yaml=()):
    """A PATH directory where every interpreter name the wrapper tries runs
    THIS interpreter with `flags` — so the test, not whatever Homebrew left on
    the machine, decides what the wrapper gets. Names in `without_yaml` are
    interpreters that exist but cannot import PyYAML."""
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for name in ("python3.14", "python3.13", "python3.12", "python3", "python"):
        if name in without_yaml:
            body = f'exec "{sys.executable}" -S -c "raise SystemExit(\'no PyYAML here\')"'
        else:
            body = f'exec "{sys.executable}" {" ".join(flags)} "$@"'
        shim = shims / name
        shim.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        shim.chmod(0o755)
    return f"{shims}:/usr/bin:/bin"


def _environment(path, pythonpath=None):
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "CLAUDE_PLUGIN_ROOT", "VIRTUAL_ENV")}
    environment["PATH"] = path
    if pythonpath:
        environment["PYTHONPATH"] = pythonpath
    return environment


def test_the_wrapper_runs_from_a_plain_checkout_without_curamoir_installed(tmp_path):
    """E2-R5. The interpreter is run with `-S`, so site-packages — where the
    editable install of cúramóir lives — is never consulted; only PyYAML's
    directory is added, because rule files are YAML. If the wrapper did not find
    `src/` beside itself, `import curamoir` would fail here."""
    import yaml

    wrapper = _vendored_copy(tmp_path)
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = subprocess.run(
        [str(wrapper), "--format", "json"],
        capture_output=True, text=True, cwd=str(repo),
        env=_environment(_shims(tmp_path, "-S"),
                         pythonpath=str(Path(yaml.__file__).resolve().parents[1])),
    )

    assert result.returncode == EXIT_VIOLATIONS, result.stderr
    assert len(json.loads(result.stdout)["violations"]) == len(PLANTED["violations"])


NETWORK_TRIPWIRE = '''\
"""Loaded by `site` before the runner. Any socket, or any import of an HTTP
or model client, aborts the process — so a pass proves none happened."""
import builtins, socket, sys
from pathlib import Path

Path(__file__).with_name("tripwire-loaded").write_text("yes")

def _refuse(*args, **kwargs):
    raise SystemExit("NETWORK: a socket was opened")

socket.socket = _refuse
socket.create_connection = _refuse

FORBIDDEN = ("anthropic", "ssl", "http.client", "urllib.request", "requests", "httpx")
_real_import = builtins.__import__

def _guarded(name, *args, **kwargs):
    if name in FORBIDDEN or name.split(".")[0] in ("anthropic", "requests", "httpx"):
        raise SystemExit(f"NETWORK: imported {name}")
    return _real_import(name, *args, **kwargs)

builtins.__import__ = _guarded
'''


def test_the_runner_makes_no_network_or_model_calls(tmp_path):
    """E2-R5 says "no model calls"; a model call is a network call, so the test
    forbids the network outright and fails if the tripwire never loaded."""
    tripwire = tmp_path / "tripwire"
    tripwire.mkdir()
    (tripwire / "sitecustomize.py").write_text(NETWORK_TRIPWIRE, encoding="utf-8")
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = subprocess.run(
        [str(WRAPPER), "--repo", str(repo), "--format", "json"],
        capture_output=True, text=True,
        env=_environment(_shims(tmp_path), pythonpath=str(tripwire)),
    )

    assert (tripwire / "tripwire-loaded").is_file(), "the tripwire never ran"
    assert "NETWORK" not in result.stderr
    assert result.returncode == EXIT_VIOLATIONS, result.stderr
    assert len(json.loads(result.stdout)["violations"]) == len(PLANTED["violations"])


def test_the_wrapper_reports_no_interpreter_as_a_runner_error(tmp_path):
    """Exit 2, not 0: a pre-commit hook on a machine with no Python must not
    wave every commit through."""
    result = subprocess.run(
        ["/bin/bash", str(WRAPPER), "--format", "json"],
        capture_output=True, text=True, cwd=str(tmp_path),
        env=_environment(str(tmp_path / "empty")),
    )

    assert result.returncode == EXIT_ERROR
    assert "python" in result.stderr


def test_the_wrapper_prefers_an_interpreter_that_can_read_rule_files(tmp_path):
    """Found by running this file: on the development machine `python3.14` is a
    bare Homebrew install and `python3.12` has PyYAML, and taking the first
    interpreter that exists refused to lint with a working one on PATH."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = subprocess.run(
        [str(WRAPPER), "--repo", str(repo), "--format", "json"],
        capture_output=True, text=True,
        env=_environment(_shims(tmp_path, without_yaml=("python3.14", "python3.13"))),
    )

    assert result.returncode == EXIT_VIOLATIONS, result.stderr
    assert len(json.loads(result.stdout)["violations"]) == len(PLANTED["violations"])


def test_with_no_pyyaml_anywhere_the_wrapper_says_so_and_exits_two(tmp_path):
    """Every candidate exists and none can import PyYAML (`-S` puts
    site-packages out of reach): the fallback interpreter runs, and the runner
    names what is missing rather than reporting a clean repo."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = subprocess.run(
        [str(WRAPPER), "--repo", str(repo)],
        capture_output=True, text=True, env=_environment(_shims(tmp_path, "-S")),
    )

    assert result.returncode == EXIT_ERROR
    assert "PyYAML" in result.stderr


def test_the_documented_json_shape_is_the_one_emitted():
    """docs/rules-authoring.md is where a sweep author learns the contract, so
    its example and its field table must be the fields the runner emits."""
    import re

    doc = (ROOT / "docs" / "rules-authoring.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)```", doc, re.DOTALL).group(1))
    table = re.findall(r"^\| `([a-z_]+)`(?:, `([a-z_]+)`)? \|", doc.split("### JSON")[1], re.M)
    documented = {name for pair in table for name in pair if name}

    assert tuple(example) == (
        "version", "fail_on", "complete", "rules_checked", "rules_skipped", "violations",
    )
    assert tuple(example["violations"][0]) == RECORD_FIELDS
    assert documented == set(RECORD_FIELDS)
