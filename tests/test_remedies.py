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
handlers_v2 but itself, PLANTED.json, the principles' `bad` example and a
beads issue (checked below). The hardening tests use small repositories of
their own with the work list and rules set by hand, as tests/test_fix_scope.py
does.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from curamoir.fix_scope import (
    FILE_DELETED,
    ScopeCheckError,
    lint_sandbox,
    verify_fix,
)
from curamoir.git_guard import prepare_sandbox, snapshot_git_state
from curamoir.remedies import apply_file_remedies
from curamoir.rules import Rule
from tests.conftest import committed_sample
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
    """A one-commit repository; `setup(repo)` writes its files."""
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
    sandbox = prepare_sandbox(repo, "HEAD", parent=repo.parent)
    sandbox.work_list = type("WorkList", (), {"items": tuple(items)})()
    sandbox.rules = tuple(rules)
    return sandbox


# --- the acceptance path -----------------------------------------------------------

def test_nothing_in_the_fixture_refers_to_the_variant_but_prose():
    """The premise of the acceptance: deleting handlers_v2.py breaks nothing,
    because no source or config file names it."""
    fixture = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures", "sample-repo")
    found = subprocess.run(["git", "grep", "-l", "-w", "handlers_v2", "--", "."], cwd=fixture,
                           capture_output=True, text=True).stdout.split()
    assert sorted(found) == [".beads/issues.jsonl", ".curamoir/principles.md", "PLANTED.json"]


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
    """The 1:1 exception is not the agent's: without `remedy: delete_file`
    the deletion is FILE_DELETED, whoever made it."""
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
    """PLANTED.json and principles.md name `handlers_v2.py` by path; that is
    a description of the file, not a use of it."""
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
