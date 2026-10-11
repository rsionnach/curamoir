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
import time
from pathlib import Path

import pytest

from curamoir.lint import (
    EXIT_CLEAN,
    EXIT_ERROR,
    EXIT_VIOLATIONS,
    NOTE_KINDS,
    RECORD_FIELDS,
    SCHEMA_VERSION,
    TOP_LEVEL_FIELDS,
    LintError,
    fingerprint,
    main,
    run_lint,
)
from curamoir.principles import load_principles
from curamoir.rules import AST_GREP_LANGUAGES

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "sample-repo"
WRAPPER = ROOT / "scripts" / "curamoir-lint"
PLANTED = json.loads((FIXTURE / "PLANTED.json").read_text(encoding="utf-8"))

# The tests that run the real ast-grep skip where it is not on PATH, which on a
# machine meant to have it would let them pass having run nothing. So where it
# is required, its absence fails test_ast_grep_is_installed_where_it_is_required
# instead: set CURAMOIR_REQUIRE_AST_GREP=1 to require it, or run under CI
# (`CI` set and not "0"/"false", as GitHub Actions and most CI services do).
# CI is safe to require it of because the `dev` extras install ast-grep-cli.
needs_ast_grep = pytest.mark.skipif(shutil.which("ast-grep") is None,
                                    reason="ast-grep is not installed")


def _ast_grep_required():
    def truthy(name):
        return os.environ.get(name, "").strip().lower() not in ("", "0", "false")
    return truthy("CURAMOIR_REQUIRE_AST_GREP") or truthy("CI")

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
        # Each file is named for its rule, so every rule text here must open
        # with its `id:` line; parsing the YAML would make this helper
        # depend on what it is helping to test.
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


def fake_ast_grep(tmp_path, stdout="", stderr="", exit_code=0, log=None, summary=True,
                  sleep=0, skipped=0, version="ast-grep 0.50.0\n",
                  version_stream="stdout", version_sleep=0):
    """An executable standing in for ast-grep, for the paths the real binary
    cannot be made to take on demand (and for machines without it). With `log`,
    each call appends its argv and working directory there as a JSON line.

    With `summary`, it also writes the two lines real ast-grep 0.50.0 writes
    to stderr under `--inspect=summary`, in its order: the project line
    (`projectDir` is the directory of the `--config` file, as the real binary
    reports it), then the file line, counting every file it was given as
    scanned and `skipped` of them as skipped (the runner refuses output
    without it). So the runner's "other inspect lines are ignored" path is
    taken by default. The interpreter is this one, named absolutely, so a test
    may give the runner a PATH holding nothing but the fake.

    `--version` is answered with `version` (by default what 0.50.0 prints) and
    exit 0, before any sleep, and logged like any call: the runner asks once
    per run for the JSON's `tools`, and a fake that answered it with match
    JSON would test nothing. `version_stream` is where that answer goes and
    `version_sleep` how long it takes, for a probe that misbehaves."""
    script = tmp_path / "bin" / "ast-grep"
    script.parent.mkdir(parents=True, exist_ok=True)
    record = (
        f"open({str(log)!r}, 'a').write(json.dumps("
        "{'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        if log is not None else ""
    )
    inspect = (
        "files = sys.argv[sys.argv.index('--') + 1:]\n"
        "config = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--config=')]\n"
        "project = ('isProject=true,projectDir=' + os.path.dirname(config[0])"
        " if config else 'isProject=false')\n"
        "sys.stderr.write(f'sg: summary|project: {project}\\n')\n"
        "sys.stderr.write(f'sg: summary|file: scannedFileCount={len(files)},"
        f"skippedFileCount={skipped}\\n')\n"
        if summary else ""
    )
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        f"{record}"
        "if sys.argv[1:] == ['--version']:\n"
        f"    time.sleep({version_sleep})\n"
        f"    sys.{version_stream}.write({version!r})\n"
        "    sys.exit(0)\n"
        f"time.sleep({sleep})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"{inspect}"
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
        "end_line": 15,
        "end_col": 11,
        "fix": "logger.info(...), or logger.debug(...) while debugging",
        "fix_source": "rule",
        "why_link": ".curamoir/principles.md#structured-logging",
        "match": "print(",
        "fingerprint": fingerprint("no-bare-print", "src/sample/core/billing.py", "print(", 0),
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
    assert record["fix_source"] == "principle"
    assert record["location"] == "src/sample/web/handlers_v2.py:1:1"
    assert record["message"] == "src/sample/web/handlers_v2.py is a parallel variant of another module"


def test_the_json_document_has_a_stable_top_level_shape(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert tuple(document) == TOP_LEVEL_FIELDS
    assert document["schema_version"] == SCHEMA_VERSION == 1
    # No ast-grep rule ran, so no ast-grep version is claimed.
    assert document["tools"] == {"ast-grep": None}
    assert document["notes"] == []
    assert document["fail_on"] == "error"
    assert document["complete"] is True
    assert document["rules_checked"] == ["no-bare-print", "no-unchained-raise", "no-variant-module"]
    assert document["rules_skipped"] == []


def test_the_documented_json_shape_is_the_one_emitted():
    """docs/rules-authoring.md is where a sweep author learns the contract, so
    its example and its field table must be the fields the runner emits."""
    import re

    doc = (ROOT / "docs" / "rules-authoring.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)```", doc, re.DOTALL).group(1))
    table = re.findall(r"^\| `([a-z_]+)`(?:, `([a-z_]+)`)? \|", doc.split("### JSON")[1], re.M)
    documented = {name for pair in table for name in pair if name}

    assert tuple(example) == TOP_LEVEL_FIELDS
    assert tuple(example["violations"][0]) == RECORD_FIELDS
    assert documented == set(RECORD_FIELDS)
    assert example["schema_version"] == SCHEMA_VERSION
    # Every note kind the runner can emit is in the doc's list of them.
    kinds = set(re.findall(r"^- `([a-z_]+)`", doc.split("#### Notes")[1].split("###")[0], re.M))
    assert kinds == set(NOTE_KINDS)


def test_the_documented_human_output_is_the_one_emitted(tmp_path, capsys):
    """The doc's example is the fixture's real output, abbreviated at `...`:
    the lines before it open the report and the line after it ends it."""
    doc = (ROOT / "docs" / "rules-authoring.md").read_text(encoding="utf-8")
    example = doc.split("### Human (the default)")[1].split("```")[1].strip("\n").splitlines()
    head, tail = example[:example.index("...")], example[example.index("...") + 1:]
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, capsys=capsys)

    lines = out.splitlines()
    assert lines[:len(head)] == head
    assert lines[-len(tail):] == tail


def test_the_documented_ast_grep_suffixes_are_the_mapped_ones():
    """Rule authors read the suffix list in docs/rules-authoring.md; the runner
    reads AST_GREP_LANGUAGES. They must be the same list."""
    import re

    doc = (ROOT / "docs" / "rules-authoring.md").read_text(encoding="utf-8")
    passage = doc.split("searches only suffixes with a mapped grammar:")[1].split("(each checked")[0]

    assert set(re.findall(r"`(\.[a-z]+)`", passage)) == set(AST_GREP_LANGUAGES)


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
    # The end is 1-based and exclusive: the column just after the match's last
    # character (ast-grep's 0-based exclusive end, plus one). Each match is 31
    # and 37 characters long, starting at column 5.
    assert [(v.end_line, v.end_col) for v in result.violations] == [(15, 36), (22, 42)]


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


def test_ast_grep_is_installed_where_it_is_required():
    """The @needs_ast_grep tests skip without the binary; where it is required
    (see needs_ast_grep), a skip would be a gate passing unchecked."""
    if shutil.which("ast-grep") is not None:
        return
    if not _ast_grep_required():
        pytest.skip("ast-grep is not required here (CURAMOIR_REQUIRE_AST_GREP / CI unset)")
    pytest.fail(
        "ast-grep is required here but not on PATH; install the dev extras "
        "(ast-grep-cli), or the real-binary tests silently skip")


@needs_ast_grep
def test_the_real_ast_grep_finds_the_planted_prints(tmp_path):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    planted = sorted((v["path"], v["line"]) for v in PLANTED["violations"]
                     if v["principle_id"] == "structured-logging")
    assert sorted((v.path, v.line) for v in result.violations) == planted


@needs_ast_grep
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
    # The `--version` query (for the JSON's `tools`) runs from a neutral
    # directory too, though it takes no config and names no files.
    assert [call["argv"] for call in calls if call["argv"][0] != "run"] == [["--version"]]
    assert any(call["argv"][0] == "run" for call in calls)
    for call in calls:
        cwd = Path(call["cwd"]).resolve()
        assert root != cwd and root not in cwd.parents
        if call["argv"][0] != "run":
            continue
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


@needs_ast_grep
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


def _shims(tmp_path, *flags, without_yaml=(), safepath=True):
    """A PATH directory where every interpreter name the wrapper tries runs
    THIS interpreter with `flags` — so the test, not whatever Homebrew left on
    the machine, decides what the wrapper gets. Names in `without_yaml` are
    interpreters that exist but cannot import PyYAML.

    With `safepath=False` each shim unsets PYTHONSAFEPATH before it execs, so
    the interpreter behaves as a pre-3.11 one does, which ignores it: the
    working directory goes back at the front of sys.path, and only the
    wrapper's `cd` into its own `src/` keeps the linted repo off it."""
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    unset = "" if safepath else "unset PYTHONSAFEPATH\n"
    for name in ("python3.14", "python3.13", "python3.12", "python3", "python"):
        if name in without_yaml:
            body = f'exec "{sys.executable}" -S -c "raise SystemExit(\'no PyYAML here\')"'
        else:
            body = f'{unset}exec "{sys.executable}" {" ".join(flags)} "$@"'
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


def test_the_lint_modules_parse_as_python_3_9():
    """The wrapper may fall back to a 3.9 interpreter (cm-bzb), where newer
    syntax is an import error and Python's exit 1 would read as violations.
    `feature_version` catches grammar, not newer library calls; the hand run
    under 3.9 covers those."""
    import ast

    for name in ("__init__", "lint", "rules", "principles", "config"):
        source = (ROOT / "src" / "curamoir" / f"{name}.py").read_text(encoding="utf-8")
        ast.parse(source, filename=f"{name}.py", feature_version=(3, 9))


# --- the wrapper imports nothing from the repository it lints ----------------

HOSTILE_MODULES = ("yaml.py", "json.py", "tempfile.py", "curamoir/__init__.py")


def _plant_hostile_modules(repo, markers):
    """Modules at the repo root that shadow what the runner imports. Each one,
    if imported, leaves a marker and kills the process with a distinctive code.
    No `print(` and no `raise`, so the test rules do not fire on them and the
    planted count is unchanged."""
    for name in HOSTILE_MODULES:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = markers / name.replace("/", "-")
        path.write_text(
            "import os, pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('imported')\n"
            "os._exit(77)\n",
            encoding="utf-8",
        )


# Each of the wrapper's two defences against importing from the linted repo is
# tested on its own: with PYTHONSAFEPATH honoured (3.11+, as here) and with it
# neutralised (a 3.9 fallback interpreter), where only the `cd` stands.
SAFEPATH = pytest.mark.parametrize(
    "safepath", [True, False], ids=["PYTHONSAFEPATH-honoured", "PYTHONSAFEPATH-ignored"])


@SAFEPATH
@pytest.mark.parametrize("repo_argument", [None, ".", "relative"])
def test_the_wrapper_never_imports_modules_from_the_repo_it_lints(tmp_path, repo_argument,
                                                                  safepath):
    """Security. Pre-commit and CI run the wrapper from the linted repo's root,
    and `python -m` / `python -c` put the working directory at the front of
    sys.path, ahead of PYTHONPATH — so a `yaml.py` or `curamoir/` committed in a
    PR would run as the linter. The wrapper must start Python somewhere neutral
    and still lint the directory it was invoked from (or the relative --repo it
    was given, resolved against where it was invoked)."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    markers = tmp_path / "markers"
    markers.mkdir()
    _plant_hostile_modules(repo, markers)
    if repo_argument == "relative":
        cwd, extra = tmp_path, ["--repo", repo.name]
    else:
        cwd, extra = repo, ([] if repo_argument is None else ["--repo", repo_argument])

    result = subprocess.run(
        [str(WRAPPER), *extra, "--format", "json"],
        capture_output=True, text=True, cwd=str(cwd),
        env=_environment(_shims(tmp_path, safepath=safepath)),
    )

    assert sorted(p.name for p in markers.iterdir()) == []
    assert result.returncode == EXIT_VIOLATIONS, result.stderr
    assert len(json.loads(result.stdout)["violations"]) == len(PLANTED["violations"])


def test_the_wrapper_names_a_missing_relative_repo_as_given(tmp_path):
    """The relative path the user typed is the one the error names, so moving
    the interpreter's working directory must not change what --repo means."""
    result = subprocess.run(
        [str(WRAPPER), "--repo", "nowhere"],
        capture_output=True, text=True, cwd=str(tmp_path),
        env=_environment(_shims(tmp_path)),
    )

    assert result.returncode == EXIT_ERROR
    assert "nowhere is not a directory" in result.stderr


@SAFEPATH
def test_a_relative_pythonpath_means_what_it_meant_where_the_wrapper_was_invoked(tmp_path,
                                                                                safepath):
    """The wrapper starts Python in its own `src/`, so an inherited relative
    `PYTHONPATH=vendor` (a CI job providing PyYAML that way) would resolve
    there instead, and the run would exit 2 for want of PyYAML. Each entry is
    made absolute against the invoking directory first. Empty entries, which
    Python reads as the working directory, are dropped: the working directory
    is the repository, and that is what Python must never import from."""
    import yaml

    invoked_from = tmp_path / "ci"
    shutil.copytree(Path(yaml.__file__).parent, invoked_from / "vendor" / "yaml",
                    ignore=shutil.ignore_patterns("__pycache__"))
    markers = tmp_path / "markers"
    markers.mkdir()
    _plant_hostile_modules(invoked_from, markers)
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = subprocess.run(
        [str(WRAPPER), "--repo", str(repo), "--format", "json"],
        capture_output=True, text=True, cwd=str(invoked_from),
        env=_environment(_shims(tmp_path, "-S", safepath=safepath), pythonpath=":vendor:"),
    )

    assert sorted(p.name for p in markers.iterdir()) == []
    assert result.returncode == EXIT_VIOLATIONS, result.stderr
    assert len(json.loads(result.stdout)["violations"]) == len(PLANTED["violations"])


# --- regex locations agree with editors and ast-grep -------------------------

def _locations(repo, path):
    result = run_lint(repo)
    return [(v.line, v.col, v.match) for v in result.violations if v.path == path]


@pytest.mark.parametrize(("content", "expected"), [
    # A form feed on its own line is a line to an editor, not a page break
    # that ends one line and starts another.
    (b'a = 1\n\x0c\nb = "\xc3\xa9"; print(1)\n', [(3, 10, "print(")]),
    # Mid-line form feed and vertical tab: str.splitlines() breaks on both.
    (b'a = 1 \x0c\x0b b = 2; print(1)\n', [(1, 17, "print(")]),
    # U+2028 and U+0085 inside a string literal are characters, not breaks.
    ('x = "\u2028\u0085"\nprint(1)\n'.encode("utf-8"), [(2, 1, "print(")]),
    # CRLF still counts one line per CRLF, and the \r is not part of a line.
    (b'a = 1\r\nprint(1)\r\nb = 2\r\n', [(2, 1, "print(")]),
    # A lone CR is not a line break to ast-grep (tree-sitter counts \n).
    (b'a = 1\rprint(1)\n', [(1, 7, "print(")]),
    # A UTF-8 BOM is not a column an editor shows.
    (b'\xef\xbb\xbfprint(1)\n', [(1, 1, "print(")]),
])
def test_regex_lines_and_columns_count_only_newlines(tmp_path, content, expected):
    repo = sample_repo(tmp_path, NO_BARE_PRINT)
    (repo / "src" / "edge.py").write_bytes(content)

    assert _locations(repo, "src/edge.py") == expected


def test_a_trailing_newline_does_not_add_a_phantom_empty_line(tmp_path):
    """Splitting on "\\n" leaves an empty last element after a final newline;
    a regex that matches an empty line must not report a line the editor does
    not have."""
    rule = NO_BARE_PRINT.replace("'(?<![\\w.])print\\('", "'^$'")
    repo = sample_repo(tmp_path, rule)
    (repo / "src" / "edge.py").write_bytes(b"a = 1\n\nb = 2\n")

    assert _locations(repo, "src/edge.py") == [(2, 1, "")]


@needs_ast_grep
@pytest.mark.parametrize(("content", "expected"), [
    (b'a = 1\n\x0c\nb = "\xc3\xa9"; print(1)\n', [(3, 10)]),
    (b'a = 1\r\nprint(1)\r\n', [(2, 1)]),
    (b'\xef\xbb\xbfprint(1); print(2)\nprint(3)\n', [(1, 1), (1, 11), (2, 1)]),
    (b'\xef\xbb\xbfx = "\xc3\xa9"; print(1)\n', [(1, 10)]),
])
def test_both_matchers_report_the_same_location_for_the_same_call(tmp_path, content,
                                                                   expected):
    """The real ast-grep is the reference for lines (it counts only `\n`), and
    an editor for columns (a BOM is not one): the regex rule and the ast-grep
    rule must put each `print(...)` call at the same line and column, and that
    must be where an editor shows it. Agreement alone is not enough — before,
    both matchers counted the BOM, and agreed on the wrong column."""
    repo = sample_repo(tmp_path, NO_BARE_PRINT, AST_GREP_PRINT)
    (repo / "src" / "edge.py").write_bytes(content)

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    by_rule = {
        rule: [(v.line, v.col) for v in result.violations
               if v.path == "src/edge.py" and v.rule_id == rule]
        for rule in ("no-bare-print", "no-bare-print-ast")
    }
    assert by_rule["no-bare-print"] == expected
    assert by_rule["no-bare-print-ast"] == expected


def test_ast_grep_columns_on_a_bom_file_are_corrected_to_the_editors(tmp_path):
    """ast-grep 0.50.0 counts a UTF-8 BOM as column 0 of line 0 (verified:
    `\\ufeffprint(1)` is reported at column 1). Replayed here so the correction
    is pinned on machines without the binary; lines after the first, and files
    without a BOM, are untouched."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    (repo / "src" / "bom.py").write_bytes(b"\xef\xbb\xbfprint(1)\nprint(2)\n")
    root = str(repo.resolve())
    recorded = json.dumps([
        {"text": "print(1)", "file": f"{root}/src/bom.py",
         "range": {"start": {"line": 0, "column": 1}, "end": {"line": 0, "column": 9}}},
        {"text": "print(2)", "file": f"{root}/src/bom.py",
         "range": {"start": {"line": 1, "column": 0}, "end": {"line": 1, "column": 8}}},
    ])

    result = run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=recorded, exit_code=0))

    assert [(v.line, v.col) for v in result.violations] == [(1, 1), (2, 1)]
    # The end of a line-1 match is shifted by the same BOM, and corrected the same.
    assert [(v.end_line, v.end_col) for v in result.violations] == [(1, 9), (2, 9)]


# --- an ast-grep rule never silently skips a file it was scoped to -----------

def _ast_grep_rule(scope):
    return AST_GREP_PRINT.replace("languages: [python]", scope)


def test_an_ast_grep_rule_scoped_to_files_with_no_grammar_is_skipped_loudly(tmp_path,
                                                                            capsys,
                                                                            monkeypatch):
    """Before: the `.md` file was dropped from the rule's file list, the rule was
    reported as checked, and the run exited 0 having checked nothing. A file
    the rule was scoped to and could not search makes the rule incomplete,
    handled like a missing ast-grep: skipped by name, partial report kept,
    exit 2."""
    repo = sample_repo(tmp_path, NO_UNCHAINED_RAISE, _ast_grep_rule('files: ["docs/**"]'))
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("print(1)\n", encoding="utf-8")
    fake = fake_ast_grep(tmp_path, stdout="[]", exit_code=1)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert code == EXIT_ERROR
    assert document["complete"] is False
    assert document["rules_checked"] == ["no-unchained-raise"]
    [skipped] = document["rules_skipped"]
    assert skipped["rule_id"] == "no-bare-print-ast"
    assert "docs/guide.md" in skipped["reason"]
    assert "no-bare-print-ast" in err


def test_an_ast_grep_rule_over_a_mapped_suffix_passes_the_language(tmp_path):
    """`.java`, `.cjs`, `.mts` were unmapped and silently dropped. Each now
    reaches ast-grep with the `--lang` 0.50.0 infers for that suffix."""
    repo = sample_repo(
        tmp_path, _ast_grep_rule('files: ["**/*.java", "**/*.cjs", "**/*.mts"]'),
    )
    for name in ("A.java", "b.cjs", "c.mts"):
        (repo / name).write_text("x\n", encoding="utf-8")
    log = tmp_path / "ast-grep-calls.jsonl"

    result = run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout="[]", exit_code=1, log=log))

    languages = sorted(
        arg.split("=", 1)[1]
        for line in log.read_text().splitlines()
        for arg in json.loads(line)["argv"] if arg.startswith("--lang=")
    )
    assert languages == ["java", "javascript", "typescript"]
    assert result.complete


def test_a_file_named_only_a_suffix_does_not_make_an_ast_grep_rule_skip(tmp_path):
    """A repo-root file named `.py` fully matches `**/*.py`, but has no suffix
    of its own (`Path(".py").suffix == ""`), so it read as a file with no
    grammar and skipped the whole rule: exit 2, forced by any commit. ast-grep
    under `--lang` does not search such a file either (see the real-binary
    test below), so it is left out of the rule's scope and the rule runs."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    (repo / ".py").write_text("print(1)\n", encoding="utf-8")
    log = tmp_path / "ast-grep-calls.jsonl"

    result = run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout="[]", exit_code=1, log=log))

    assert result.complete
    assert result.checked == ("no-bare-print-ast",)
    searched = []
    for line in log.read_text().splitlines():
        argv = json.loads(line)["argv"]
        if argv[0] != "run":
            continue
        searched += [Path(f).relative_to(repo.resolve()).as_posix()
                     for f in argv[argv.index("--") + 1:]]
    assert "src/sample/core/billing.py" in searched
    assert ".py" not in searched


@needs_ast_grep
def test_the_real_ast_grep_does_not_search_a_file_named_only_a_suffix(tmp_path):
    """Why leaving `.py` out of scope loses nothing: named explicitly, under
    `--lang=python`, ast-grep 0.50.0 finds no match in it."""
    (tmp_path / ".py").write_text("print(1)\n", encoding="utf-8")

    completed = subprocess.run(
        [shutil.which("ast-grep"), "run", "--pattern=print($$$A)", "--lang=python",
         "--json=compact", "--", str(tmp_path / ".py")],
        capture_output=True, text=True, cwd=str(tmp_path), check=False,
    )

    assert (completed.returncode, json.loads(completed.stdout)) == (1, [])


# --- writing the report is inside the gate -----------------------------------

def _run_module(repo, **kwargs):
    environment = _environment(os.environ["PATH"], pythonpath=str(ROOT / "src"))
    environment.update(kwargs.pop("env", {}))
    return subprocess.run(
        [sys.executable, "-m", "curamoir.lint", "--repo", str(repo)],
        env=environment, **kwargs,
    )


def test_a_report_the_terminal_cannot_encode_exits_two_not_one(tmp_path):
    """Exit 1 means "violations"; a crash while printing must not read as one.
    Here the only violation is a warning (exit 0 if printed), and its match
    contains a character stdout's encoding cannot represent."""
    rule = NO_BARE_PRINT.replace("severity: error", "severity: warning").replace(
        "'(?<![\\w.])print\\('", "'print\\(.*'")
    repo = sample_repo(tmp_path, rule)
    shutil.rmtree(repo / "src")
    (repo / "src").mkdir()
    (repo / "src" / "only.py").write_text('print("caf\u00e9")\n', encoding="utf-8")

    result = _run_module(repo, capture_output=True, text=True,
                         env={"PYTHONIOENCODING": "ascii"})

    assert result.returncode == EXIT_ERROR, result.stderr
    assert "Traceback" not in result.stderr
    assert "PYTHONIOENCODING" in result.stderr


def test_a_closed_stdout_exits_two_without_a_traceback(tmp_path):
    """`curamoir-lint | head -0`: the reader has gone before the report is
    written. Not a lint result, so 2, and no traceback from the flush Python
    attempts at shutdown."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        result = _run_module(repo, stdout=write_end, stderr=subprocess.PIPE, text=True)
    finally:
        os.close(write_end)

    assert result.returncode == EXIT_ERROR, result.stderr
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr


# --- nothing in scope goes unchecked without the run saying so ---------------

def test_ast_grep_skipping_a_file_it_was_given_skips_the_rule(tmp_path):
    """ast-grep 0.50.0 silently skips a file it cannot read, cannot decode as
    UTF-8, or finds too large (a 4 MB file is skipped, a 3 MB one is not):
    `[]`, exit 1, nothing on stderr — "no matches". Only `--inspect=summary`
    says so. A rule that did not search every file in its scope is skipped,
    not checked. Replayed here so the large-file case needs no large file."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="[]\n", exit_code=1, skipped=1)

    result = run_lint(repo, ast_grep=fake)

    assert not result.complete
    [skipped] = result.skipped
    assert skipped.rule_id == "no-bare-print-ast"
    assert "skipped 1" in skipped.reason


@needs_ast_grep
def test_the_real_ast_grep_skipping_a_latin1_file_skips_the_rule_and_names_it(tmp_path,
                                                                              capsys):
    repo = sample_repo(tmp_path, NO_UNCHAINED_RAISE, AST_GREP_PRINT)
    (repo / "src" / "legacy.py").write_bytes(
        b"# -*- coding: latin-1 -*-\nname = 'caf\xe9'\nprint(name)\n")

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert code == EXIT_ERROR
    assert document["rules_checked"] == ["no-unchained-raise"]
    [skipped] = document["rules_skipped"]
    assert skipped["rule_id"] == "no-bare-print-ast"
    assert "src/legacy.py" in skipped["reason"]
    assert "no-bare-print-ast" in err


def test_ast_grep_output_without_its_summary_is_a_runner_error(tmp_path):
    """No summary line means no evidence that every file was searched."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    with pytest.raises(LintError, match="no-bare-print-ast"):
        run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout="[]\n", exit_code=1,
                                              summary=False))


def test_an_inspect_line_containing_ERROR_is_not_an_ast_grep_failure(tmp_path):
    """The summary echoes the project directory, which is a temp path the
    runner does not choose; an `ERROR` in it is not ast-grep reporting one."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(
        tmp_path, stdout="[]\n", exit_code=1,
        stderr="sg: summary|project: isProject=true,projectDir=/tmp/ERROR-dir\n")

    assert run_lint(repo, ast_grep=fake).complete


@pytest.mark.parametrize(("stdout", "exit_code"), [
    ("", 0), ("", 1), ("{}", 0), ('[{"text": "print(1)"}]', 0), ("[1]", 0),
])
def test_ast_grep_output_that_is_not_a_list_of_matches_is_a_runner_error(tmp_path, stdout,
                                                                         exit_code):
    """Real 0.50.0 prints `[]` when nothing matches. Empty output, or JSON of
    another shape, is not "no matches"."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    with pytest.raises(LintError, match="no-bare-print-ast"):
        run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=stdout, exit_code=exit_code))


@pytest.mark.parametrize("end", [None, {"line": 0, "column": 3}, {"line": 0, "column": -1}])
def test_an_ast_grep_record_without_a_usable_end_is_a_runner_error(tmp_path, end):
    """Missing, or before its start: the region would be invented, and E3-R2's
    "fix only the violation" would edit the wrong span."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    span = {"start": {"line": 0, "column": 4}}
    if end is not None:
        span["end"] = end
    record = {"text": "print(1)", "file": str(repo.resolve() / "src" / "x.py"), "range": span}

    with pytest.raises(LintError, match="no-bare-print-ast"):
        run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=json.dumps([record])))


def test_an_ast_grep_call_that_does_not_return_is_a_runner_error(tmp_path):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="[]\n", exit_code=1, sleep=30)

    started = time.monotonic()
    with pytest.raises(LintError, match="no-bare-print-ast"):
        run_lint(repo, ast_grep=fake, ast_grep_timeout=0.5)

    assert time.monotonic() - started < 10


def test_the_run_timeout_bounds_a_hung_ast_grep(tmp_path, capsys, monkeypatch):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="[]\n", exit_code=1, sleep=30)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    started = time.monotonic()
    code, out, err = lint(repo, "--timeout", "1", capsys=capsys)

    assert time.monotonic() - started < 10
    assert code == EXIT_ERROR
    assert out == ""
    assert "did not finish within 1" in err


def test_the_run_timeout_bounds_a_backtracking_regex(tmp_path, capsys):
    """A rule's regex runs in this process, and Python cannot interrupt the
    regex engine except by a signal. `(x+x+)+y` on forty x's runs for hours."""
    rule = NO_BARE_PRINT.replace("'(?<![\\w.])print\\('", "'(x+x+)+y'")
    repo = sample_repo(tmp_path, rule)
    (repo / "src" / "slow.py").write_text("x" * 40 + "\n", encoding="utf-8")

    started = time.monotonic()
    code, _, err = lint(repo, "--timeout", "1", capsys=capsys)

    assert time.monotonic() - started < 10
    assert code == EXIT_ERROR
    assert "did not finish within 1" in err


def test_the_default_run_timeout_is_ten_minutes():
    from curamoir.lint import DEFAULT_TIMEOUT
    assert DEFAULT_TIMEOUT == 600
    assert "--timeout" in (ROOT / "docs" / "rules-authoring.md").read_text(encoding="utf-8")


def test_a_fifo_is_not_read_and_is_reported(tmp_path):
    """Opening a FIFO blocks until something writes to it, which in CI is
    never. Like a symlink it is not content git can store, so it is reported
    by name and not linted. Run as a subprocess so a regression hangs only it."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    os.mkfifo(repo / "src" / "pipe.py")

    result = _run_module(repo, capture_output=True, text=True, timeout=60)

    assert result.returncode == EXIT_VIOLATIONS, result.stderr
    assert "src/pipe.py" in result.stderr


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a directory whatever its mode")
def test_an_unreadable_directory_is_a_runner_error_not_a_pruned_one(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    locked = repo / "src" / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text("print('unseen')\n", encoding="utf-8")
    locked.chmod(0)
    try:
        code, _, err = lint(repo, "--format", "json", capsys=capsys)
    finally:
        locked.chmod(0o755)

    assert code == EXIT_ERROR
    assert "src/locked" in err


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a file whatever its mode")
def test_an_unreadable_file_skips_its_regex_rule_and_keeps_the_report(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE)
    unreadable = repo / "src" / "secret.py"
    unreadable.write_text("print('unseen')\n", encoding="utf-8")
    unreadable.chmod(0)
    try:
        code, out, _ = lint(repo, "--format", "json", capsys=capsys)
    finally:
        unreadable.chmod(0o644)

    document = json.loads(out)
    assert code == EXIT_ERROR
    assert document["rules_checked"] == ["no-variant-module"]
    [skipped] = document["rules_skipped"]
    assert skipped["rule_id"] == "no-bare-print"
    assert "src/secret.py" in skipped["reason"]
    assert document["violations"]


def test_an_empty_repo_argument_is_refused(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--repo", ""])

    assert caught.value.code == EXIT_ERROR
    assert "--repo" in capsys.readouterr().err


def test_abbreviated_options_are_refused(tmp_path, capsys):
    """`--re` must not quietly mean `--repo`: options are a gate's contract,
    and a later `--report` would silently change what an old abbreviation does."""
    with pytest.raises(SystemExit):
        main(["--re", str(tmp_path)])


def test_a_build_directory_below_the_root_is_source_and_is_linted(tmp_path, capsys):
    """`build`, `dist` and `venv` are output directories at the root, and
    ordinary package names below it (`src/app/build/`, `cmd/dist/`)."""
    repo = sample_repo(tmp_path, NO_BARE_PRINT)
    for directory in ("src/app/build", "cmd/dist", "build", "dist"):
        (repo / directory).mkdir(parents=True)
        (repo / directory / "m.py").write_text("print('x')\n", encoding="utf-8")

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    paths = {r["path"] for r in json.loads(out)["violations"]}
    assert {"src/app/build/m.py", "cmd/dist/m.py"} <= paths
    assert "build/m.py" not in paths and "dist/m.py" not in paths


def test_tooling_directories_are_pruned_at_any_depth(tmp_path, capsys):
    repo = sample_repo(tmp_path, NO_BARE_PRINT)
    for directory in ("packages/web/node_modules/x", "src/__pycache__", "sub/.venv/lib"):
        (repo / directory).mkdir(parents=True)
        (repo / directory / "m.py").write_text("print('x')\n", encoding="utf-8")

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    paths = {r["path"] for r in json.loads(out)["violations"]}
    assert paths == {"src/sample/core/billing.py", "src/sample/web/handlers.py"}


def test_the_wrapper_runs_when_invoked_through_a_symlink(tmp_path):
    """A symlink in a bin directory is how a checkout is usually put on PATH;
    the wrapper must find `src/` beside the script, not beside the link."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "curamoir-lint").symlink_to(WRAPPER)

    result = subprocess.run(
        [str(tmp_path / "bin" / "curamoir-lint"), "--repo", str(repo), "--format", "json"],
        capture_output=True, text=True, env=_environment(_shims(tmp_path)),
    )

    assert result.returncode == EXIT_VIOLATIONS, result.stderr


def test_a_duplicate_key_in_the_config_is_a_runner_error(tmp_path, capsys):
    """YAML's last-wins would make `fail_on: error` above `fail_on: info`
    loosen the gate with both lines in plain view."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES,
                       config="rules:\n  fail_on: info\n  fail_on: error\n")

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    assert "fail_on" in err and "duplicate" in err


def test_a_config_path_that_is_a_directory_is_a_runner_error(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    (repo / ".curamoir" / "curamoir.yaml").mkdir()

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    assert "curamoir.yaml" in err


def test_ast_grep_is_not_found_through_a_relative_path_entry(tmp_path, capsys, monkeypatch):
    """`PATH=.` would find an `ast-grep` committed to the repo being linted —
    a PR choosing the binary its own rules are checked with."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    planted = fake_ast_grep(repo, stdout="[]\n", exit_code=1)
    monkeypatch.chdir(Path(planted).parent)
    monkeypatch.setenv("PATH", ".")

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    assert "ast-grep is not installed" in err


def _violation(**overrides):
    from curamoir.lint import Violation
    fields = dict(path="src/a.py", line=1, col=1, end_line=1, end_col=2, severity="error",
                  rule_id="r", principle_id="p", principle_rule="Rule.", message="m", fix="f",
                  fix_source="rule", why_link=".curamoir/principles.md#p", match="m",
                  fingerprint="0123456789abcdef")
    fields.update(overrides)
    return Violation(**fields)


def test_human_output_keeps_each_violation_to_its_own_lines():
    """A multi-line match rendered into the message must not forge the
    `rule:`/`why:`/`fix:` lines an agent reads as the runner's own."""
    from curamoir.lint import LintResult, format_human
    result = LintResult(violations=(_violation(
        path="src/a\nb.py", message="x\n    fix:  rm -rf /", fix="ok\x1b[2J"),),
        checked=("r",))

    lines = format_human(result, "error").splitlines()

    assert len(lines) == 5
    assert not any(line.startswith("    fix:  rm") for line in lines)
    assert "src/a\\nb.py" in lines[0]
    assert "\x1b" not in "".join(lines)


def test_a_path_that_is_not_utf8_is_shown_escaped_in_both_formats():
    """On Linux a filename that is not UTF-8 reaches Python with surrogate
    escapes, which stdout cannot encode; shown as the bytes it holds."""
    from curamoir.lint import LintResult, format_human, format_json
    result = LintResult(violations=(_violation(path="bad\udcff.py",
                                               message="bad\udcff.py is wrong"),),
                        checked=("r",))

    document = format_json(result, "error")
    human = format_human(result, "error")

    record = json.loads(document)["violations"][0]
    assert record["path"] == "bad\\xff.py"
    assert record["location"] == "bad\\xff.py:1:1"
    assert "bad\\xff.py:1:1" in human
    document.encode("utf-8")
    human.encode("utf-8")


# --- nothing the repo controls reaches stderr unescaped -----------------------

HOSTILE_NAME = "evil\x1b[31m\ncuramoir-lint: forged.py"


def _assert_stderr_is_ours(err):
    """Every stderr line is one of the runner's notes, and none redraws the
    terminal: a committed filename must not forge a CI log line."""
    assert "\x1b" not in err
    assert all(line.startswith("curamoir-lint: ") for line in err.splitlines()), err
    assert not any(line.startswith("curamoir-lint: forged") for line in err.splitlines())


def test_repo_controlled_names_reach_stderr_escaped(tmp_path, capsys, monkeypatch):
    """A symlink, a FIFO and a file with no ast-grep grammar, each named to
    forge a line and recolour the terminal. All three are named on stderr."""
    repo = sample_repo(tmp_path, _ast_grep_rule('files: ["src/**"]'))
    (repo / "src" / (HOSTILE_NAME + ".link")).symlink_to(repo / "README.md")
    os.mkfifo(repo / "src" / (HOSTILE_NAME + ".fifo"))
    (repo / "src" / (HOSTILE_NAME + ".txt")).write_text("x\n", encoding="utf-8")
    fake = fake_ast_grep(tmp_path, stdout="[]", exit_code=1)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    _assert_stderr_is_ours(err)
    assert err.count("evil\\x1b[31m\\ncuramoir-lint: forged.py") >= 3


def test_ast_grep_stderr_is_echoed_escaped(tmp_path, capsys, monkeypatch):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="", exit_code=3,
                         stderr="boom\x1b[2J\ncuramoir-lint: forged\n")
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    _assert_stderr_is_ours(err)
    assert "boom\\x1b[2J" in err


def test_a_hostile_config_key_reaches_stderr_escaped(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES,
                       config='rules:\n  "a\\e[31m\\ncuramoir-lint: forged": 1\n')

    code, _, err = lint(repo, capsys=capsys)

    assert code == EXIT_ERROR
    _assert_stderr_is_ours(err)


# --- review minors -------------------------------------------------------------

@pytest.mark.parametrize("value", ["1e300", "1e7"])
def test_a_timeout_too_large_for_the_timer_is_a_usage_error(capsys, value):
    with pytest.raises(SystemExit) as caught:
        main(["--timeout", value])

    assert caught.value.code == EXIT_ERROR
    assert "--timeout" in capsys.readouterr().err


def _grandchild_spawning_ast_grep(tmp_path):
    """A wrapper-script install of ast-grep: a shell that starts the real work
    as a grandchild. Its pid is written down so the test can look for it."""
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "bin" / "ast-grep"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\n/bin/sleep 60 &\necho $! > '{pidfile}'\nwait\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return str(script), pidfile


def _gone_within(pid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    os.kill(pid, 9)  # do not leave it behind whatever the verdict
    return False


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_a_timed_out_ast_grep_call_leaves_no_grandchild(tmp_path):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake, pidfile = _grandchild_spawning_ast_grep(tmp_path)

    with pytest.raises(LintError):
        run_lint(repo, ast_grep=fake, ast_grep_timeout=1)

    assert _gone_within(int(pidfile.read_text()), 5)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_the_run_timeout_leaves_no_ast_grep_grandchild(tmp_path, capsys, monkeypatch):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake, pidfile = _grandchild_spawning_ast_grep(tmp_path)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, _, err = lint(repo, "--timeout", "1", capsys=capsys)

    assert code == EXIT_ERROR
    assert "did not finish within 1" in err
    assert _gone_within(int(pidfile.read_text()), 5)


def test_the_wrapper_runs_from_a_symlinked_scripts_directory(tmp_path):
    """`ln -s /opt/curamoir/scripts ~/tools`: the script itself is not a
    symlink, its directory is, and `src/` is beside the real one."""
    wrapper = _vendored_copy(tmp_path)
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "scripts").symlink_to(wrapper.parent, target_is_directory=True)

    result = subprocess.run(
        [str(tmp_path / "elsewhere" / "scripts" / "curamoir-lint"), "--repo", str(repo)],
        capture_output=True, text=True, env=_environment(_shims(tmp_path)),
    )

    assert result.returncode == EXIT_VIOLATIONS, result.stderr


@pytest.mark.parametrize("start", [{"line": -1, "column": 0}, {"line": 0, "column": -3}])
def test_a_negative_ast_grep_location_is_a_runner_error(tmp_path, start):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    record = {"text": "print(1)", "file": str(repo.resolve() / "src" / "x.py"),
              "range": {"start": start}}

    with pytest.raises(LintError, match="no-bare-print-ast"):
        run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=json.dumps([record])))


def test_a_size_guess_is_not_named_when_unreadable_files_explain_the_skip(tmp_path):
    """ast-grep says how many it skipped, not which. One file is not UTF-8 and
    one skip was reported, so the large file, which ast-grep searched, is not
    named as a suspect."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    (repo / "src" / "legacy.py").write_bytes(b"name = 'caf\xe9'\n")
    (repo / "src" / "big.py").write_text("x = 1\n" * 600_000, encoding="utf-8")

    result = run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout="[]", exit_code=1,
                                                   skipped=1))

    [skipped] = result.skipped
    assert "src/legacy.py: not UTF-8" in skipped.reason
    assert "big.py" not in skipped.reason


def test_a_partly_searched_ast_grep_rule_keeps_what_it_found(tmp_path, capsys, monkeypatch):
    """The rule is skipped (it did not search its whole scope, so exit 2), but
    the matches it found in the files it did search are real and reported."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout=recorded_output_for(repo), exit_code=0, skipped=1)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, out, _ = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert code == EXIT_ERROR
    assert document["complete"] is False
    assert document["rules_checked"] == []
    assert [s["rule_id"] for s in document["rules_skipped"]] == ["no-bare-print-ast"]
    assert [(v["rule_id"], v["line"]) for v in document["violations"]] == [
        ("no-bare-print-ast", 15), ("no-bare-print-ast", 22)]


def test_the_report_is_written_after_the_time_limit_is_lifted(tmp_path, monkeypatch):
    """An alarm during the write would leave half a JSON document on stdout,
    where an error run promises nothing. The report is rendered inside the
    bound and written outside it."""
    import contextlib
    import io

    import curamoir.lint as module

    state = {"bounded": False}
    writes = []

    @contextlib.contextmanager
    def recording_limit(seconds):
        state["bounded"] = True
        try:
            yield
        finally:
            state["bounded"] = False

    class Recorder(io.StringIO):
        def write(self, text):
            writes.append(state["bounded"])
            return super().write(text)

    monkeypatch.setattr(module, "_time_limit", recording_limit)
    monkeypatch.setattr(sys, "stdout", Recorder())
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    assert main(["--repo", str(repo), "--format", "json"]) == EXIT_VIOLATIONS
    assert writes and not any(writes)


@needs_ast_grep
@pytest.mark.parametrize("content", [b"   \n\t\n", b"\xef\xbb\xbf", b"\r\n\r\n"],
                         ids=["whitespace", "bom", "crlf"])
def test_a_file_with_no_code_is_not_a_false_skip(tmp_path, content):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    (repo / "src" / "blank.py").write_bytes(content)

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    assert result.complete, result.skipped


# --- the JSON contract settled before anything consumes it (cm-9jv) ----------
#
# The sweep (E3-R1) parses this output as its work list, and Epic 4 reads it
# for "introduced, not merely found" (E4-R4). These tests pin the fields those
# consumers need — a fingerprint, the end of each match, where the fix came
# from, which ast-grep produced the result, and the facts that were only on
# stderr — and the versioning promises around them.

def _print_rule(rule_id="print-call", pattern="print\\(.*?\\)"):
    """A regex rule quoting a whole single-line `print(...)` call, so its
    match text is what an ast-grep `print($$$ARGS)` rule would quote."""
    return NO_BARE_PRINT.replace("id: no-bare-print", f"id: {rule_id}").replace(
        "'(?<![\\w.])print\\('", f"'{pattern}'")


def _fingerprints(repo, path):
    return [v.fingerprint for v in run_lint(repo).violations if v.path == path]


# --- cm-9jv: the fingerprint -------------------------------------------------

def test_the_fingerprint_algorithm_is_pinned_to_one_exact_value():
    """Golden. A consumer compares fingerprints across runs, possibly across
    cúramóir versions, so the algorithm may not drift silently: changing it is
    a schema_version bump, and this value is what says it changed. Computed
    by hand from the documented recipe: sha256 over
    `13:no-bare-print,8:src/a.py,10:print("x"),1:0,`, first 16 hex digits."""
    assert fingerprint("no-bare-print", "src/a.py", 'print("x")', 0) == "08a4ac93b2a157d5"
    # Whitespace is not part of the match's identity.
    assert fingerprint("no-bare-print", "src/a.py", 'print( "x" )\n', 0) == "08a4ac93b2a157d5"


def test_the_fingerprint_separates_what_it_should_and_nothing_else():
    base = fingerprint("r", "src/a.py", "print(x)", 0)
    assert len(base) == 16 and all(c in "0123456789abcdef" for c in base)
    # Different rule, path, text or occurrence: different fingerprint.
    assert fingerprint("s", "src/a.py", "print(x)", 0) != base
    assert fingerprint("r", "src/b.py", "print(x)", 0) != base
    assert fingerprint("r", "src/a.py", "print(y)", 0) != base
    assert fingerprint("r", "src/a.py", "print(x)", 1) != base
    # Length-prefixed fields: moving characters between fields is not the same.
    assert fingerprint("ab", "c", "d", 0) != fingerprint("a", "bc", "d", 0)
    # Every ASCII whitespace character is dropped, wherever it is.
    assert fingerprint("r", "src/a.py", "print(\n    x,\t\r\f\v)", 0) == \
        fingerprint("r", "src/a.py", "print(x,)", 0)


def test_a_fingerprint_survives_a_line_move_and_a_whitespace_reformat(tmp_path):
    """E4-R4 needs "the same violation" to survive edits that do not touch it,
    or every unrelated change reads as violations introduced; E3-R7 needs a
    second sweep to recognise what it already saw."""
    repo = sample_repo(tmp_path, _print_rule())
    target = repo / "src" / "moved.py"
    target.write_text('def f():\n    print("x")\n', encoding="utf-8")
    before = _fingerprints(repo, "src/moved.py")

    target.write_text('import os\n\n\ndef f():\n        print( "x" )\n', encoding="utf-8")
    after = _fingerprints(repo, "src/moved.py")

    assert len(before) == 1
    assert after == before


def test_identical_matches_in_one_file_get_distinct_fingerprints(tmp_path):
    """Two `print("x")` in one file are the same (rule, path, text), so the
    occurrence index in file order tells them apart. Its stated limit: deleting
    the first shifts the second onto the first's fingerprint."""
    repo = sample_repo(tmp_path, _print_rule())
    target = repo / "src" / "twice.py"
    target.write_text('print("x")\nprint("y")\nprint("x")\n', encoding="utf-8")

    first, other, second = _fingerprints(repo, "src/twice.py")

    assert len({first, other, second}) == 3
    assert first == fingerprint("print-call", "src/twice.py", 'print("x")', 0)
    assert second == fingerprint("print-call", "src/twice.py", 'print("x")', 1)
    target.write_text('print("y")\nprint("x")\n', encoding="utf-8")
    assert _fingerprints(repo, "src/twice.py") == [other, first]


def test_every_fingerprint_in_a_report_is_unique(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES, _print_rule())

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    prints = [r["fingerprint"] for r in json.loads(out)["violations"]]
    assert len(prints) == len(set(prints)) > 1


def test_the_fingerprint_is_computed_from_the_records_own_fields(tmp_path, capsys):
    """So a consumer can recompute it from the JSON alone, the inputs are the
    record's `rule_id`, `path` and `match` exactly as emitted."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    seen = {}
    for record in json.loads(out)["violations"]:
        key = (record["rule_id"], record["path"], "".join(record["match"].split()))
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        assert record["fingerprint"] == fingerprint(
            record["rule_id"], record["path"], record["match"], occurrence)


# --- cm-9jv: where each match ends -------------------------------------------

def test_regex_ends_are_one_based_and_exclusive_on_the_same_line(tmp_path):
    """A `target: line` regex sees one line without its newline (and a pattern
    that needs a newline is refused at load), so its match never spans lines."""
    repo = sample_repo(tmp_path, _print_rule())
    (repo / "src" / "end.py").write_text('x = "é"; print("ab")\n', encoding="utf-8")

    (violation,) = [v for v in run_lint(repo).violations if v.path == "src/end.py"]

    assert (violation.line, violation.col, violation.end_line, violation.end_col) == (1, 10, 1, 21)
    assert violation.end_col - violation.col == len(violation.match)


def test_a_path_rule_ends_where_it_starts(tmp_path, capsys):
    """A `target: path` match is in the filename, not the file: its region is
    the empty one at 1:1, the file itself."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    record = next(r for r in json.loads(out)["violations"] if r["rule_id"] == "no-variant-module")
    assert (record["line"], record["col"], record["end_line"], record["end_col"]) == (1, 1, 1, 1)


def test_a_multi_line_ast_grep_match_ends_on_its_last_line(tmp_path):
    """Replayed from what 0.50.0 reports for a call spanning lines 3-5
    (`print(\\n  1,\\n  2)`): end line 4, column 4, both 0-based."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    (repo / "src" / "multi.py").write_text("x = 1\n\nprint(\n  1,\n  2)\n", encoding="utf-8")
    recorded = json.dumps([{
        "text": "print(\n  1,\n  2)", "file": f"{repo.resolve()}/src/multi.py",
        "range": {"start": {"line": 2, "column": 0}, "end": {"line": 4, "column": 4}},
    }])

    result = run_lint(repo, ast_grep=fake_ast_grep(tmp_path, stdout=recorded, exit_code=0))

    assert [(v.line, v.col, v.end_line, v.end_col) for v in result.violations] == [(3, 1, 5, 5)]


@needs_ast_grep
@pytest.mark.parametrize("content", [
    'x = "é"; print("ab")\n',
    '﻿print("ab"); print(1)\nprint(2)\n',
    'a = 1\r\n    print("tab\\there")\r\n',
])
def test_both_matchers_report_the_same_region_for_the_same_call(tmp_path, content):
    """The real ast-grep's end is 0-based and exclusive, counted in characters
    (checked against 0.50.0: `print("héllo")` at column 0 ends at column 14),
    so plus one is the regex matcher's 1-based exclusive end. Start and end
    must agree between the matchers, BOM file included."""
    repo = sample_repo(tmp_path, _print_rule(), AST_GREP_PRINT)
    (repo / "src" / "edge.py").write_bytes(content.encode("utf-8"))

    result = run_lint(repo, ast_grep=shutil.which("ast-grep"))

    by_rule = {
        rule: [(v.line, v.col, v.end_line, v.end_col, v.match) for v in result.violations
               if v.path == "src/edge.py" and v.rule_id == rule]
        for rule in ("print-call", "no-bare-print-ast")
    }
    assert by_rule["print-call"]
    assert by_rule["print-call"] == by_rule["no-bare-print-ast"]


# --- cm-9jv: fix_source and tools --------------------------------------------

def test_fix_source_says_where_the_fix_came_from(tmp_path, capsys):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    sources = {r["rule_id"]: r["fix_source"] for r in json.loads(out)["violations"]}
    assert sources == {"no-bare-print": "rule", "no-unchained-raise": "rule",
                       "no-variant-module": "principle"}


def test_the_ast_grep_version_is_reported_when_an_ast_grep_rule_ran(tmp_path, capsys,
                                                                    monkeypatch):
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="[]", exit_code=1, version="ast-grep 0.50.0\n")
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, out, _ = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert document["tools"] == {"ast-grep": "0.50.0"}
    assert document["notes"] == []
    assert code == EXIT_CLEAN


def test_ast_grep_is_not_asked_its_version_when_no_ast_grep_rule_runs(tmp_path, capsys,
                                                                      monkeypatch):
    """Installed but unused, it produced nothing in this report, so no version
    is claimed for it — and a run of regex rules does not depend on it."""
    repo = sample_repo(tmp_path, NO_BARE_PRINT)
    log = tmp_path / "ast-grep-calls.jsonl"
    fake = fake_ast_grep(tmp_path, stdout="[]", exit_code=1, log=log)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    assert json.loads(out)["tools"] == {"ast-grep": None}
    assert not log.exists()


@pytest.mark.parametrize("version", ["", "something else\n", "ast-grep\n"])
def test_an_unreadable_ast_grep_version_is_a_note_not_a_failure(tmp_path, capsys,
                                                                 monkeypatch, version):
    """The version is provenance, not evidence: each ast-grep call's own output
    is checked (its summary, its JSON) whatever the version says. So a version
    that cannot be read leaves a clean run clean, reported as null with a note
    saying why, rather than failing the gate over a label."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="[]", exit_code=1, version=version)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    document = json.loads(out)
    assert code == EXIT_CLEAN
    assert document["tools"] == {"ast-grep": None}
    assert [note["kind"] for note in document["notes"]] == ["ast_grep_version_unknown"]
    assert document["notes"][0]["reason"]
    assert "version" in err


def test_the_real_ast_grep_version_is_the_one_it_prints(tmp_path, capsys):
    binary = shutil.which("ast-grep")
    if binary is None:
        pytest.skip("ast-grep is not installed")
    printed = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             check=True).stdout.split()[-1]
    repo = sample_repo(tmp_path, AST_GREP_PRINT)

    _, out, _ = lint(repo, "--format", "json", capsys=capsys)

    assert json.loads(out)["tools"] == {"ast-grep": printed}


def test_a_version_on_stderr_is_named_as_stderr_not_as_printed(tmp_path, capsys,
                                                               monkeypatch):
    """The reason quotes what ast-grep said; quoting stderr as what it
    "printed" would send a reader looking at stdout for it."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="[]", exit_code=1, version_stream="stderr")
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    code, out, _ = lint(repo, "--format", "json", capsys=capsys)

    (note,) = json.loads(out)["notes"]
    assert note["reason"] == ("`ast-grep --version` exited 0 and printed nothing on stdout; "
                              "stderr: 'ast-grep 0.50.0'")
    assert code == EXIT_CLEAN


def test_a_hanging_version_probe_is_a_note_not_a_timed_out_run(tmp_path, capsys,
                                                               monkeypatch):
    """The probe has its own short bound, so a hang there costs that bound
    and a note, not the whole `--timeout` and an exit 2. The bound is patched
    small here only to keep the test fast."""
    import curamoir.lint as lint_module

    monkeypatch.setattr(lint_module, "AST_GREP_VERSION_TIMEOUT", 0.5)
    repo = sample_repo(tmp_path, AST_GREP_PRINT)
    fake = fake_ast_grep(tmp_path, stdout="[]", exit_code=1, version_sleep=30)
    monkeypatch.setenv("PATH", str(Path(fake).parent))

    started = time.monotonic()
    code, out, _ = lint(repo, "--format", "json", "--timeout", "60", capsys=capsys)

    assert time.monotonic() - started < 10
    document = json.loads(out)
    assert code == EXIT_CLEAN
    assert document["tools"] == {"ast-grep": None}
    assert document["notes"] == [{
        "kind": "ast_grep_version_unknown",
        "reason": "`ast-grep --version` did not finish within 0.5 seconds",
    }]


# --- cm-9jv: notes -----------------------------------------------------------

def test_notes_are_emitted_in_note_kinds_order_and_only_those_kinds():
    """NOTE_KINDS is the documented order; a run with one of each kind must
    list them in it, and nothing else."""
    from curamoir.lint import LintResult, report_notes
    result = LintResult(ast_grep_version_problem="it hung", symlinks=("s.link",),
                        not_regular=("pipe",))

    assert tuple(note["kind"] for note in report_notes(result)) == NOTE_KINDS


def test_a_note_kind_missing_from_note_kinds_is_refused(monkeypatch):
    """Emitting it would put an undocumented kind in the contract."""
    import curamoir.lint as lint_module

    monkeypatch.setattr(lint_module, "NOTE_KINDS", ("no_rules",))
    result = lint_module.LintResult(symlinks=("s.link",))

    with pytest.raises(ValueError, match="symlink_not_followed"):
        lint_module.report_notes(result)


def test_symlinks_and_other_non_files_are_notes_in_the_json(tmp_path, capsys):
    """Until now only stderr said so; a sweep reading the JSON could not tell
    a part of the tree had not been read. Paths are exact, like `path`."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    (repo / "src" / "b.link").symlink_to(repo / "README.md")
    (repo / "src" / "a.link").symlink_to(repo / "README.md")
    os.mkfifo(repo / "src" / "pipe.py")

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    assert json.loads(out)["notes"] == [
        {"kind": "symlink_not_followed", "path": "src/a.link"},
        {"kind": "symlink_not_followed", "path": "src/b.link"},
        {"kind": "not_regular_file", "path": "src/pipe.py"},
    ]
    # stderr is unchanged by this: the same two lines as before.
    assert "did not follow 2 symlink(s): src/a.link, src/b.link" in err
    assert "did not read 1 item(s) that are not regular files: src/pipe.py" in err
    assert code == EXIT_VIOLATIONS


def test_no_rules_is_a_note_in_the_json(tmp_path, capsys):
    repo = sample_repo(tmp_path)

    code, out, err = lint(repo, "--format", "json", capsys=capsys)

    assert json.loads(out)["notes"] == [{"kind": "no_rules"}]
    assert "nothing was checked" in err
    assert code == EXIT_CLEAN


def test_a_note_path_that_is_not_utf8_is_escaped_like_a_record_path():
    from curamoir.lint import LintResult, format_json
    result = LintResult(checked=("r",), symlinks=("bad\udcff.link",))

    document = json.loads(format_json(result, "error"))

    assert document["notes"] == [{"kind": "symlink_not_followed", "path": "bad\\xff.link"}]


# --- cm-9jv: version and CI pinning ------------------------------------------

def test_the_version_flag_names_the_tool_and_the_schema(capsys):
    from curamoir import __version__

    with pytest.raises(SystemExit) as exited:
        main(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out == f"curamoir-lint {__version__} (JSON schema {SCHEMA_VERSION})\n"


def test_the_package_version_matches_the_project_and_plugin_versions():
    """`--version` reads `curamoir.__version__` because the standalone runner
    has no installed metadata to ask; this keeps it from going stale."""
    import tomllib

    from curamoir import __version__

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert __version__ == project["project"]["version"] == plugin["version"]


def test_the_wrapper_prints_its_version(tmp_path):
    from curamoir import __version__

    result = subprocess.run([str(WRAPPER), "--version"], capture_output=True, text=True,
                            timeout=60, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"curamoir-lint {__version__} (JSON schema {SCHEMA_VERSION})\n"


def test_the_help_explains_each_format(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert "--format {human,json}" in help_text
    assert "json: the machine-readable report" in help_text


def test_the_ci_snippet_pins_curamoir_and_ast_grep():
    """An unpinned clone or ast-grep can change the gate's verdict between two
    CI runs of the same commit; the runner is verified against 0.50.0."""
    doc = (ROOT / "docs" / "rules-authoring.md").read_text(encoding="utf-8")
    snippet = doc.split("### In CI and pre-commit")[1].split("```")[1]

    assert "'ast-grep-cli==0.50.*'" in snippet
    assert "--branch v" in snippet
