"""E3-R2 (cm-fkd.3): the PR review step verifies a sweep fix's scope, in code.

The acceptance criterion is that a sweep fix which changes a function
signature, adds a dependency, or edits a file with no violation is rejected
at the PR review step, independently of the prompt. So the fixes here are
written by the test, not by a model — the verifier must not care who made
them — and each out-of-scope one must be refused with the right kind and a
location, while the minimal fix of a planted violation must pass.

Grouped by subject: the adapter end to end (sandbox, work list, principles,
git state), then each rule — files and dependencies, the window, the header
exception, signatures, new imports — then line numbering. The git layer
under the adapter (hardened git, the snapshot, the sandbox itself) is
tests/test_git_guard.py.

**Fixture provenance.** The adapter tests run in a sandbox
(git_guard.prepare_sandbox) of a git repository made from a copy of
`fixtures/sample-repo` with tests/test_lint.py's regex rules installed (the
same five planted violations tests/test_sweep.py pins), and the work list
comes from the real `scripts/curamoir-lint` run in that sandbox at its base
(fix_scope.lint_sandbox). Line numbers quoted below are the fixture's, from
PLANTED.json: billing.py 15 (`print`), 37 (`raise RuntimeError(...)`), 22
(the discount bug, which is not a lint violation), handlers.py 22 and 31,
and handlers_v2.py's name. Tests on small repositories of their own set a
sandbox's work list by hand; the pure-core tests build their own inputs.
"""

import ast
import os
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from curamoir.fix_scope import (
    ADDED,
    BASE_MOVED,
    DELETED,
    DEPENDENCY_CHANGE,
    ENCODING_DECLARATION,
    FILE_ADDED,
    FILE_DELETED,
    FILE_NOT_IN_WORK_LIST,
    GIT_INTERNALS_CHANGED,
    HIDDEN_CHANGE,
    MODIFIED,
    NEW_DEPENDENCY,
    OUTSIDE_VIOLATION,
    SIGNATURE_CHANGE,
    UNVERIFIABLE,
    WINDOW,
    Breach,
    FileChange,
    Hunk,
    ScopeCheckError,
    ScopeVerdict,
    _only_header_statements,
    _restore_from_base,
    base_known_modules,
    base_principles,
    check_scope,
    compute_hunks,
    drop_breaching_paths,
    is_dependency_file,
    lint_sandbox,
    parse_hunks,
    script_package,
    verify_fix,
)
from curamoir.git_guard import (
    GitGuardError,
    prepare_sandbox,
    resolve_base,
    snapshot_git_state,
)
from curamoir.sweep import work_list
from tests.conftest import committed_sample

BILLING = "src/sample/core/billing.py"
HANDLERS = "src/sample/web/handlers.py"
VARIANT = "src/sample/web/handlers_v2.py"
PRINCIPLES = ".curamoir/principles.md"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout


def edit(root, path, old, new):
    target = root / path
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} is not exactly once in {path}"
    target.write_text(text.replace(old, new), encoding="utf-8")


def verdict(sandbox):
    """The snapshot is taken here, after the test's edit: these tests edit
    only the work tree, so .git is as it was before them (the git-state tests
    take theirs first)."""
    return verify_fix(sandbox, git_state=snapshot_git_state(sandbox))


def kinds_at(result):
    return {(b.kind, b.path, b.line) for b in result.breaches}


def git_repo(tmp_path, files, name="plain"):
    """A one-commit repository holding `files` (path -> text, written as bytes
    so no newline is translated)."""
    repo = tmp_path / name
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_bytes(text.encode("utf-8"))
    for command in (["init", "--initial-branch=main"],
                    ["config", "user.email", "test@example.invalid"],
                    ["config", "user.name", "test"], ["add", "-A"], ["commit", "-m", "base"]):
        subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
    return repo


def sandbox_of(repo, *violations):
    """A sandbox of `repo` at HEAD whose work list is `violations`, set by
    hand where lint_sandbox would set the real lint's, and no rules."""
    sandbox = prepare_sandbox(repo, "HEAD", parent=repo.parent)
    sandbox.work_list = SimpleNamespace(items=list(violations), tier1=())
    sandbox.rules = ()
    return sandbox


def linted_sandbox(repo):
    sandbox = prepare_sandbox(repo, "HEAD", parent=repo.parent)
    lint_sandbox(sandbox)
    return sandbox


def permit_signature_changes(repo, principle_id):
    """Commit `permits: signature_change` on `principle_id` in `repo`."""
    path = repo / PRINCIPLES
    text = path.read_text(encoding="utf-8")
    head, tail = text.split(f"## {principle_id}\n", 1)
    tail = tail.replace("- **promoted_on:** null\n",
                        "- **promoted_on:** null\n- **permits:** `signature_change`\n", 1)
    path.write_text(f"{head}## {principle_id}\n{tail}", encoding="utf-8")
    git(repo, "commit", "-qam", f"permit {principle_id}")


@dataclass(frozen=True)
class V:
    path: str
    line: int
    end_line: int = None
    col: int = 5
    end_col: int = 10
    principle_id: str = "p"
    rule_id: str = "r"

    def __post_init__(self):
        if self.end_line is None:
            object.__setattr__(self, "end_line", self.line)


def modified(path, old, new):
    return FileChange(path, MODIFIED, old, new)


def numbered(count, prefix="x"):
    return "".join(f"{prefix}{n} = {n}\n" for n in range(1, count + 1))


# --- the adapter, end to end ---------------------------------------------------------

def test_the_minimal_fix_of_a_planted_violation_is_in_scope(sandboxed):
    """The print on billing.py:15 replaced by the module logger, on the same
    line: what E3-R2 means by a scoped fix."""
    edit(sandboxed.path, BILLING, 'print("calculating line total")',
         'logger.info("calculating line total")')

    result = verdict(sandboxed)

    assert result.ok, result.render()
    assert result.paths == (BILLING,) and not result.nothing_changed


def test_fixing_every_planted_violation_in_place_is_in_scope(sandboxed):
    """Every line-level plant fixed, including chaining a cause, which edits
    the `except` line above the `raise` (inside WINDOW), and the variant file
    deleted, which is the fix for a violation that is the file's name."""
    root = sandboxed.path
    edit(root, BILLING, 'print("calculating line total")', 'logger.info("calculating line total")')
    edit(root, BILLING, "    except OSError:\n        raise RuntimeError(\"could not load rates\")",
         "    except OSError as error:\n"
         "        raise RuntimeError(f\"could not load rates from {path}\") from error")
    edit(root, HANDLERS, 'print(f"request failed: {exception}")',
         'logger.error("request failed: %s", exception)')
    edit(root, HANDLERS, '    except IndexError:\n        raise ValueError("bad token")',
         '    except IndexError as error:\n        raise ValueError(f"bad token: {raw!r}") from error')
    (root / VARIANT).unlink()

    result = verdict(sandboxed)

    assert result.ok, result.render()
    assert result.paths == (BILLING, HANDLERS, VARIANT)


def test_no_change_is_in_scope_and_fixes_nothing(sandboxed):
    """An ok verdict with no paths is not a fix: the runner opens no PR and
    records nothing fixed (ScopeVerdict.nothing_changed)."""
    result = verdict(sandboxed)

    assert result.ok and result.paths == () and result.nothing_changed
    assert "nothing changed" in result.render()


def test_the_verdict_renders_every_breach_with_its_location(sandboxed):
    edit(sandboxed.path, BILLING, "return total * (1 - percent)",
         "return total * (1 - percent / 100)")
    (sandboxed.path / "new.py").write_text("x = 1\n", encoding="utf-8")

    result = verdict(sandboxed)
    text = result.render()

    assert text.startswith("out of scope: 2 breach(es)")
    assert f"{OUTSIDE_VIOLATION} {BILLING}:22" in text
    assert f"{FILE_ADDED} new.py" in text
    assert result.as_dict()["breaches"][0]["kind"] in (FILE_ADDED, OUTSIDE_VIOLATION)
    assert result.kinds() == (FILE_ADDED, OUTSIDE_VIOLATION)


def test_a_mode_change_cannot_be_verified(sandboxed):
    (sandboxed.path / BILLING).chmod(0o755)

    assert verdict(sandboxed).kinds() == (UNVERIFIABLE,)


def test_a_fix_that_does_not_parse_cannot_be_verified(sandboxed):
    edit(sandboxed.path, BILLING, 'print("calculating line total")',
         'logger.info("calculating line total"')

    assert verdict(sandboxed).kinds() == (UNVERIFIABLE,)


def test_verify_fix_without_a_snapshot_refuses_before_git_runs(sandboxed, tmp_path):
    """Fails closed: a caller that forgot the snapshot gets no verdict, and a
    planted fsmonitor does not run."""
    marker = tmp_path / "ran"
    config = sandboxed.path / ".git" / "config"
    config.write_text(config.read_text() + f"[core]\n\tfsmonitor = touch {marker}\n")

    with pytest.raises(ScopeCheckError, match="no git-state snapshot"):
        verify_fix(sandboxed, git_state=None)
    with pytest.raises(TypeError):
        verify_fix(sandboxed)
    assert not marker.exists()


def test_verify_fix_reviews_only_a_sandbox_and_only_its_own_snapshot(sandboxed, tmp_path):
    other = sandbox_of(git_repo(tmp_path, {"m.py": "x = 1\n"}), V("m.py", 1))

    with pytest.raises(ScopeCheckError, match="not a sandbox"):
        verify_fix(sandboxed.path, git_state=snapshot_git_state(sandboxed))
    with pytest.raises(ScopeCheckError, match="snapshot is of"):
        verify_fix(sandboxed, git_state=snapshot_git_state(other))
    assert issubclass(ScopeCheckError, GitGuardError)


def test_verify_fix_needs_the_work_list_linted_in_the_sandbox(tmp_path):
    """The work list is the sandbox's own, from its base: one computed in the
    user's checkout (another commit, uncommitted edits) cannot be passed in."""
    sandbox = prepare_sandbox(committed_sample(tmp_path), "HEAD", parent=tmp_path)

    with pytest.raises(ScopeCheckError, match="no work list"):
        verify_fix(sandbox, git_state=snapshot_git_state(sandbox))


def test_lint_sandbox_refuses_a_tree_that_is_not_the_base(tmp_path):
    sandbox = prepare_sandbox(committed_sample(tmp_path), "HEAD", parent=tmp_path)
    (sandbox.path / BILLING).write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ScopeCheckError, match="not the base"):
        lint_sandbox(sandbox)
    assert sandbox.work_list is None


def test_lint_sandbox_records_the_work_list_of_the_base(sandboxed):
    """The same violations the lint reports in the clean source at that commit."""
    work = work_list(sandboxed.source)

    assert sandboxed.work_list.paths == work.paths == (BILLING, HANDLERS, VARIANT)
    assert [v.fingerprint for v in sandboxed.work_list.items] == [
        v.fingerprint for v in work.items]


# The principles a fix may lean on are the base commit's.

def test_a_signature_change_is_allowed_where_a_principle_at_the_base_permits_it(tmp_path):
    """structured-logging's violation is the print two lines below `def
    line_total`, and its permission is committed in the base."""
    repo = committed_sample(tmp_path)
    permit_signature_changes(repo, "structured-logging")
    sandbox = linted_sandbox(repo)
    edit(sandbox.path, BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")

    assert base_principles(sandbox).by_id("structured-logging").permits == ("signature_change",)
    result = verdict(sandbox)
    assert result.ok, result.render()


def test_a_permission_written_into_the_tree_grants_nothing(sandboxed):
    """The fix agent can edit `.curamoir/principles.md` in its sandbox; the
    review reads principles from the base commit, so the edit is an
    out-of-scope file and the signature change it tried to permit is still
    refused."""
    root = sandboxed.path
    text = (root / PRINCIPLES).read_text(encoding="utf-8")
    (root / PRINCIPLES).write_text(text.replace(
        "- **promoted_on:** null\n",
        "- **promoted_on:** null\n- **permits:** `signature_change`\n"), encoding="utf-8")
    edit(root, BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")

    found = kinds_at(verdict(sandboxed))

    assert (SIGNATURE_CHANGE, BILLING, 13) in found
    assert (FILE_NOT_IN_WORK_LIST, PRINCIPLES, None) in found


def test_principles_that_do_not_parse_at_the_base_give_no_verdict(tmp_path):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n", PRINCIPLES: "## a\n## a\n"})
    sandbox = sandbox_of(repo, V("m.py", 1))

    with pytest.raises(ScopeCheckError, match="principles"):
        verdict(sandbox)


# The sandbox's git state, and what git cannot see.

def test_an_edit_to_the_sandboxs_git_config_fails_before_git_runs_again(sandboxed, tmp_path):
    marker = tmp_path / "ran"
    state = snapshot_git_state(sandboxed)
    config = sandboxed.path / ".git" / "config"
    config.write_text(config.read_text() + f"[core]\n\tfsmonitor = touch {marker}\n")
    edit(sandboxed.path, BILLING, 'print("calculating line total")', 'logger.info("x")')

    result = verify_fix(sandboxed, git_state=state)

    assert result.kinds() == (GIT_INTERNALS_CHANGED,)
    assert ".git/config" in result.render()
    assert result.paths == ()
    assert not marker.exists()


def test_a_gitattributes_added_anywhere_fails_the_verdict(sandboxed):
    state = snapshot_git_state(sandboxed)
    (sandboxed.path / "src" / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")

    result = verify_fix(sandboxed, git_state=state)

    assert result.kinds() == (GIT_INTERNALS_CHANGED,)
    assert "src/.gitattributes" in result.render()


def test_a_head_moved_off_the_base_is_its_own_breach(sandboxed):
    """The diff is always against the base SHA, but a sandbox whose HEAD left
    it has been through something no fix step does."""
    edit(sandboxed.path, BILLING, 'print("calculating line total")', 'logger.info("x")')
    git(sandboxed.path, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qam", "moved")

    result = verdict(sandboxed)

    assert result.kinds() == (BASE_MOVED,)


def test_a_file_written_where_git_cannot_see_it_fails_the_verdict(tmp_path):
    """Inside an uninitialised submodule, a new file is invisible to every git
    command; the snapshot's work-tree manifest still sees it."""
    sub = git_repo(tmp_path, {"s.txt": "s\n"}, name="sub")
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    git(repo, "commit", "-qm", "submodule")
    sandbox = sandbox_of(repo, V("m.py", 1))
    state = snapshot_git_state(sandbox)
    (sandbox.path / "vendor" / "boot.py").write_text("import os\n", encoding="utf-8")

    result = verify_fix(sandbox, git_state=state)

    assert kinds_at(result) == {(HIDDEN_CHANGE, "vendor/boot.py", None)}


def test_a_file_git_ignores_is_seen_and_refused(sandboxed):
    """The in-checkout review listed untracked files with
    `--exclude-standard`, so `.venv/lib/requests.py` was invisible and the
    verdict ok. In the sandbox nothing is excluded."""
    state = snapshot_git_state(sandboxed)
    (sandboxed.path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    venv = sandboxed.path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "requests.py").write_text("def get(*a):\n    return None\n", encoding="utf-8")

    found = kinds_at(verify_fix(sandboxed, git_state=state))

    assert (FILE_ADDED, ".venv/lib/requests.py", None) in found
    assert (FILE_ADDED, ".gitignore", None) in found


def test_a_self_ignoring_nested_gitignore_hides_nothing(sandboxed):
    state = snapshot_git_state(sandboxed)
    hidden = sandboxed.path / "src" / "sample" / "hidden"
    hidden.mkdir()
    (hidden / ".gitignore").write_text("*\n", encoding="utf-8")
    (hidden / "boot.py").write_text("import os\n", encoding="utf-8")

    found = kinds_at(verify_fix(sandboxed, git_state=state))

    assert found == {(FILE_ADDED, "src/sample/hidden/.gitignore", None),
                     (FILE_ADDED, "src/sample/hidden/boot.py", None)}


def test_an_nfd_named_work_list_path_matches_gits_nfc_one(tmp_path):
    """macOS file systems hand back names in the form they were written, and
    git (core.precomposeunicode) reports NFC; the comparison is in NFC on
    both sides, so the same file is the same path."""
    nfc = unicodedata.normalize("NFC", "café.py")
    nfd = unicodedata.normalize("NFD", "café.py")
    assert nfc != nfd
    repo = git_repo(tmp_path, {nfc: "x = 1\n"})
    sandbox = sandbox_of(repo, V(nfd, 1))
    state = snapshot_git_state(sandbox)
    (sandbox.path / nfc).write_text("x = 2\n", encoding="utf-8")

    result = verify_fix(sandbox, git_state=state)

    assert result.ok, result.render()
    assert check_scope([modified(nfc, "x = 1\n", "x = 2\n")], [V(nfd, 1)]).ok


# Nested repositories and submodules: git must never open one.

def submodule_repo(tmp_path):
    sub = git_repo(tmp_path, {"s.txt": "s\n"}, name="sub")
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    git(repo, "commit", "-qm", "submodule")
    return repo


@pytest.mark.parametrize("where", ["sub", "vendor"])
def test_a_nested_repository_the_agent_makes_fails_before_git_runs(tmp_path, where):
    """In a fresh directory and at a real (uninitialised) submodule path: the
    planted repository's fsmonitor would run if git opened it, and it never
    does, because the verdict is given before any git command."""
    marker = tmp_path / "ran"
    sandbox = sandbox_of(submodule_repo(tmp_path), V("m.py", 1))
    state = snapshot_git_state(sandbox)
    nested = sandbox.path / where / ".git"
    for name in ("objects", "refs"):
        (nested / name).mkdir(parents=True, exist_ok=True)
    (nested / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (nested / "config").write_text(
        f"[core]\n\trepositoryformatversion = 0\n\tfsmonitor = touch {marker}\n",
        encoding="utf-8")
    (sandbox.path / where / "f.py").write_text("x = 1\n", encoding="utf-8")
    (sandbox.path / "m.py").write_text("x = 2\n", encoding="utf-8")

    result = verify_fix(sandbox, git_state=state)

    assert result.kinds() == (GIT_INTERNALS_CHANGED,)
    assert f"{where}/.git/config" in result.render()
    assert not marker.exists()


def test_a_removed_submodule_is_seen_though_git_is_told_to_ignore_submodules(tmp_path):
    sandbox = sandbox_of(submodule_repo(tmp_path), V("m.py", 1))
    state = snapshot_git_state(sandbox)
    (sandbox.path / "vendor").rmdir()

    result = verify_fix(sandbox, git_state=state)

    assert kinds_at(result) == {(HIDDEN_CHANGE, "vendor", None)}


# --- files and dependencies ----------------------------------------------------------

def test_editing_a_file_with_no_violation_is_rejected(sandboxed):
    """Acceptance: config.py has no planted violation."""
    path = "src/sample/config.py"
    target = sandboxed.path / path
    target.write_text(target.read_text(encoding="utf-8") + "# tidy\n", encoding="utf-8")

    assert kinds_at(verdict(sandboxed)) == {(FILE_NOT_IN_WORK_LIST, path, None)}


def test_adding_a_file_is_rejected(sandboxed):
    (sandboxed.path / "src" / "sample" / "log.py").write_text("import logging\n",
                                                             encoding="utf-8")

    assert kinds_at(verdict(sandboxed)) == {(FILE_ADDED, "src/sample/log.py", None)}


def test_a_staged_new_file_is_rejected_too(sandboxed):
    (sandboxed.path / "helpers.py").write_text("x = 1\n", encoding="utf-8")
    git(sandboxed.path, "add", "helpers.py")

    assert kinds_at(verdict(sandboxed)) == {(FILE_ADDED, "helpers.py", None)}


def test_deleting_a_file_whose_violations_are_in_its_text_is_rejected(sandboxed):
    (sandboxed.path / BILLING).unlink()

    assert kinds_at(verdict(sandboxed)) == {(FILE_DELETED, BILLING, None)}


def test_a_rename_is_a_deletion_and_an_addition(sandboxed):
    """Renaming the variant to a new name keeps the variant: the new path was
    never on the work list."""
    git(sandboxed.path, "mv", VARIANT, "src/sample/web/pagination.py")

    found = kinds_at(verdict(sandboxed))

    assert (FILE_ADDED, "src/sample/web/pagination.py", None) in found
    assert not any(kind == FILE_DELETED for kind, _, _ in found)


def test_adding_a_dependency_to_pyproject_and_the_lockfile_is_rejected(sandboxed):
    """Acceptance: a new dependency, declared and locked."""
    edit(sandboxed.path, "pyproject.toml", 'requires-python = ">=3.12"\n',
         'requires-python = ">=3.12"\ndependencies = ["structlog>=24"]\n')
    (sandboxed.path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    found = kinds_at(verdict(sandboxed))

    assert (DEPENDENCY_CHANGE, "pyproject.toml", None) in found
    assert (DEPENDENCY_CHANGE, "uv.lock", None) in found
    assert (FILE_ADDED, "uv.lock", None) in found


def test_adding_a_package_json_dependency_is_rejected(sandboxed):
    (sandboxed.path / "package.json").write_text('{"dependencies": {"pino": "^9"}}\n',
                                                 encoding="utf-8")

    assert (DEPENDENCY_CHANGE, "package.json", None) in kinds_at(verdict(sandboxed))


def test_a_dependency_file_on_the_work_list_is_still_refused():
    """Even with a violation in it: the whole file is out of bounds."""
    change = modified("pyproject.toml", 'name = "a"\n', 'name = "b"\n')

    found = check_scope([change], [V("pyproject.toml", 1)]).breaches

    assert [b.kind for b in found] == [DEPENDENCY_CHANGE]


@pytest.mark.parametrize("path", ["pyproject.toml", "sub/package.json", "uv.lock", "poetry.lock",
                                  "requirements.txt", "requirements-dev.txt",
                                  "requirements/base.in", "constraints.txt", "yarn.lock",
                                  "pnpm-lock.yaml", "package-lock.json", "Pipfile.lock",
                                  "setup.cfg", "setup.py"])
def test_dependency_files_are_recognised_by_name(path):
    assert is_dependency_file(path)


@pytest.mark.parametrize("path", ["src/requirements.py", "docs/package.md", "lock.py"])
def test_other_files_are_not_dependency_files(path):
    assert not is_dependency_file(path)


def test_deleting_a_file_level_violation_is_the_fix_when_its_rule_says_so():
    file_level = V("a_v2.py", 1, end_line=1, col=1, end_col=1, rule_id="no-variant")
    deleted = FileChange("a_v2.py", DELETED, "x = 1\n", None)

    assert check_scope([deleted], [file_level], deletable_rules={"no-variant"}).ok


def test_deleting_a_file_level_violation_whose_rule_names_no_remedy_is_refused():
    """A `target: path` rule without `remedy: delete_file` may want a rename,
    not a deletion. Requiring the remedy means a file-level violation alone
    licenses no deletion: only one of a rule that asks for it, which the
    runner applies (cm-ul5)."""
    file_level = V("a_v2.py", 1, end_line=1, col=1, end_col=1, rule_id="naming")
    deleted = FileChange("a_v2.py", DELETED, "x = 1\n", None)

    found = check_scope([deleted], [file_level], deletable_rules={"other"}).breaches

    assert [(b.kind, b.path) for b in found] == [(FILE_DELETED, "a_v2.py")]
    assert "remedy: delete_file" in found[0].detail
    assert not check_scope([deleted], [file_level]).ok


def test_an_added_file_is_refused_even_if_a_violation_names_it():
    """Cannot happen with a work list from the base commit, but the rule is
    the rule: an added path's text was not linted."""
    found = check_scope([FileChange("n.py", ADDED, None, "x = 1\n")], [V("n.py", 1)])

    assert [b.kind for b in found.breaches] == [FILE_ADDED]


# --- the window ----------------------------------------------------------------------

def test_a_refactor_far_from_the_violation_is_rejected(sandboxed):
    """billing.py is on the work list, but line 22 (the discount bug) is 7
    lines from the print and 15 from the raise: fixing a bug the lint did
    not report is beyond the violation, however welcome."""
    edit(sandboxed.path, BILLING, "return total * (1 - percent)",
         "return total * (1 - percent / 100)")

    assert kinds_at(verdict(sandboxed)) == {(OUTSIDE_VIOLATION, BILLING, 22)}


@pytest.mark.parametrize("line,ok", [(10, True), (10 + WINDOW, True), (10 + WINDOW + 1, False),
                                     (10 - WINDOW, True), (10 - WINDOW - 1, False)])
def test_the_window_is_exactly_WINDOW_lines_either_side(line, ok):
    old = numbered(30)
    new = old.replace(f"x{line} = {line}\n", f"x{line} = {line} + 0\n")

    result = check_scope([modified("m.py", old, new)], [V("m.py", 10)])

    assert result.ok is ok, result.render()


def test_a_multi_line_violation_is_measured_from_both_ends():
    old = numbered(30)
    new = old.replace("x16 = 16\n", "x16 = 0\n")

    assert check_scope([modified("m.py", old, new)], [V("m.py", 10, end_line=13)]).ok


def test_an_end_at_column_1_is_the_line_before_it():
    """end_col is exclusive: a match ending at 14:1 ends on line 13."""
    old = numbered(30)
    new = old.replace("x17 = 17\n", "x17 = 0\n")

    tight = V("m.py", 10, end_line=14, end_col=1)
    assert not check_scope([modified("m.py", old, new)], [tight]).ok
    assert check_scope([modified("m.py", old, new)], [V("m.py", 10, end_line=14, end_col=2)]).ok


def test_each_breach_names_the_base_line_of_its_hunk():
    old = numbered(40)
    new = old.replace("x30 = 30\n", "x30 = 0\n").replace("x2 = 2\n", "")

    found = check_scope([modified("m.py", old, new)], [V("m.py", 15)]).breaches

    assert [(b.kind, b.line) for b in found] == [(OUTSIDE_VIOLATION, 2), (OUTSIDE_VIOLATION, 30)]


# --- the header exception ------------------------------------------------------------

def test_a_logger_added_to_the_module_header_is_in_scope(sandboxed):
    """The no-print rule's own fix puts `logger = logging.getLogger(...)` at
    module level, far from the print; the header allows imports and
    module-level assignments."""
    edit(sandboxed.path, BILLING, "TAX_RATE = 0.2\n",
         "TAX_RATE = 0.2\nimport logging as _logging\naudit = _logging.getLogger('audit')\n")
    edit(sandboxed.path, BILLING, 'print("calculating line total")',
         'audit.info("calculating line total")')

    assert verdict(sandboxed).ok


@pytest.mark.parametrize("inserted", [
    "TAX_RATE = 0.3\n",
    "from decimal import Decimal as TAX_RATE\n",
    "line_total = None\n",
])
def test_a_header_insertion_that_rebinds_an_existing_name_is_not(sandboxed, inserted):
    """Each would change what a later name means while looking like a new
    import or assignment; inserted after line 10, out of the print's window."""
    edit(sandboxed.path, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\n" + inserted)

    assert kinds_at(verdict(sandboxed)) == {(OUTSIDE_VIOLATION, BILLING, 10)}


@pytest.mark.parametrize("inserted", [
    '_boot = __import__("os").system("true")\n',
    "helper = lambda a: a * 2\n",
    "squares = [n * n for n in range(3)]\n",
    "env = __import__('os').environ\n",
    "label = f\"{__import__('os').getcwd()}\"\n",
    "level: __import__('os').getcwd() = 1\n",
    "audit = logging.getLogger(__import__('os').getcwd())\n",
    "audit = logging.getLogger(name=__name__)\n",
    "audit = logging.root.manager.getLogger('x')\n",
    "audit = TAX_RATE.getLogger('x')\n",
])
def test_a_header_insertion_that_runs_code_at_import_is_not(sandboxed, inserted):
    """S2: a header line runs when the module is imported — including by the
    sweep's own test step — so it may bind a new name only to a literal or a
    logger, never to an arbitrary expression. Each falls back to the window."""
    edit(sandboxed.path, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\n" + inserted)

    assert kinds_at(verdict(sandboxed)) == {(OUTSIDE_VIOLATION, BILLING, 10)}


@pytest.mark.parametrize("inserted", [
    "RETRIES = 3\n",
    "GREETING = 'hi'\n",
    "TIMEOUT: float = -1.5\n",
    "DEBUG = None\n",
    "LEVELS = ('info', 'debug')\n",
    "__all__ = ['line_total']\n",
    "audit: logging.Logger = logging.getLogger('audit')\n",
    "audit = logging.getLogger()\n",
])
def test_a_header_insertion_of_a_literal_or_a_logger_is_in_scope(sandboxed, inserted):
    edit(sandboxed.path, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\n" + inserted)

    assert verdict(sandboxed).ok


def test_repeating_an_import_the_module_already_has_is_harmless(sandboxed):
    edit(sandboxed.path, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\nimport logging\n")

    assert verdict(sandboxed).ok


def test_a_function_added_to_the_module_header_is_not(sandboxed):
    """After the import on line 6: well outside the print's window (12-18),
    so the header rule decides, and a function is not an import or an
    assignment. It is also a new definition, so a signature change."""
    edit(sandboxed.path, BILLING, "import logging\n",
         "import logging\n\n\ndef helper():\n    return 1\n")

    result = verdict(sandboxed)

    assert OUTSIDE_VIOLATION in result.kinds()
    assert SIGNATURE_CHANGE in result.kinds()


def test_the_no_print_fix_in_a_module_without_a_logger_is_in_scope():
    """examples/python/no-print.yaml's own fix, verbatim, into a module that
    has neither the import nor the logger: both land in the header."""
    old = '"""m."""\nimport os\n\n\n\n\n\n\n\n\ndef f():\n    print(os.sep)\n'
    new = old.replace("import os\n", "import os\nimport logging\n\nlogger = logging.getLogger(__name__)\n")
    new = new.replace("print(os.sep)", "logger.info(os.sep)")

    assert check_scope([modified("m.py", old, new)], [V("m.py", 12)]).ok


def test_a_logger_from_a_module_merely_named_like_logging_is_not():
    """getLogger is trusted only on the standard `logging` module, under any
    alias; `import os as log` then `log.getLogger(...)` is someone else's call."""
    old = '"""m."""\nimport os as log\n'
    assert not _only_header_statements(["audit = log.getLogger(__name__)"], True, ast.parse(old))


@pytest.mark.parametrize("line,ok", [
    ('import { readFile } from "node:fs";', True),
    ("import fs from 'fs'", True),
    ('import "./polyfill.js";', True),
    ('const fs = require("fs");', True),
    ("const { join } = require('path')", True),
    ("// a comment", True),
    ('const run = require("child_process").execSync("touch /tmp/x");', False),
    ('import("./boot.js").then(m => m.go());', False),
    ("const logger = makeLogger();", False),
    ('require("./polyfill.js");', True),
])
def test_a_script_header_insertion_is_only_a_plain_import(line, ok):
    """The same rule for JS/TS, by line, without a parser: a static import or a
    `const x = require("…")` and nothing chained after it."""
    assert _only_header_statements([line], False) is ok


HEADER_MODULE = ('"""m."""\nimport os\n' + "\n" * 10
                 + "def f():\n    return os.sep, helper_name\n")


@pytest.mark.parametrize("inserted", [
    "print = None", "open = 'x'", "__name__ = 'x'", "__getattr__ = None",
    "__builtins__ = None", "from os import open", "helper_name = 1", "sep = 1",
    "import json as print",
])
def test_a_header_insertion_may_not_shadow_a_builtin_dunder_or_used_name(inserted):
    """S4: a new module-level name changes what a builtin, a dunder, or a name
    the module already reads (`helper_name`, `os.sep`'s `sep` is not one, but
    the attribute base `os` is) means everywhere below it."""
    new = HEADER_MODULE.replace("import os\n", f"import os\n{inserted}\n")

    result = check_scope([modified("m.py", HEADER_MODULE, new)], [V("m.py", 14)])

    if inserted == "sep = 1":
        assert result.ok, result.render()
    else:
        assert kinds_at(result) == {(OUTSIDE_VIOLATION, "m.py", 2)}, inserted


def test_any_new_header_name_is_refused_after_a_star_import():
    old = HEADER_MODULE.replace("import os\n", "import os\nfrom os.path import *\n")
    new = old.replace("from os.path import *\n", "from os.path import *\nRETRIES = 3\n")

    result = check_scope([modified("m.py", old, new)], [V("m.py", 15)])

    assert OUTSIDE_VIOLATION in result.kinds()


@pytest.mark.parametrize("old,new", [
    ('"""m."""\nX = make(\n    a,\n)\n' + "\n" * 10 + "def f():\n    return 1\n",
     '"""m."""\nX = make(\n    a,\nY = True,\n)\n' + "\n" * 10 + "def f():\n    return 1\n"),
    ('"""m."""\nDOC = """\nline\n"""\n' + "\n" * 10 + "def f():\n    return 1\n",
     '"""m."""\nDOC = """\nline\nimport os\n"""\n' + "\n" * 10 + "def f():\n    return 1\n"),
])
def test_a_header_insertion_inside_a_statement_is_not_a_header_statement(old, new):
    """C1: parsed on their own, `Y = True,` is an assignment and `import os` an
    import; in the module they are a keyword argument and string text."""
    result = check_scope([modified("m.py", old, new)], [V("m.py", 17)])

    assert kinds_at(result) == {(OUTSIDE_VIOLATION, "m.py", 3)}, result.render()


# --- signatures ----------------------------------------------------------------------

def test_a_signature_change_is_rejected_even_beside_the_violation(sandboxed):
    """Acceptance: `def line_total` is two lines above the print, inside the
    window, so only the signature check can catch this."""
    edit(sandboxed.path, BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")
    edit(sandboxed.path, BILLING, 'print("calculating line total")',
         'logger.info("calculating line total")')

    result = verdict(sandboxed)

    assert (SIGNATURE_CHANGE, BILLING, 13) in kinds_at(result)
    assert "line_total" in result.render()


def test_a_permission_elsewhere_in_the_file_does_not_cover_this_definition(tmp_path):
    """errors-carry-context permits signature changes and has a violation in
    billing.py, at line 37; `line_total` (lines 13 to 16) is not near it."""
    repo = committed_sample(tmp_path)
    permit_signature_changes(repo, "errors-carry-context")
    sandbox = linted_sandbox(repo)
    edit(sandbox.path, BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")

    assert (SIGNATURE_CHANGE, BILLING, 13) in kinds_at(verdict(sandbox))


@pytest.mark.parametrize("before,after,name", [
    ("def f(a):\n    return a\n", "def f(a, b=1):\n    return a\n", "f"),
    ("def f(a=1):\n    return a\n", "def f(a=2):\n    return a\n", "f"),
    ("def f(a: int):\n    return a\n", "def f(a: str):\n    return a\n", "f"),
    ("def f(a) -> int:\n    return a\n", "def f(a) -> str:\n    return a\n", "f"),
    ("def f(a):\n    return a\n", "async def f(a):\n    return a\n", "f"),
    ("def f(a):\n    return a\n", "@cache\ndef f(a):\n    return a\n", "f"),
    ("class C(A):\n    pass\n", "class C(B):\n    pass\n", "C"),
    ("class C:\n    def m(self):\n        pass\n", "class C:\n    def m(self, x):\n        pass\n",
     "C.m"),
    ("def f(a):\n    return a\n", "def g(a):\n    return a\n", "f"),
])
def test_python_signature_changes_are_seen(before, after, name):
    found = check_scope([modified("m.py", before, after)], [V("m.py", 1)]).breaches

    assert any(b.kind == SIGNATURE_CHANGE and f"`{name}`" in b.detail for b in found), found


def test_a_body_change_is_not_a_signature_change():
    before = "def f(a):\n    print(a)\n    return a\n"
    after = "def f(a):\n    logger.info(a)\n    return a\n"

    assert check_scope([modified("m.py", before, after)], [V("m.py", 2)]).ok


def test_a_definition_under_a_module_level_if_is_compared_too():
    before = "if X:\n    def f(a):\n        return a\n"
    after = "if X:\n    def f(a, b):\n        return a\n"

    found = check_scope([modified("m.py", before, after)], [V("m.py", 3)]).breaches

    assert [b.kind for b in found] == [SIGNATURE_CHANGE]


@pytest.mark.parametrize("before,after", [
    ("export function f(a: string): void {\n  g();\n}\n",
     "export function f(a: string, b: number): void {\n  g();\n}\n"),
    ("const f = (a) => {\n  g();\n};\n", "const f = (a, b) => {\n  g();\n};\n"),
    ("class C {\n  m(a: string): void {\n    g();\n  }\n}\n",
     "class C {\n  m(a: string, b: string): void {\n    g();\n  }\n}\n"),
    ("export interface I {\n  a: string;\n}\n", "export interface J {\n  a: string;\n}\n"),
])
def test_a_changed_js_ts_declaration_line_is_a_signature_change(before, after):
    found = check_scope([modified("m.ts", before, after)], [V("m.ts", 1)]).breaches

    assert [b.kind for b in found] == [SIGNATURE_CHANGE]


def test_a_changed_js_ts_statement_is_not():
    before = 'export function f(m: string): void {\n  console.log(`x ${m}`);\n}\n'
    after = 'export function f(m: string): void {\n  logger.info(`x ${m}`);\n}\n'

    assert check_scope([modified("m.ts", before, after)], [V("m.ts", 2)]).ok


SHIFTED = ('"""m."""\nimport os\n\ndef a():\n'
           + "".join(f"    x{n} = {n}\n" for n in range(5, 41)))
CONSTANTS = "".join(f"C{n} = {n}\n" for n in range(10))
INNER = "    def inner():\n        return 1\n"


def test_an_added_definition_is_measured_in_the_fixed_files_lines():
    """C8: ten header lines push base line 20 to fixed line 30. A definition
    added there is ten lines from the permitted violation at base 30, though
    its fixed line number coincides with it."""
    new = SHIFTED.replace("import os\n", "import os\n" + CONSTANTS)
    new = new.replace("    x20 = 20\n", "    x20 = 20\n" + INNER)

    result = check_scope([modified("m.py", SHIFTED, new)], [V("m.py", 30)], permitting={"p"})

    assert SIGNATURE_CHANGE in result.kinds()


def test_an_added_definition_at_a_permitted_violation_after_a_shift_is_allowed():
    new = SHIFTED.replace("import os\n", "import os\n" + CONSTANTS)
    new = new.replace("    x30 = 30\n", "    x30 = 30\n" + INNER)

    result = check_scope([modified("m.py", SHIFTED, new)], [V("m.py", 30)], permitting={"p"})

    assert result.ok, result.render()


def test_a_permitted_span_ending_inside_a_replaced_hunk_reaches_the_hunks_end():
    """The violation at line 5 permits base lines 2-8; the fix replaces base
    6-8 with six lines whose last two are a nested `helper` (fixed line 10).
    The span's end is inside the hunk, so it maps to the hunk's last fixed
    line, 11, not its first."""
    old = "def f():\n" + "".join(f"    x{n} = {n}\n" for n in range(2, 16))
    new = old.replace("    x6 = 6\n    x7 = 7\n    x8 = 8\n",
                      "    y1 = 1\n    y2 = 2\n    y3 = 3\n    y4 = 4\n"
                      "    def helper():\n        return 1\n")
    assert new.splitlines()[9] == "    def helper():"

    result = check_scope([modified("m.py", old, new)], [V("m.py", 5)], permitting={"p"})

    assert result.ok, result.render()


# --- new imports ---------------------------------------------------------------------

def test_importing_a_package_the_repo_does_not_use_is_a_new_dependency(sandboxed):
    """No manifest touched, but `structlog` is imported nowhere in the
    repository: a dependency in all but the declaration."""
    edit(sandboxed.path, BILLING, '    print("calculating line total")',
         '    import structlog\n    structlog.get_logger().info("calculating line total")')

    result = verdict(sandboxed)

    assert result.kinds() == (NEW_DEPENDENCY,)
    assert result.breaches[0].path == BILLING and "structlog" in result.breaches[0].detail


def test_importing_the_standard_library_or_the_repos_own_modules_is_not(sandboxed):
    edit(sandboxed.path, BILLING, '    print("calculating line total")',
         '    import sys\n    from sample import config\n'
         '    logger.info("calculating line total %s", sys.argv[0])')

    result = verdict(sandboxed)

    assert result.ok, result.render()


def test_a_new_js_ts_package_import_is_a_new_dependency_and_a_relative_one_is_not():
    before = "export function f(m: string): void {\n  console.log(m);\n}\n"
    local = "import { logger } from './log';\n" + before.replace("console.log", "logger.info")
    package = "import pino from 'pino';\n" + before.replace("console.log", "pino().info")

    assert check_scope([modified("m.ts", before, local)], [V("m.ts", 2)]).ok
    found = check_scope([modified("m.ts", before, package)], [V("m.ts", 2)]).breaches
    assert [b.kind for b in found] == [NEW_DEPENDENCY]
    assert check_scope([modified("m.ts", before, package)], [V("m.ts", 2)],
                       known_modules=frozenset({"pino"})).ok


@pytest.mark.parametrize("specifier,package", [
    ("pino", "pino"), ("lodash/fp", "lodash"), ("@scope/pkg/sub", "@scope/pkg"),
    ("node:fs/promises", "fs"), ("./log", None), ("../x", None), ("/abs", None),
])
def test_script_package_names(specifier, package):
    assert script_package(specifier) == package


def test_base_known_modules_reads_imports_with_git_on_this_platform(tmp_path):
    """C3: Apple git's `grep -E` has no `\\s`; an import used elsewhere is known."""
    repo = git_repo(tmp_path, {"a.py": "import requests\nfrom yaml import safe_load\n",
                               "b.js": "const x = require( 'left-pad' )\n"})

    known = base_known_modules(repo, resolve_base(repo, "HEAD"))

    assert {"requests", "yaml", "left-pad"} <= known


def test_only_python_files_and_packages_are_local_modules(tmp_path):
    """C4: docs/pandas.md is not a module called pandas."""
    repo = git_repo(tmp_path, {"docs/pandas.md": "x\n", "pkg/__init__.py": "",
                               "src/tool/run.py": "", "stub.pyi": "",
                               "m.py": "import os\n\n\n\n\ndef f():\n    return os.sep\n"})

    known = base_known_modules(repo, resolve_base(repo, "HEAD"))

    assert {"pkg", "tool", "run", "stub", "m"} <= known
    assert "pandas" not in known and "docs" not in known
    sandbox = sandbox_of(repo, V("m.py", 7))
    (sandbox.path / "m.py").write_text("import os\n\n\n\n\ndef f():\n    import pandas\n"
                                       "    return os.sep\n", encoding="utf-8")
    assert verdict(sandbox).kinds() == (NEW_DEPENDENCY,)


@pytest.mark.parametrize("call", ["importlib.import_module('pandas')", "__import__('pandas')",
                                  "import_module('pandas.io')"])
def test_a_dynamic_import_of_a_literal_name_is_an_import(call):
    """C9: the string-literal forms are imports for the new-dependency check."""
    old = "import importlib\nfrom importlib import import_module\n\n\ndef f():\n    return 1\n"
    new = old.replace("return 1", f"return {call}")

    result = check_scope([modified("m.py", old, new)], [V("m.py", 6)])

    assert kinds_at(result) == {(NEW_DEPENDENCY, "m.py", 6)}


_JS_TODO = "const a = 1;\nfunction f(x) {\n  return x; // TODO\n}\n"


def _js_fix(line, old=_JS_TODO):
    return old.replace("  return x; // TODO", "  " + line + "\n  return x;")


@pytest.mark.parametrize("line", [
    "const l = require(`lodash`);",
    "const l = await import(`lodash`);",
    "const l = require('lod' + 'ash');",
    "const l = await import(`lod${x}`);",
    "const l = require(name);",
    "const l = require(",
])
def test_a_js_require_or_import_of_a_template_or_a_computed_name_is_a_new_dependency(line):
    """A template literal without `${` is a literal like a quoted one. Any
    other argument (a concatenation, a template with a substitution, a
    variable, one on the next line) names a module the review cannot know,
    so it counts as new."""
    result = check_scope([modified("m.js", _JS_TODO, _js_fix(line))], [V("m.js", 3)])

    assert result.kinds() == (NEW_DEPENDENCY,)


def test_a_template_import_of_a_relative_or_known_module_and_an_old_computed_one_pass():
    old = "const m = require(name);\n" + _JS_TODO
    unchanged = old.replace(" // TODO", "")

    for line in ("const l = require(`./log`);", "const p = await import(`pino`);"):
        assert check_scope([modified("m.js", _JS_TODO, _js_fix(line))], [V("m.js", 3)],
                           known_modules=frozenset({"pino"})).ok, line
    assert check_scope([modified("m.js", old, unchanged)], [V("m.js", 4)]).ok


def test_a_second_copy_of_an_old_computed_require_is_a_new_dependency():
    """The base's computed loads are counted, not just looked up: pasting
    an existing `require(name)` line elsewhere, with `name` rebound, loads
    whatever the new binding names."""
    old = "const m = require(name);\n" + _JS_TODO
    new = _js_fix("name = 'left-pad';\n  const m = require(name);", old)

    result = check_scope([modified("m.js", old, new)], [V("m.js", 4)])

    assert result.kinds() == (NEW_DEPENDENCY,)
    assert [breach.line for breach in result.breaches] == [5]


def test_base_known_modules_reads_a_template_literal_require(tmp_path):
    repo = git_repo(tmp_path, {"m.js": "const l = require(`lodash`);\n"})

    assert "lodash" in base_known_modules(repo, resolve_base(repo, "HEAD"))


# --- line numbering ------------------------------------------------------------------

def test_parse_hunks_reads_git_minus_U0_headers():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -3 +3 @@ def f():\n-a\n+b\n"
        "@@ -10,0 +11,2 @@\n+c\n+d\n"
        "@@ -20,3 +22,0 @@\n-e\n-f\n-g\n"
    )

    assert parse_hunks(diff) == (Hunk(3, 1, 3, 1), Hunk(10, 0, 11, 2), Hunk(20, 3, 22, 0))


def test_compute_hunks_agrees_with_git_on_the_simple_cases():
    old = "a\nb\nc\nd\n"

    assert compute_hunks(old, "a\nB\nc\nd\n") == (Hunk(2, 1, 2, 1),)
    assert compute_hunks(old, "a\nb\nx\nc\nd\n") == (Hunk(2, 0, 3, 1),)
    assert compute_hunks(old, "a\nd\n") == (Hunk(2, 2, 1, 0),)


def test_a_form_feed_above_an_insertion_does_not_shift_the_lines_read(tmp_path):
    """C2: git counts lines by \\n only; str.splitlines also splits at \\f, so
    the inserted line was read as the `import sys` above it."""
    base = ("import os\n\x0c\nimport sys\n" + "\n" * 10
            + "def f():\n    return os.sep, sys.argv\n")
    sandbox = sandbox_of(git_repo(tmp_path, {"m.py": base}), V("m.py", 15))
    (sandbox.path / "m.py").write_bytes(base.replace(
        "import sys\n", "import sys\nX = os.system('rm -rf ~')\n").encode())

    assert kinds_at(verdict(sandbox)) == {(OUTSIDE_VIOLATION, "m.py", 3)}


def test_a_committed_no_diff_attribute_does_not_hide_the_hunks(tmp_path):
    """An owner's `*.py -diff` (or `binary`) makes `git diff` print "Binary
    files differ" with no hunks, and an empty hunk list passed the window
    rule; the review diffs with --text."""
    base = numbered(30)
    repo = git_repo(tmp_path, {"m.py": base, ".gitattributes": "*.py -diff\n"})
    sandbox = sandbox_of(repo, V("m.py", 5))
    (sandbox.path / "m.py").write_text(base.replace("x25 = 25\n", "x25 = 0\n"),
                                       encoding="utf-8")

    assert kinds_at(verdict(sandbox)) == {(OUTSIDE_VIOLATION, "m.py", 25)}


def test_a_lone_carriage_return_makes_a_python_file_unverifiable():
    """Python numbers lines at a lone \\r and git does not, so no line of the
    parse can be matched to a hunk."""
    result = check_scope([modified("m.py", "x = 1\n", "x = 1\ry = 2\n")], [V("m.py", 1)])

    assert result.kinds() == (UNVERIFIABLE,)


# --- encoding ----------------------------------------------------------------------

#: A module whose one violation is the TODO on line 5.
_PY_TODO = "x = 1\n\n\ndef f(a):\n    return a  # TODO\n"


def test_a_utf7_coding_cookie_cannot_smuggle_code_past_the_parse():
    """The reviewers' repro: read as UTF-8, the inserted lines are comments;
    CPython honours the cookie and decodes them as an import and a new `f`."""
    planted = "# coding: utf-7\n#+AAo-import requests+AAo-def f(a, b+AD0-1):+AAo-    return 42\n"
    new = planted + _PY_TODO.replace("  # TODO", "")
    assert "requests" in compile(new.encode("ascii"), "m.py", "exec").co_names

    result = check_scope([modified("m.py", _PY_TODO, new)], [V("m.py", 5)])

    assert result.kinds() == (UNVERIFIABLE,)
    assert "utf-7" in result.render()


@pytest.mark.parametrize("old,new,cookie_line", [
    (_PY_TODO, "# coding: utf-8\n" + _PY_TODO.replace("  # TODO", ""), 1),
    (_PY_TODO, "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n"
               + _PY_TODO.replace("  # TODO", ""), 2),
    ("# coding: utf-8\n" + _PY_TODO, _PY_TODO.replace("  # TODO", ""), 1),
])
def test_a_fix_may_not_touch_an_encoding_declaration_even_a_utf8_one(old, new, cookie_line):
    """The breach names the line the cookie is on, not always line 1."""
    line = old.count("\n") - _PY_TODO.count("\n") + 5
    result = check_scope([modified("m.py", old, new)], [V("m.py", line)])

    assert result.kinds() == (ENCODING_DECLARATION,)
    assert [breach.line for breach in result.breaches] == [cookie_line]


def test_a_base_whose_source_encoding_is_not_utf8_is_unverifiable():
    """Even with ASCII content and an in-window fix: the review reads text as
    UTF-8, so a file CPython decodes otherwise is refused, not reasoned
    about (fix_scope module docstring)."""
    old = "# -*- coding: latin-1 -*-\n" + _PY_TODO

    result = check_scope([modified("m.py", old, old.replace("  # TODO", ""))], [V("m.py", 6)])

    assert result.kinds() == (UNVERIFIABLE,)
    assert "iso-8859-1" in result.render()


def test_a_utf8_byte_order_mark_is_utf8():
    old = "﻿" + _PY_TODO

    assert check_scope([modified("m.py", old, old.replace("  # TODO", ""))], [V("m.py", 5)]).ok


# --- putting a dropped path back (cm-9ab) ----------------------



def test_a_restore_never_writes_through_a_hard_link_to_a_file_outside(tmp_path):
    repo = git_repo(tmp_path, {"a.py": "base\n"})
    sandbox = sandbox_of(repo)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside, untouched\n", encoding="utf-8")
    (sandbox.path / "a.py").unlink()
    os.link(outside, sandbox.path / "a.py")

    _restore_from_base(sandbox, "a.py")

    assert outside.read_text(encoding="utf-8") == "outside, untouched\n"
    assert (sandbox.path / "a.py").read_text(encoding="utf-8") == "base\n"
    assert not os.path.samefile(outside, sandbox.path / "a.py")


def test_a_directory_where_the_file_was_is_a_scope_check_error_not_a_crash(tmp_path):
    repo = git_repo(tmp_path, {"d/f.py": "base\n"})
    sandbox = sandbox_of(repo)
    (sandbox.path / "d" / "f.py").unlink()
    (sandbox.path / "d" / "f.py").mkdir()
    (sandbox.path / "d" / "f.py" / "x").write_text("x", encoding="utf-8")

    with pytest.raises(ScopeCheckError, match="is a directory in the sandbox"):
        _restore_from_base(sandbox, "d/f.py")


def test_a_path_under_a_symlinked_directory_is_refused(tmp_path):
    repo = git_repo(tmp_path, {"d/f.py": "base\n"})
    sandbox = sandbox_of(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(sandbox.path / "d")
    (sandbox.path / "d").symlink_to(elsewhere)

    with pytest.raises(ScopeCheckError):
        _restore_from_base(sandbox, "d/f.py")
    assert list(elsewhere.iterdir()) == []


def test_a_symlink_in_the_base_is_put_back_as_one(tmp_path):
    repo = git_repo(tmp_path, {"target.py": "t\n"})
    (repo / "link.py").symlink_to("target.py")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "link"], cwd=repo, check=True)
    sandbox = sandbox_of(repo)
    (sandbox.path / "link.py").unlink()
    (sandbox.path / "link.py").write_text("edited\n", encoding="utf-8")

    _restore_from_base(sandbox, "link.py")

    assert os.readlink(sandbox.path / "link.py") == "target.py"


def test_a_restored_executable_keeps_its_executable_bit(tmp_path):
    repo = git_repo(tmp_path, {"run.sh": "echo\n"})
    (repo / "run.sh").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "exec"], cwd=repo, check=True)
    sandbox = sandbox_of(repo)
    (sandbox.path / "run.sh").write_text("edited\n", encoding="utf-8")

    _restore_from_base(sandbox, "run.sh")

    assert os.access(sandbox.path / "run.sh", os.X_OK)


def test_a_tracked_path_that_looks_like_pathspec_magic_is_put_back_not_deleted(tmp_path):
    """R5 edge-case review: `:colon` read as pathspec magic found no base
    entry, so the restore deleted the tracked file instead."""
    repo = git_repo(tmp_path, {":colon": "base\n"})
    sandbox = sandbox_of(repo)
    (sandbox.path / ":colon").write_text("edited\n", encoding="utf-8")

    _restore_from_base(sandbox, ":colon")

    assert (sandbox.path / ":colon").read_text(encoding="utf-8") == "base\n"


def test_an_added_path_naming_another_file_by_magic_is_removed_not_filled(tmp_path):
    """`:(top)a.py`, read as magic, resolved to the base's a.py, whose bytes
    were then written into the agent's new file."""
    repo = git_repo(tmp_path, {"a.py": "a\n"})
    sandbox = sandbox_of(repo)
    (sandbox.path / ":(top)a.py").write_text("new\n", encoding="utf-8")

    _restore_from_base(sandbox, ":(top)a.py")

    assert not (sandbox.path / ":(top)a.py").exists()


def test_a_git_failure_while_putting_back_is_a_scope_check_error(tmp_path, monkeypatch):
    from curamoir import fix_scope
    from curamoir.git_guard import GitGuardError

    repo = git_repo(tmp_path, {"a.py": "base\n"})
    sandbox = sandbox_of(repo)

    def failing(*_args, **_kwargs):
        raise GitGuardError("git ls-tree timed out")
    monkeypatch.setattr(fix_scope, "git", failing)

    with pytest.raises(ScopeCheckError, match="timed out"):
        _restore_from_base(sandbox, "a.py")


def test_a_path_a_remedy_deleted_is_put_back_deleted(tmp_path):
    """The fix step's snapshot holds the remedy's result, not the base."""
    repo = git_repo(tmp_path, {"gone.py": "base\n"})
    sandbox = sandbox_of(repo)
    (sandbox.path / "gone.py").write_text("recreated by the agent\n", encoding="utf-8")

    _restore_from_base(sandbox, "gone.py", deleted=True)

    assert not (sandbox.path / "gone.py").exists()


def test_a_refused_restore_leaves_the_agents_file_as_it_was(tmp_path):
    """The base's kind is checked before anything is unlinked."""
    repo = git_repo(tmp_path, {"d/f.py": "base\n"})
    sandbox = sandbox_of(repo)
    shutil.rmtree(sandbox.path / "d")
    (sandbox.path / "d").write_text("a file where the base has a directory\n",
                                    encoding="utf-8")

    with pytest.raises(ScopeCheckError):
        _restore_from_base(sandbox, "d")

    assert (sandbox.path / "d").read_text(encoding="utf-8").startswith("a file")


# --- drop_breaching_paths (cm-9ab) -----------------------------------------------------

#: A path the agent breached E3-R2 in, which a drop would put back.
_STRAY = Breach(FILE_NOT_IN_WORK_LIST, "a.py", None,
                "changed, but the work list has no violation in this file")


@pytest.mark.parametrize("kind", [GIT_INTERNALS_CHANGED, BASE_MOVED, HIDDEN_CHANGE])
def test_a_whole_run_breach_drops_nothing_and_returns_the_verdict_as_it_is(tmp_path, kind):
    """WHOLE_RUN_KINDS: the sandbox itself is not to be trusted, so no file
    of it is put back, not even one another breach names, and the review
    is not run again: the run is refused on the first verdict."""
    repo = git_repo(tmp_path, {"a.py": "base\n", "b.py": "base\n"})
    sandbox = sandbox_of(repo)
    state = snapshot_git_state(sandbox)
    (sandbox.path / "a.py").write_text("edited\n", encoding="utf-8")
    (sandbox.path / "b.py").write_text("edited\n", encoding="utf-8")
    first = ScopeVerdict(breaches=(_STRAY, Breach(kind, "b.py", None, "whole run")),
                         paths=("a.py", "b.py"))

    second, dropped = drop_breaching_paths(sandbox, first, git_state=state)

    assert second is first and dropped == ()
    assert (sandbox.path / "a.py").read_text(encoding="utf-8") == "edited\n"
    assert (sandbox.path / "b.py").read_text(encoding="utf-8") == "edited\n"


def test_a_path_a_remedy_deleted_is_dropped_back_to_deleted(tmp_path):
    """`removed` (the remedies' deletions) reaches the restore: the agent's
    re-creation of a file a remedy deleted is undone to the remedy's
    result, not to the base's file."""
    repo = git_repo(tmp_path, {"gone.py": "base\n", "a.py": "base\n"})
    sandbox = sandbox_of(repo)
    (sandbox.path / "gone.py").unlink()  # the remedy, before the fix step
    state = snapshot_git_state(sandbox)
    (sandbox.path / "gone.py").write_text("re-created by the agent\n", encoding="utf-8")
    first = verify_fix(sandbox, git_state=state)
    assert "gone.py" in {breach.path for breach in first.breaches}

    _second, dropped = drop_breaching_paths(sandbox, first, git_state=state,
                                            removed=("gone.py",))

    assert dropped == first.breaches
    assert not os.path.lexists(sandbox.path / "gone.py")


def test_breaching_paths_are_put_back_deepest_first(tmp_path, monkeypatch):
    """The order is observed directly. In the sandboxes where order might
    matter (a file `d` the agent made a directory holding `d/x.py`, and a
    directory `d/` the agent made a file `d`), both orders are refused, so
    only the calls show it."""
    from curamoir import fix_scope

    order = []
    monkeypatch.setattr(fix_scope, "_restore_from_base",
                        lambda _sandbox, path, deleted=False: order.append(path))
    monkeypatch.setattr(fix_scope, "verify_fix",
                        lambda *_args, **_kwargs: ScopeVerdict(breaches=(), paths=()))
    paths = ["z.py", "a.py", "a/b.py", "a/b/c.py", "a/b"]
    first = ScopeVerdict(breaches=tuple(Breach(FILE_NOT_IN_WORK_LIST, path, None, "x")
                                        for path in paths), paths=tuple(sorted(paths)))

    drop_breaching_paths(SimpleNamespace(), first, git_state=None)

    assert order == ["a/b/c.py", "a/b", "a/b.py", "a.py", "z.py"]
