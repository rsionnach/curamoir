"""R5-R59: the two known escapes, through round 0 with no model (cm-8ap.15).

The replay corpus (fixtures/r5-replay/) flags two entries ``escape: true``,
changes that passed every lens of the sequential gate and should not have.
Spec v1.15 holds each to what it can show:

- ``opensrm-ih0v`` (``tests/test_manifest_v2_archetypes.py``) must fail
  round 0 on the R5-R33 checks. Its module skips itself when a sibling
  checkout is absent, finds that checkout by ``Path(__file__).parents[2]``,
  and parametrises over a directory listing with no minimum: on any machine
  without the sibling the module is skipped or empty, and the run is green.
- ``opensrm-oh27`` cannot be caught mechanically, and this test says so. Its
  code and its fixtures share one shape real manifests never have
  (``spec.slos`` as a list), so its suite is green, none of the R5-R33
  shapes is in its tests, and a mutation of the predicate is killed by those
  same fixtures. Only a fixture traced to a real artefact shows it, which is
  the provenance reviewer's duty (R5-R35), not round 0's.

**How the entries are run.** Each entry holds the change as ``diff.patch``,
not the repository, so a repository is built here: a base commit, then a
head commit with the entry's new test files taken whole from its diff. The
suite step's run function reports every test those files define as run and
passed, which is what the recorded gates saw (both suites were green; the
escape is that green was wrong). So what fails ih0v below is the guard
rules alone, with a passing suite, and nothing in round 0 depends on a model
or on the entries' own dependencies, which are not installed here.

oh27 also modifies ``tests/learn/test_retrospective_financial.py``, and its
diff holds only that file's three hunks, not the file, so it cannot be
rebuilt; the hunks change a call's argument (``loaded`` to
``loaded.manifests``), which no R5-R33 check concerns.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from curamoir.gate.rounds import Step, StepRun, _tests_in, run_round0
from tests.repo_root import REPO_ROOT
from tests.test_lint import needs_ast_grep

ROOT = REPO_ROOT
CORPUS = ROOT / "fixtures" / "r5-replay"

#: The entry and the new test files its diff adds whole.
ESCAPES = {
    "opensrm-ih0v-nthlayer-common": ["tests/test_manifest_v2_archetypes.py"],
    "opensrm-oh27": ["tests/learn/test_retrospective_manifest_failures.py"],
}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def escape_repo(tmp_path, entry):
    """(repo, base): a base commit, then head with the entry's new test
    files applied from its own diff."""
    repo = tmp_path / entry
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("replay\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    patch = CORPUS / entry / "diff.patch"
    for path in ESCAPES[entry]:
        _git(repo, "apply", f"--include={path}", str(patch))
        assert (repo / path).is_file(), path
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo, base


def green_suite(step, checkout):
    """The recorded gates' green run: every test the head's test files
    define, collected and passed, as pytest's xunit1 JUnit XML."""
    cases = []
    for path in sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob("test_*.py")):
        module = path[:-len(".py")].replace("/", ".")
        for chain, name, *_ in _tests_in((checkout / path).read_text(encoding="utf-8")):
            cases.append(f'<testcase classname="{".".join((module, *chain))}" name="{name}" '
                         f'file="{path}" line="1" time="0.001"/>')
    assert cases
    xml = f'<testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'
    return StepRun(0, b"passed\n", 1.0, xml.encode())


def round0(repo, base):
    return run_round0(repo, base=base, head=_git(repo, "rev-parse", "HEAD"),
                      steps=(Step("suite", ("pytest", "-q"), 600, suite=True),),
                      run=green_suite, test_paths=("tests",),
                      ast_grep=shutil.which("ast-grep"))


def test_both_entries_are_the_corpus_escapes():
    for entry in ESCAPES:
        meta = json.loads((CORPUS / entry / "meta.json").read_text(encoding="utf-8"))
        assert meta["escape"] is True, entry
    flagged = sorted(p.parent.name for p in CORPUS.glob("*/meta.json")
                     if json.loads(p.read_text(encoding="utf-8")).get("escape") is True)
    assert flagged == sorted(ESCAPES)


@needs_ast_grep
def test_ih0v_fails_round_0_with_no_model_while_its_suite_is_green(tmp_path):
    repo, base = escape_repo(tmp_path, "opensrm-ih0v-nthlayer-common")

    result = round0(repo, base)

    assert not result.passed
    assert result.failed_steps == ()
    rules = sorted({f["claim"].split(":", 1)[0] for f in result.findings})
    assert rules == ["no-layout-relative-path", "no-module-level-skip",
                     "parametrize-needs-minimum"]
    assert {f["path"] for f in result.findings} == {"tests/test_manifest_v2_archetypes.py"}
    assert all(f["lens"] == "provenance" and f["severity"] == "IMPORTANT"
               for f in result.findings)
    assert result.round["lenses"]["provenance"]["model"] is None


@needs_ast_grep
def test_oh27_passes_round_0_a_stated_limit_not_a_catch(tmp_path):
    """R5-R59: no mechanical check sees a shape the code and its fixtures
    share. If this starts failing, round 0 has learned something new about
    oh27: read why before changing the expectation, and update the spec."""
    repo, base = escape_repo(tmp_path, "opensrm-oh27")

    result = round0(repo, base)

    assert result.passed, result.findings
    assert result.findings == ()
    assert result.round["lenses"]["provenance"]["status"] == "ok"


@pytest.mark.parametrize("entry", sorted(ESCAPES))
def test_each_escape_has_its_new_test_files_whole_in_its_diff(entry):
    text = (CORPUS / entry / "diff.patch").read_text(encoding="utf-8")
    for path in ESCAPES[entry]:
        header = f"diff --git a/{path} b/{path}\nnew file mode"
        assert header in text, path
