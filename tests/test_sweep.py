"""E3-R1, first clause (cm-fkd.1): the sweep runs `curamoir-lint` first and
treats its output as the work list.

What is tested is the step between "the lint ran" and "an agent edits code":
the sweep acts only on a report it can trust (exit 0 or 1, `complete`, a
schema it reads), and the list it hands on is exactly the lint's violations,
in an order that does not depend on how the lint happened to sort them.

**Fixture provenance.** The planted violations are `fixtures/sample-repo`'s,
from PLANTED.json `violations` (5 across 3 principles: E3's acceptance
fixture). The fixture ships no rules of its own, so the rules are installed
into a copy: the regex test rules from tests/test_lint.py, which that file
already pins to exactly those 5 records. They are not added to the fixture
itself because tests/test_lint.py asserts exact violation sets over copies of
it with its own rules installed, and tests/test_bundled_rules.py swaps in the
starter principles, under which fixture-local rules citing the fixture's
principles would no longer load.
"""

import io
import json
import os
import subprocess
import sys

import pytest

from curamoir import cli, lint, sweep
from curamoir.sweep import NO_WORK, WORK, LintUntrusted, work_list
from tests.test_lint import (
    ALL_REGEX_RULES,
    AST_GREP_PRINT,
    NO_BARE_PRINT,
    NO_UNCHAINED_RAISE,
    NO_VARIANT_MODULE,
    PLANTED,
    ROOT,
    needs_ast_grep,
    sample_repo,
)
from tests.test_lint_report import DOCUMENT, RECORD

PLANTED_VIOLATIONS = sorted(
    (v["path"], v["line"], v["principle_id"]) for v in PLANTED["violations"]
)


def fake_lint(tmp_path, stdout="", stderr="", exit_code=0, sleep=0):
    """An executable standing in for `curamoir-lint`, for the exits and
    reports the real runner cannot be made to produce on demand (a report
    that contradicts its own exit code, say)."""
    script = tmp_path / "bin" / "fake-curamoir-lint"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"time.sleep({sleep})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def report(**changes):
    return json.dumps(dict(DOCUMENT, **changes))


# --- acceptance: the work list is the 5 planted violations --------------------

def test_the_work_list_is_exactly_the_planted_violations(tmp_path):
    """E3's acceptance fixture, through the real `scripts/curamoir-lint`:
    5 violations across 3 principles, as a set, so a missed violation and an
    invented one both fail."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = work_list(repo)

    assert result.outcome == WORK
    assert sorted((i.path, i.line, i.principle_id) for i in result.items) == PLANTED_VIOLATIONS
    assert result.principle_ids == (
        "errors-carry-context", "no-file-variant-proliferation", "structured-logging",
    )


def test_every_severity_enters_the_work_list(tmp_path):
    """The lint's threshold decides its exit code, not what it reports, and
    the spec makes its output — not its failing part — the work list. The
    errors-carry-context rule is a `warning`, below the default `fail_on:
    error`; filtering at the threshold would drop 2 of the 5."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = work_list(repo)

    assert {i.severity for i in result.items} == {"error", "warning"}
    assert len(result.items) == 5


def test_the_work_list_is_ordered_by_principle_then_location(tmp_path):
    """The later PR body and fix counts are grouped by principle, so the
    order is principle first; the lint's own order (by path) would
    interleave them."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = work_list(repo)

    keys = [(i.principle_id, i.path, i.line, i.col) for i in result.items]
    assert keys == sorted(keys)
    grouped = result.by_principle()
    assert list(grouped) == list(result.principle_ids)
    assert [len(items) for items in grouped.values()] == [2, 1, 2]
    assert result.paths == (
        "src/sample/core/billing.py", "src/sample/web/handlers.py",
        "src/sample/web/handlers_v2.py",
    )


def test_two_runs_over_the_same_tree_give_the_same_work_list(tmp_path):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    assert work_list(repo).items == work_list(repo).items


@needs_ast_grep
def test_the_work_list_through_the_real_ast_grep(tmp_path):
    """The structured-logging plants found by an ast-grep rule, run by the real
    binary through the wrapper the sweep calls."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT, NO_UNCHAINED_RAISE, NO_VARIANT_MODULE)

    result = work_list(repo)

    assert sorted((i.path, i.line, i.principle_id) for i in result.items) == PLANTED_VIOLATIONS
    assert result.report.tools["ast-grep"]


# --- nothing to do -----------------------------------------------------------

def test_no_rules_is_no_work(tmp_path):
    """A repo with no rules exits 0 with a `no_rules` note; the sweep has
    nothing from the lint to act on. The note is kept, so whoever records the
    `no_work` can say why."""
    repo = sample_repo(tmp_path)

    result = work_list(repo)

    assert result.outcome == NO_WORK
    assert result.items == ()
    assert [n.kind for n in result.report.notes] == ["no_rules"]


def test_rules_that_find_nothing_is_no_work(tmp_path):
    clean_rule = NO_VARIANT_MODULE.replace("_(v[0-9]+|new|old)", "_(v99)")
    repo = sample_repo(tmp_path, clean_rule)

    result = work_list(repo)

    assert result.outcome == NO_WORK
    assert result.report.rules_checked == ("no-variant-module",)


# --- an untrustworthy work list is never acted on ------------------------------

def test_exit_2_from_the_real_runner_stops_the_sweep(tmp_path):
    """A config that does not validate is a 2: the gate's verdict is unknown,
    so the sweep must not guess at it."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES, config="rules:\n  fail_on: fatal\n")

    with pytest.raises(LintUntrusted) as raised:
        work_list(repo)

    assert raised.value.exit_code == 2
    assert "fail_on" in str(raised.value)


def test_exit_2_with_a_partial_report_stops_the_sweep_and_names_the_skipped_rules(tmp_path):
    """A partial report's findings are real, but what the skipped rule did not
    search has not been shown clean; the sweep stops rather than fixing half."""
    skipped = [{"rule_id": "no-print", "reason": "ast-grep is not installed"}]
    lint_ = fake_lint(tmp_path, stdout=report(complete=False, rules_skipped=skipped),
                      stderr="curamoir-lint: no-print skipped\n", exit_code=2)

    with pytest.raises(LintUntrusted, match="no-print"):
        work_list(tmp_path, lint=lint_)


def test_an_incomplete_report_stops_the_sweep_whatever_the_exit_code(tmp_path):
    skipped = [{"rule_id": "no-print", "reason": "ast-grep is not installed"}]
    lint_ = fake_lint(tmp_path, stdout=report(complete=False, rules_skipped=skipped),
                      exit_code=1)

    with pytest.raises(LintUntrusted, match="incomplete"):
        work_list(tmp_path, lint=lint_)


@pytest.mark.parametrize("code", [3, 127, -9])
def test_an_exit_code_outside_the_contract_stops_the_sweep(tmp_path, code):
    lint_ = fake_lint(tmp_path, stdout=report(), exit_code=code % 256)

    with pytest.raises(LintUntrusted):
        work_list(tmp_path, lint=lint_)


def test_a_report_that_the_reader_refuses_stops_the_sweep(tmp_path):
    lint_ = fake_lint(tmp_path, stdout=report(schema_version=2), exit_code=1)

    with pytest.raises(LintUntrusted, match="schema_version"):
        work_list(tmp_path, lint=lint_)


@pytest.mark.parametrize("exit_code, violations", [(0, [RECORD]), (1, [])])
def test_an_exit_code_that_contradicts_the_report_stops_the_sweep(tmp_path, exit_code,
                                                                   violations):
    """Exit 1 means a violation at or above `fail_on`, exit 0 means none. A
    report that disagrees with its own exit code was not produced by the
    runner the sweep thinks it ran."""
    lint_ = fake_lint(tmp_path, stdout=report(violations=violations), exit_code=exit_code)

    with pytest.raises(LintUntrusted, match="exit"):
        work_list(tmp_path, lint=lint_)


def test_a_runner_that_cannot_be_started_stops_the_sweep(tmp_path):
    with pytest.raises(LintUntrusted):
        work_list(tmp_path, lint=tmp_path / "no-such-lint")


def test_a_runner_that_hangs_stops_the_sweep(tmp_path):
    """`--timeout` bounds the runner itself; this is the backstop for a
    runner that does not honour it."""
    lint_ = fake_lint(tmp_path, stdout=report(), sleep=30)

    with pytest.raises(LintUntrusted, match="did not finish"):
        work_list(tmp_path, lint=lint_, timeout=1, grace=1)


def test_an_empty_repo_is_refused_before_anything_runs(tmp_path):
    """`Path("").resolve()` is the working directory, so resolving first would
    bypass curamoir-lint's own refusal of `--repo ""` and lint wherever the
    sweep happened to start. The stand-in leaves a mark if it is run at all."""
    mark = tmp_path / "ran"
    lint_ = fake_lint(tmp_path, stdout=report(), exit_code=0)
    lint_.write_text(lint_.read_text(encoding="utf-8").replace(
        "import sys, time\n", f"import sys, time\nopen({str(mark)!r}, 'w').close()\n"),
        encoding="utf-8")

    with pytest.raises(LintUntrusted, match="empty"):
        work_list("", lint=lint_)
    assert not mark.exists()


def test_a_symlink_loop_repo_stops_the_sweep(tmp_path):
    """On 3.9 `Path.resolve()` raises RuntimeError for a loop; on later
    versions it may return the path and the lint refuses it. Either way the
    sweep stops with LintUntrusted, not a traceback."""
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")

    with pytest.raises(LintUntrusted):
        work_list(tmp_path / "a")


@pytest.mark.parametrize("error", [RuntimeError("Symlink loop from 'a'"),
                                   OSError("resolve failed")])
def test_a_repo_that_cannot_be_resolved_stops_the_sweep(tmp_path, monkeypatch, error):
    """3.9's symlink-loop RuntimeError, simulated so it is tested on any Python."""
    lint_ = fake_lint(tmp_path, stdout=report(), exit_code=0)

    def refuse(self, strict=False):
        raise error

    monkeypatch.setattr(sweep.Path, "resolve", refuse)

    with pytest.raises(LintUntrusted, match="resolve"):
        work_list(tmp_path / "repo", lint=lint_)


# --- the deterministic helper the skill calls ---------------------------------

def run_cli(*args):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "curamoir.cli", *args],
        capture_output=True, text=True, env=environment, timeout=120,
    )


def test_the_cli_prints_the_work_list_grouped_by_principle(tmp_path):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = run_cli("sweep-work-list", "--repo", str(repo))

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["outcome"] == WORK
    assert printed["count"] == 5
    assert [p["principle_id"] for p in printed["principles"]] == [
        "errors-carry-context", "no-file-variant-proliferation", "structured-logging",
    ]
    found = sorted(
        (v["path"], v["line"], p["principle_id"])
        for p in printed["principles"] for v in p["violations"]
    )
    assert found == PLANTED_VIOLATIONS
    assert all(p["principle_rule"] and p["why_link"] for p in printed["principles"])


def test_the_cli_says_no_work(tmp_path):
    result = run_cli("sweep-work-list", "--repo", str(sample_repo(tmp_path)))

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["outcome"] == NO_WORK
    assert printed["principles"] == []
    assert printed["notes"] == ["no_rules"]


def test_the_cli_exits_2_with_nothing_on_stdout_when_the_lint_cannot_be_trusted(tmp_path):
    """Nothing on stdout, so an agent cannot mistake a partial list for the
    work list."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES, config="rules:\n  fail_on: fatal\n")

    result = run_cli("sweep-work-list", "--repo", str(repo))

    assert result.returncode == lint.EXIT_ERROR
    assert result.stdout == ""
    assert "do not edit anything" in result.stderr


def test_the_cli_refuses_an_empty_repo_with_exit_2_and_nothing_on_stdout():
    result = run_cli("sweep-work-list", "--repo", "")

    assert result.returncode == lint.EXIT_ERROR
    assert result.stdout == ""
    assert "empty" in result.stderr


def test_the_cli_writes_utf8_whatever_the_locale(tmp_path, monkeypatch):
    """`ensure_ascii=False` keeps a message readable, but printed through an
    ASCII stdout it would raise UnicodeEncodeError and exit 1 mid-contract.
    The bytes are UTF-8 whatever the stream's encoding."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    lint_ = fake_lint(tmp_path, stdout=report(violations=[
        dict(RECORD, message="naïve print — use the logger", path="src/café.py")]),
        exit_code=1)
    monkeypatch.setattr(cli, "work_list", lambda repo: work_list(repo, lint=lint_))
    monkeypatch.setattr(sys, "stdout", stream)

    code = cli.main(["sweep-work-list", "--repo", str(tmp_path)])

    stream.flush()
    printed = json.loads(stream.buffer.getvalue().decode("utf-8"))
    assert code == 0
    violation = printed["principles"][0]["violations"][0]
    assert violation["message"] == "naïve print — use the logger"
    assert printed["paths"] == ["src/café.py"]


def test_the_cli_writes_utf8_under_an_ascii_locale_end_to_end(tmp_path):
    """The same through a real process with PYTHONIOENCODING=ascii, with a
    rule whose message is not ASCII (the fixture's own messages are)."""
    non_ascii = NO_BARE_PRINT.replace("with no level or logger name",
                                      "with no level — naïve logging")
    assert non_ascii != NO_BARE_PRINT
    repo = sample_repo(tmp_path, non_ascii, NO_UNCHAINED_RAISE, NO_VARIANT_MODULE)
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
                   "PYTHONIOENCODING": "ascii"}

    result = subprocess.run(
        [sys.executable, "-m", "curamoir.cli", "sweep-work-list", "--repo", str(repo)],
        capture_output=True, env=environment, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout.decode("utf-8"))
    messages = [v["message"] for p in printed["principles"] for v in p["violations"]]
    assert any("— naïve logging" in message for message in messages)


# --- the skill ---------------------------------------------------------------

SKILL = ROOT / "skills" / "sweep" / "SKILL.md"


def test_the_skill_calls_the_helper_and_states_the_scope_limits():
    """The skill is prompt text, so what it must say is pinned: the command it
    runs exists (tested above), the E3-R2 limits are stated (the spec says the
    prompt MUST state them), and it cannot fire on its own."""
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]

    assert "name: sweep" in frontmatter
    assert "disable-model-invocation: true" in frontmatter
    assert "scripts/curamoir sweep-work-list" in text
    assert ("only files with violations; no refactors beyond the violation; no new "
            "dependencies; no signature changes without an explicit principle "
            "permitting it.") in " ".join(text.split())
    assert NO_WORK in text


# --- edge cases (cm-fkd.1 review) ---------------------------------------------

def test_a_timeout_of_0_means_no_backstop_either(tmp_path):
    """`--timeout 0` is the lint's "no bound"; a backstop of `0 + grace` would
    kill a run the caller asked not to bound once `grace` had passed."""
    lint_ = fake_lint(tmp_path, stdout=report(), exit_code=1, sleep=2)

    result = work_list(tmp_path, lint=lint_, timeout=0, grace=0.5)

    assert result.outcome == WORK


def test_a_runner_that_is_not_executable_stops_the_sweep(tmp_path):
    lint_ = tmp_path / "curamoir-lint"
    lint_.write_text("#!/bin/sh\n", encoding="utf-8")
    lint_.chmod(0o644)

    with pytest.raises(LintUntrusted, match="could not be started"):
        work_list(tmp_path, lint=lint_)


def test_a_missing_runner_stops_the_sweep_with_a_reason(tmp_path):
    with pytest.raises(LintUntrusted, match="could not be started"):
        work_list(tmp_path, lint=tmp_path / "no-such-lint")


def cli_with(monkeypatch, lint_, repo):
    """The CLI in-process against a stand-in lint, its stdout a UTF-8 byte
    stream as a real one is."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(cli, "work_list", lambda repo: work_list(repo, lint=lint_))
    monkeypatch.setattr(sys, "stdout", stream)
    code = cli.main(["sweep-work-list", "--repo", str(repo)])
    stream.flush()
    return code, stream.buffer.getvalue()


def test_the_cli_exits_2_when_the_runner_cannot_be_started(tmp_path, monkeypatch, capsys):
    code, stdout = cli_with(monkeypatch, tmp_path / "no-such-lint", tmp_path)

    assert code == lint.EXIT_ERROR
    assert stdout == b""
    assert "could not be started" in capsys.readouterr().err


def test_the_cli_exits_2_on_a_path_that_cannot_be_written_as_utf8(tmp_path, monkeypatch,
                                                                    capsys):
    """A lone surrogate is valid JSON but cannot be encoded; the reader
    refuses it, so the CLI stops before writing rather than crashing mid-write."""
    lint_ = fake_lint(tmp_path, stdout=report(violations=[
        dict(RECORD, path="src/\udcff.py")]), exit_code=1)

    code, stdout = cli_with(monkeypatch, lint_, tmp_path)

    assert code == lint.EXIT_ERROR
    assert stdout == b""
    assert "do not edit anything" in capsys.readouterr().err


def test_the_cli_lists_each_note_kind_once_in_first_seen_order(tmp_path, monkeypatch):
    notes = [{"kind": "symlink_not_followed", "path": f"link{i}"} for i in range(5000)]
    notes.insert(1, {"kind": "not_regular_file", "path": "fifo"})
    lint_ = fake_lint(tmp_path, stdout=report(notes=notes), exit_code=1)

    code, stdout = cli_with(monkeypatch, lint_, tmp_path)

    assert code == 0
    assert json.loads(stdout)["notes"] == ["symlink_not_followed", "not_regular_file"]


def test_a_reader_that_closes_early_is_exit_2_with_no_traceback(tmp_path):
    """A real broken pipe: the work list is far larger than a pipe buffer and
    the reader stops after one byte."""
    violations = [dict(RECORD, fingerprint=f"{i:016x}", line=i + 1, end_line=i + 1)
                  for i in range(3000)]
    lint_ = fake_lint(tmp_path, stdout=report(violations=violations), exit_code=1)
    program = ("import sys\n"
               "from curamoir import cli, sweep\n"
               "cli.work_list = lambda repo: sweep.work_list(repo, lint=sys.argv[1])\n"
               "sys.exit(cli.main(['sweep-work-list', '--repo', sys.argv[2]]))\n")
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    process = subprocess.Popen(
        [sys.executable, "-c", program, str(lint_), str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    )
    assert process.stdout.read(1) == b"{"
    process.stdout.close()
    with process.stderr:
        stderr = process.stderr.read().decode("utf-8", "backslashreplace")
    code = process.wait(timeout=120)

    assert code == lint.EXIT_ERROR, stderr
    assert "Traceback" not in stderr and "Exception ignored" not in stderr, stderr
    assert "could not be written" in stderr
