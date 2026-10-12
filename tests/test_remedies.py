"""cm-ul5: file-level remedies, applied by the sweep's runner, not its agent.

E3's acceptance plants five violations, one of which is
`src/sample/web/handlers_v2.py` existing at all (no-file-variant-
proliferation). Its fix is deleting the file, and the fix agent's toolset
(Read, Grep, Glob, Edit) cannot delete. So a rule says `remedy:
delete_file` (rules.REMEDIES), and remedies.apply_file_remedies deletes the
file in the sandbox, deterministically, before the agent starts — unless
something at the base still refers to it. fix_scope.verify_fix accepts that
deletion, and only a deletion a rule with that remedy licenses.

**Fixture provenance.** The end-to-end tests use a sandbox of a copy of
`fixtures/sample-repo` with tests/test_lint.py's regex rules installed
(tests/conftest.py, committed_sample), whose NO_VARIANT_MODULE carries
`remedy: delete_file`; the work list is the real `scripts/curamoir-lint`'s
in that sandbox (fix_scope.lint_sandbox). Nothing in the fixture refers to
handlers_v2 but itself, PLANTED.json (which the fixture's .gitattributes
declares `linguist-documentation`), the principles' `bad` example and a
beads issue (checked below). The hardening tests use small repositories of
their own with the work list and rules set by hand, as tests/test_fix_scope.py
does.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from curamoir import remedies
from curamoir.fix_scope import (
    FILE_DELETED,
    ScopeCheckError,
    lint_sandbox,
    verify_fix,
)
from curamoir.git_guard import GitGuardError, prepare_sandbox, snapshot_git_state
from curamoir.remedies import apply_file_remedies
from curamoir.rules import Rule
from tests.conftest import committed_sample
from tests.test_fix_scope import git_repo as files_repo
from tests.test_fix_scope import sandbox_of
from tests.test_lint import NO_BARE_PRINT, NO_UNCHAINED_RAISE, NO_VARIANT_MODULE

BILLING = "src/sample/core/billing.py"
HANDLERS = "src/sample/web/handlers.py"
VARIANT = "src/sample/web/handlers_v2.py"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout


def commit(repo, files, message="more"):
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def edit(root, path, old, new):
    target = root / path
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} is not exactly once in {path}"
    target.write_text(text.replace(old, new), encoding="utf-8")


def git_repo(tmp_path, setup):
    """A one-commit repository; `setup(repo)` makes its entries. WHY beside
    files_repo (tests/test_fix_scope.py's, a dict of text files): these
    tests need a symlink or a directory where a file is expected, which
    text alone cannot make."""
    repo = tmp_path / "plain"
    repo.mkdir()
    setup(repo)
    for command in (["init", "--initial-branch=main"],
                    ["config", "user.email", "test@example.invalid"],
                    ["config", "user.name", "test"], ["add", "-A"], ["commit", "-m", "base"]):
        subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
    return repo


@dataclass(frozen=True)
class V:
    """A file-level violation, as the lint reports a `target: path` match."""
    path: str
    rule_id: str = "no-variant"
    line: int = 1
    col: int = 1
    end_line: int = 1
    end_col: int = 1
    principle_id: str = "p"


def path_rule(rule_id="no-variant", remedy="delete_file"):
    return Rule(id=rule_id, principle="p", severity="error", matcher="regex",
                pattern="_v[0-9]+", message="m", target="path", remedy=remedy)


def hand_sandbox(repo, items, rules):
    """tests/test_fix_scope.py's sandbox_of, with `rules` as well."""
    sandbox = sandbox_of(repo, *items)
    sandbox.rules = tuple(rules)
    return sandbox


def remedy_files(tmp_path, files, *paths):
    """(result, kept): apply_file_remedies's RemedyResult on a one-commit
    repository of `files` whose work list is a file-level violation of each
    of `paths` (default: the first file), and which of `files` were still in
    the sandbox afterwards (read before the sandbox is removed)."""
    repo = files_repo(tmp_path, files)
    with hand_sandbox(repo, [V(path) for path in paths or [next(iter(files))]],
                      [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)
        kept = {path for path in files if (sandbox.path / path).exists()}
    return result, kept


# --- the acceptance path -----------------------------------------------------------

def test_nothing_in_the_fixture_refers_to_the_variant_but_prose():
    """The premise of the acceptance: deleting handlers_v2.py breaks nothing,
    because only prose and records name it, and the one record of a kind the
    search reads (PLANTED.json) is declared documentation."""
    fixture = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures", "sample-repo")
    found = subprocess.run(["git", "grep", "-l", "-w", "handlers_v2", "--", "."], cwd=fixture,
                           capture_output=True, text=True).stdout.split()
    assert sorted(found) == [".beads/issues.jsonl", ".curamoir/principles.md", "PLANTED.json"]
    attribute = subprocess.run(["git", "check-attr", "linguist-documentation", "--",
                                "PLANTED.json"], cwd=fixture, capture_output=True,
                               text=True).stdout
    assert attribute.strip().endswith(": set")


def test_the_variant_is_deleted_in_the_sandbox_and_the_review_accepts_it(sandboxed):
    result = apply_file_remedies(sandboxed)

    assert result.deleted == (VARIANT,)
    assert result.skipped == ()
    assert not (sandboxed.path / VARIANT).exists()
    verdict = verify_fix(sandboxed, git_state=snapshot_git_state(sandboxed))
    assert verdict.ok, verdict.render()
    assert verdict.paths == (VARIANT,)


def test_the_agent_is_given_only_what_the_runner_did_not_handle(sandboxed):
    result = apply_file_remedies(sandboxed)

    left = result.for_agent(sandboxed.work_list)

    assert VARIANT not in left.paths
    assert len(left.items) == len(sandboxed.work_list.items) - 1 == 4
    assert left.report is sandboxed.work_list.report


def test_one_sweep_fixes_all_five_planted_violations(sandboxed, tmp_path):
    """The E3 acceptance, with the agent's four line edits written by the
    test (the review must not care who made them): lint, remedies, edits,
    review, and the patch of exactly the verdict's paths applies to the base
    and deletes the variant there too."""
    deleted = apply_file_remedies(sandboxed).deleted
    root = sandboxed.path
    state = snapshot_git_state(sandboxed)  # what run_fix_step takes, after the remedies
    edit(root, BILLING, 'print("calculating line total")', 'logger.info("calculating line total")')
    edit(root, BILLING, "    except OSError:\n        raise RuntimeError(\"could not load rates\")",
         "    except OSError as error:\n"
         "        raise RuntimeError(f\"could not load rates from {path}\") from error")
    edit(root, HANDLERS, 'print(f"request failed: {exception}")',
         'logger.error("request failed: %s", exception)')
    edit(root, HANDLERS, '    except IndexError:\n        raise ValueError("bad token")',
         '    except IndexError as error:\n        raise ValueError(f"bad token: {raw!r}") from error')

    verdict = verify_fix(sandboxed, git_state=state)

    assert deleted == (VARIANT,)
    assert verdict.ok, verdict.render()
    assert verdict.paths == (BILLING, HANDLERS, VARIANT)
    patch = tmp_path / "fix.patch"
    patch.write_bytes(sandboxed.patch(verdict.paths))
    source = Path(sandboxed.source)
    git(source, "apply", str(patch))
    assert not (source / VARIANT).exists()
    assert "logger.info" in (source / BILLING).read_text(encoding="utf-8")


def test_remedies_run_once_on_the_pristine_base_only(sandboxed):
    """Before the agent, on the tree the work list was linted from: a second
    call, or a call after any edit, is refused rather than deciding on a tree
    that is not the base."""
    apply_file_remedies(sandboxed)

    with pytest.raises(ScopeCheckError):
        apply_file_remedies(sandboxed)


def test_remedies_need_the_work_list_and_rules_linted_in_the_sandbox(tmp_path):
    repo = committed_sample(tmp_path)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox, \
            pytest.raises(ScopeCheckError, match="lint_sandbox"):
        apply_file_remedies(sandbox)


# --- no remedy, no deletion --------------------------------------------------------

def without_remedy(tmp_path):
    return committed_sample(tmp_path, NO_BARE_PRINT, NO_UNCHAINED_RAISE,
                            NO_VARIANT_MODULE.replace("remedy: delete_file\n", ""))


def test_a_path_rule_without_a_remedy_deletes_nothing(tmp_path):
    with prepare_sandbox(without_remedy(tmp_path), "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)

        result = apply_file_remedies(sandbox)

        assert result.deleted == () and result.skipped == ()
        assert (sandbox.path / VARIANT).is_file()
        assert VARIANT in result.for_agent(sandbox.work_list).paths


def test_deleting_a_file_whose_path_rule_names_no_remedy_is_refused(tmp_path):
    """A file-level violation alone licenses no deletion: without `remedy:
    delete_file` on its rule, the deletion is FILE_DELETED, whoever made it."""
    with prepare_sandbox(without_remedy(tmp_path), "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        state = snapshot_git_state(sandbox)
        (sandbox.path / VARIANT).unlink()

        verdict = verify_fix(sandbox, git_state=state)

    assert [(b.kind, b.path) for b in verdict.breaches] == [(FILE_DELETED, VARIANT)]


def test_a_remedy_written_into_the_sandboxs_rules_licenses_nothing(tmp_path):
    """The rules are the ones lint_sandbox loaded at the base: an agent that
    adds `remedy: delete_file` to a rule file in the tree and then deletes
    the file is refused for both."""
    with prepare_sandbox(without_remedy(tmp_path), "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        state = snapshot_git_state(sandbox)
        rule = sandbox.path / ".curamoir" / "rules" / "no-variant-module.yaml"
        rule.write_text(rule.read_text(encoding="utf-8") + "remedy: delete_file\n",
                        encoding="utf-8")
        (sandbox.path / VARIANT).unlink()

        verdict = verify_fix(sandbox, git_state=state)

    assert (FILE_DELETED, VARIANT) in {(b.kind, b.path) for b in verdict.breaches}


# --- a file the base still refers to is kept --------------------------------------

@pytest.mark.parametrize("path, text, why", [
    ("src/sample/web/routes.py", "from sample.web.handlers_v2 import paginate\n", "import"),
    ("src/sample/web/routes.py", "from . import handlers, handlers_v2\n", "sibling import"),
    ("src/sample/web/routes.py", "from . import (\n    handlers_v2,\n)\n", "multi-line import"),
    ("src/sample/web/routes.py",
     "import importlib\nmod = importlib.import_module('sample.web.handlers_v2')\n", "dynamic"),
    ("tests/test_pagination.py", "from sample.web import handlers_v2\n", "a test"),
    ("src/sample/web/page.ts", "import { paginate } from './handlers_v2';\n", "JS/TS import"),
    ("deploy/app.toml", "[app]\nentry = \"sample.web.handlers_v2:paginate\"\n",
     "an entry point in config"),
])
def test_a_file_the_base_refers_to_is_skipped_with_the_reason(tmp_path, path, text, why):
    repo = committed_sample(tmp_path)
    commit(repo, {path: text}, f"refer to the variant: {why}")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)

        result = apply_file_remedies(sandbox)

        assert result.deleted == ()
        assert [(s.path, s.rule_id) for s in result.skipped] == [(VARIANT, "no-variant-module")]
        assert path in result.skipped[0].reason
        assert (sandbox.path / VARIANT).is_file()
        # Skipped is the runner's to report as unfixed, not the agent's to retry.
        assert VARIANT not in result.for_agent(sandbox.work_list).paths


def test_a_mention_in_prose_does_not_keep_the_file(sandboxed):
    """principles.md (prose) and PLANTED.json (declared documentation) name
    `handlers_v2.py` by path; that is a description of the file, not a use."""
    assert apply_file_remedies(sandboxed).deleted == (VARIANT,)


# --- the deletion is of a regular file in the sandbox, and nothing else ------------

def test_a_symlink_at_the_path_is_skipped_not_followed(tmp_path):
    def setup(repo):
        (repo / "real.py").write_text("x = 1\n", encoding="utf-8")
        os.symlink("real.py", repo / "a_v2.py")
    repo = git_repo(tmp_path, setup)
    with hand_sandbox(repo, [V("a_v2.py")], [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)

        assert result.deleted == ()
        assert "symlink" in result.skipped[0].reason
        assert os.path.islink(sandbox.path / "a_v2.py")
        assert (sandbox.path / "real.py").is_file()


def test_a_directory_at_the_path_is_skipped(tmp_path):
    def setup(repo):
        (repo / "d_v2.py").mkdir()
        (repo / "d_v2.py" / "inner.txt").write_text("x\n", encoding="utf-8")
    repo = git_repo(tmp_path, setup)
    with hand_sandbox(repo, [V("d_v2.py")], [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)

        assert result.deleted == ()
        assert "not a regular file" in result.skipped[0].reason
        assert (sandbox.path / "d_v2.py" / "inner.txt").is_file()


def test_a_path_through_a_symlinked_directory_is_skipped(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a_v2.py").write_text("x = 1\n", encoding="utf-8")

    def setup(repo):
        (repo / "m.py").write_text("x = 1\n", encoding="utf-8")
        os.symlink(str(outside), repo / "linked")
    repo = git_repo(tmp_path, setup)
    with hand_sandbox(repo, [V("linked/a_v2.py")], [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)

    assert result.deleted == ()
    assert "linked" in result.skipped[0].reason
    assert (outside / "a_v2.py").is_file()


@pytest.mark.parametrize("path", ["../a_v2.py", "/tmp/a_v2.py", "sub/../../a_v2.py",
                                  ".git/a_v2.py", "./a_v2.py", ""])
def test_a_path_that_is_not_plainly_inside_the_sandbox_is_skipped(tmp_path, path):
    def setup(repo):
        (repo / "a_v2.py").write_text("x = 1\n", encoding="utf-8")
    repo = git_repo(tmp_path, setup)
    (tmp_path / "a_v2.py").write_text("outside\n", encoding="utf-8")
    with hand_sandbox(repo, [V(path)], [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)

        assert result.deleted == ()
        assert len(result.skipped) == 1
        assert (sandbox.path / "a_v2.py").is_file()
    assert (tmp_path / "a_v2.py").is_file()


def test_a_dependency_manifest_is_never_deleted(tmp_path):
    """The review refuses any change to one (DEPENDENCY_CHANGE), so deleting
    it would only sink the whole fix."""
    def setup(repo):
        (repo / "requirements_old.txt").write_text("x\n", encoding="utf-8")
    repo = git_repo(tmp_path, setup)
    with hand_sandbox(repo, [V("requirements_old.txt")], [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)

        assert result.deleted == ()
        assert "dependency" in result.skipped[0].reason


def test_a_violation_that_is_not_file_level_is_not_a_remedy(tmp_path):
    def setup(repo):
        (repo / "a_v2.py").write_text("x = 1\n", encoding="utf-8")
    repo = git_repo(tmp_path, setup)
    with hand_sandbox(repo, [V("a_v2.py", line=1, end_col=6)], [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)

        assert result.deleted == ()
        assert "file-level" in result.skipped[0].reason
        assert (sandbox.path / "a_v2.py").is_file()


def test_two_violations_of_one_file_delete_it_once(tmp_path):
    def setup(repo):
        (repo / "a_v2.py").write_text("x = 1\n", encoding="utf-8")
    repo = git_repo(tmp_path, setup)
    rules = [path_rule("one"), path_rule("two")]
    with hand_sandbox(repo, [V("a_v2.py", rule_id="one"), V("a_v2.py", rule_id="two")],
                      rules) as sandbox:
        result = apply_file_remedies(sandbox)

        assert result.deleted == ("a_v2.py",)
        assert result.skipped == ()


# --- what is never deleted --------------------------------------------------------
# The governing rule: never delete a file the base still uses. Each of these
# is a file a word search cannot prove unused, so it is refused outright.

def test_a_test_file_that_is_a_variant_is_kept_and_keeps_what_it_imports(tmp_path):
    """tests/test_handlers_v2.py matches the variant rule too; deleting it
    would remove the only thing exercising handlers_v2, and it is a use."""
    test_file = "tests/test_handlers_v2.py"
    repo = committed_sample(tmp_path)
    commit(repo, {test_file: "from sample.web.handlers_v2 import paginate\n\n"
                             "def test_p():\n    assert paginate([1], 1) == [1]\n"})
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)

        result = apply_file_remedies(sandbox)

        assert result.deleted == ()
        reasons = {s.path: s.reason for s in result.skipped}
        assert "test" in reasons[test_file]
        assert test_file in reasons[VARIANT]
        assert (sandbox.path / test_file).is_file() and (sandbox.path / VARIANT).is_file()


@pytest.mark.parametrize("path", [
    "tests/a_v2.py", "test/a_v2.py", "src/__tests__/a_v2.js", "spec/a_v2.ts",
    "test_a_v2.py", "a_v2_test.py", "conftest_v2.py", "a_v2.test.ts", "a_v2.spec.js"])
def test_a_test_file_is_never_deleted(tmp_path, path):
    result, kept = remedy_files(tmp_path, {path: "x = 1\n"})

    assert result.deleted == () and kept == {path}
    assert "test" in result.skipped[0].reason


@pytest.mark.parametrize("path", ["templates/nav_v2.html", "static/site_v2.css",
                                  "gen_v2.sh", "notes_v2.md", "a_v2.PY"])
def test_a_file_that_is_not_python_or_js_ts_source_is_never_deleted(tmp_path, path):
    """A template, stylesheet or script is used in ways a word search cannot
    prove absent (`{% include 'nav_v2.html' %}`, a `<link>`, a cron line)."""
    result, kept = remedy_files(tmp_path, {path: "x\n"})

    assert result.deleted == () and kept == {path}
    assert "source" in result.skipped[0].reason


@pytest.mark.parametrize("name", ["__init__.py", "__main__.py"])
def test_a_package_init_or_main_is_never_deleted(tmp_path, name):
    """What is referenced is the package's name (`python -m tool_v2`), and
    deleting either file changes what the package is."""
    path = f"tool_v2/{name}"
    result, kept = remedy_files(tmp_path, {path: "x = 1\n"})

    assert result.deleted == () and kept == {path}
    assert name in result.skipped[0].reason


@pytest.mark.parametrize("path, referrer, text", [
    ("scripts/migrate_v2.py", ".github/workflows/ci.yml",
     "jobs:\n  m:\n    steps:\n      - run: python scripts/migrate_v2.py\n"),
    ("tools/build_new.py", "Makefile", "build:\n\tpython tools/build_new.py\n"),
    ("tools/build_new.py", "tools/Makefile", "build:\n\tpython build_new.py\n"),
    ("serve_v2.py", "Dockerfile", 'CMD ["python", "serve_v2.py"]\n'),
    ("run_old.py", "pyproject.toml", "[project.scripts]\nrun = 'run_old:main'\n"),
    ("cli_v2.py", "pyproject.toml", "[project.scripts]\ncli = \"cli_v2:main\"\n"),
    ("app_v2.py", "Procfile", "web: gunicorn app_v2:app\n"),
    ("app_v2.py", "deploy/run.sh", "#!/bin/sh\nexec python app_v2.py\n"),
    ("web/main_v2.ts", "package.json", '{"main": "web/main_v2.ts"}\n'),
    ("a_v2.py", "b.py", "\0import a_v2\n"),
    # No allow-list of file kinds: each of these was missed by one.
    ("src/util_v2.ts", "src/App.vue",
     "<script setup>\nimport { f } from './util_v2'\n</script>\n"),
    ("src/util_v2.ts", "src/A.svelte", "<script>\n  import { f } from './util_v2';\n</script>\n"),
    ("k_v2.py", "fast.pyx", "from k_v2 import f\n"),
    ("lib_v2.py", "nb.ipynb", '{"cells": [{"source": ["import lib_v2\\n"]}]}\n'),
    ("srv/d_v2.py", "deploy/app.service",
     "[Service]\nExecStart=/usr/bin/python3 /srv/d_v2.py\n"),
    ("srv_v2.py", ".vscode/launch.json",
     '{"configurations": [{"program": "${workspaceFolder}/srv_v2.py"}]}\n'),
    ("ci/deploy_v2.py", "Jenkinsfile",
     "pipeline { stages { stage('d') { steps { sh 'python ci/deploy_v2.py' } } } }\n"),
    ("tools/gen_v2.py", "build.ps1", "python tools/gen_v2.py\n"),
    ("tools/gen_v2.py", "ops/crontab.txt", "0 * * * * python /app/tools/gen_v2.py\n"),
])
def test_a_build_or_config_file_that_runs_it_keeps_it(tmp_path, path, referrer, text):
    result, kept = remedy_files(tmp_path, {path: "x = 1\n", referrer: text}, path)

    assert result.deleted == () and path in kept
    assert referrer in result.skipped[0].reason


def test_a_mention_in_prose_or_curamoirs_own_files_does_not_keep_it(tmp_path):
    files = {"a_v2.py": "x = 1\n", "docs/notes.md": "a_v2.py is gone\n",
             "CHANGES.rst": "a_v2\n", ".github/PULL_REQUEST_TEMPLATE.md": "a_v2\n",
             "README.MD": "a_v2\n", "docs/guide.adoc": "a_v2\n", "x.markdown": "a_v2\n",
             "CHANGELOG": "- removed a_v2\n", "pkg/LICENSE.txt": "a_v2\n",
             ".curamoir/config.yaml": "exclude: [a_v2.py]\n", ".beads/x.yaml": "a_v2\n"}
    result, kept = remedy_files(tmp_path, files, "a_v2.py")

    assert result.deleted == ("a_v2.py",) and "a_v2.py" not in kept


@pytest.mark.parametrize("attributes, deleted", [
    ("records/*.json linguist-documentation\n", ("a_v2.py",)),
    ("records/*.json linguist-documentation=true\n", ("a_v2.py",)),
    ("records/*.json linguist-documentation=false\n", ()),
    ("records/*.json -linguist-documentation\n", ()),
    ("other/*.json linguist-documentation\n", ()),
])
def test_a_record_the_project_declares_documentation_does_not_keep_it(tmp_path, attributes,
                                                                      deleted):
    """`linguist-documentation` (GitHub Linguist's attribute) is how a
    project says a file describes code rather than being part of it; the
    fixture's PLANTED.json is declared so. Anything else keeps the file."""
    files = {"a_v2.py": "x = 1\n", ".gitattributes": attributes,
             "records/manifest.json": '{"path": "a_v2.py"}\n'}
    result, _ = remedy_files(tmp_path, files, "a_v2.py")

    assert result.deleted == deleted
    if not deleted:
        assert "records/manifest.json" in result.skipped[0].reason


def test_a_documentation_record_does_not_hide_a_real_use(tmp_path):
    files = {"a_v2.py": "x = 1\n", ".gitattributes": "*.json linguist-documentation\n",
             "m.json": '"a_v2"\n', "main.py": "import a_v2\n"}
    result, _ = remedy_files(tmp_path, files, "a_v2.py")

    assert result.deleted == ()
    assert "main.py" in result.skipped[0].reason and "m.json" not in result.skipped[0].reason


_WORD = rb"[A-Za-z0-9_]"


def per_file_oracle(files, candidates):
    """The candidates nothing refers to, decided one stem at a time by plain
    Python over the files: the stem as a whole word (git grep -w's word
    characters) in any other file that is not prose or a record."""
    import re
    prose = re.compile(r"(?i)(.*\.(md|markdown|rst|adoc)|(.*/)?(changelog|license)[^/]*)$")
    unused = set()
    for path in candidates:
        stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
        word = re.compile(rb"(?<!" + _WORD + rb")" + re.escape(stem.encode()) + rb"(?!" + _WORD
                          + rb")")
        if not any(word.search(text.encode()) for other, text in files.items()
                   if other != path and not prose.match(other)
                   and not other.startswith((".curamoir/", ".beads/"))):
            unused.add(path)
    return unused


def test_the_one_batched_search_decides_as_a_search_per_file_would(tmp_path):
    """One `git grep` for every candidate's stem gives the decisions a
    search per stem would, on stems that overlap, share a name, contain
    non-word characters, or appear only in prose, a binary or the file
    itself."""
    files = {
        "a_v2.py": "x = 1\n", "m.py": "import a_v2\n",
        "b_v2.py": "x = 1\n", "n.py": "xb_v2 = b_v2x = 1\n",
        "web/c_v2.py": "x = 1\n", "conf.ini": "[app]\nmodule = web.c_v2:app\n",
        "d-x_v2.py": "x = 1\n", "x_v2.py": "x = 1\n", "run.sh": "python d-x_v2.py\n",
        "e-f_v2.py": "x = 1\n", "f-g_v2.py": "x = 1\n", "o.txt": "e-f-g_v2\n",
        "h_v2.ts": "x = 1\n", "blob.dat": "\0\x01h_v2\0",
        "i_v2.py": "x = 1\n", "README.md": "i_v2 is old\n",
        "p/k_v2.py": "k_v2 = 1\n", "q/k_v2.py": "x = 1\n",
        "lone_v2.py": "lone_v2 = 'only itself'\n",
    }
    candidates = [path for path in files if "_v2." in path]
    expected = per_file_oracle(files, candidates)
    assert expected == {"b_v2.py", "e-f_v2.py", "i_v2.py", "p/k_v2.py", "lone_v2.py"}

    result, _ = remedy_files(tmp_path, files, *candidates)

    assert set(result.deleted) == expected
    assert {s.path for s in result.skipped} == set(candidates) - expected


def test_a_path_the_base_does_not_track_under_that_exact_name_is_refused(tmp_path):
    """On a case-insensitive file system `A_V2.py` opens `a_v2.py`; the work
    list's path must be the base's own spelling."""
    result, kept = remedy_files(tmp_path, {"a_v2.py": "x = 1\n"}, "A_V2.py", "gone_v2.py")

    assert result.deleted == () and kept == {"a_v2.py"}
    assert [s.path for s in result.skipped] == ["A_V2.py", "gone_v2.py"]
    assert all("tracks under exactly that name" in s.reason for s in result.skipped)


def test_any_file_level_item_for_a_path_is_enough_not_the_first(tmp_path):
    repo = files_repo(tmp_path, {"a_v2.py": "x = 1\n"})
    items = [V("a_v2.py", end_col=6), V("a_v2.py")]
    with hand_sandbox(repo, items, [path_rule()]) as sandbox:
        result = apply_file_remedies(sandbox)

    assert result.deleted == ("a_v2.py",) and result.skipped == ()


def test_a_git_failure_while_deciding_leaves_the_sandbox_untouched(tmp_path, monkeypatch):
    """Every search runs before any deletion: a grep that times out, after
    both files have passed every other check, must not leave either deleted
    in a sandbox the runner is about to discard as unanswered."""
    real = remedies.git

    def failing(repo, *args, **kwargs):
        if args[0] == "grep":
            raise GitGuardError("git grep timed out")
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(remedies, "git", failing)
    repo = files_repo(tmp_path, {"a_v2.py": "x = 1\n", "b_v2.py": "x = 1\n"})
    with hand_sandbox(repo, [V("a_v2.py"), V("b_v2.py")], [path_rule()]) as sandbox:
        with pytest.raises(GitGuardError):
            apply_file_remedies(sandbox)

        assert (sandbox.path / "a_v2.py").is_file() and (sandbox.path / "b_v2.py").is_file()
