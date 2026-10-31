"""cm-2qp.4 (E14.4): `/curamoir:init` offers the GitHub Actions sweep, and
writes it only when asked (`curamoir init --github-workflow`), never over
an existing file (scaffold.write_github_workflow).
"""

from pathlib import Path

from curamoir import scaffold
from curamoir.cli import main

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "github" / "curamoir-sweep.yml"
WORKFLOW = Path(".github", "workflows", "curamoir-sweep.yml")

#: `curamoir init`'s output at cm-2qp.3 (f5bfa3d) for a one-package repo,
#: with the repository's path as REPO: the flag must not change it.
DEFAULT_OUTPUT = """\
Scaffolded REPO/.curamoir
  languages: python
  modules (1):
    - src/app
  note: fewer than three modules, so review selection will round-robin rather than choose by grade.

Next: review .curamoir/curamoir.yaml, then run /curamoir:doctor.
"""


def one_package_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "src" / "app" / "__init__.py").write_text("", encoding="utf-8")
    return repo


def init(repo, capsys, *flags):
    code = main(["init", "--repo", str(repo), *flags])
    return code, capsys.readouterr().out.replace(str(repo), "REPO")


def test_the_default_init_writes_no_workflow_and_its_output_is_unchanged(tmp_path, capsys):
    repo = one_package_repo(tmp_path)

    code, out = init(repo, capsys)

    assert code == 0
    assert out == DEFAULT_OUTPUT
    assert not (repo / ".github").exists()


def test_init_writes_the_workflow_from_the_template_when_asked(tmp_path, capsys):
    repo = one_package_repo(tmp_path)

    code, out = init(repo, capsys, "--github-workflow")

    assert code == 0
    assert (repo / WORKFLOW).read_text(encoding="utf-8") == TEMPLATE.read_text(
        encoding="utf-8")
    assert "workflow: wrote .github/workflows/curamoir-sweep.yml" in out
    assert "ANTHROPIC_API_KEY" in out
    # The rest of the report is the default one.
    assert out.startswith(DEFAULT_OUTPUT.split("\nNext:")[0])
    assert out.endswith("Next: review .curamoir/curamoir.yaml, then run /curamoir:doctor.\n")


def test_an_existing_workflow_is_never_overwritten(tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    (repo / WORKFLOW).parent.mkdir(parents=True)
    (repo / WORKFLOW).write_text("# mine, hand-tuned\n", encoding="utf-8")

    code, out = init(repo, capsys, "--github-workflow")

    assert code == 0
    assert (repo / WORKFLOW).read_text(encoding="utf-8") == "# mine, hand-tuned\n"
    pending = repo / WORKFLOW.with_name("curamoir-sweep.yml.new")
    assert pending.read_text(encoding="utf-8") == TEMPLATE.read_text(encoding="utf-8")
    assert "already exists; nothing was overwritten" in out
    assert "curamoir-sweep.yml.new" in out


def test_an_already_initialised_repo_can_still_ask_for_the_workflow(tmp_path, capsys):
    """Exit 0 when the workflow was written: it is what was asked for, and
    the slash command reports a non-zero exit as a failure. The "already
    initialised" note is still printed (edge cases review)."""
    repo = one_package_repo(tmp_path)
    init(repo, capsys)

    code, out = init(repo, capsys, "--github-workflow")

    assert code == 0
    assert "already initialised" in out
    assert "workflow: wrote .github/workflows/curamoir-sweep.yml" in out
    assert (repo / WORKFLOW).is_file()


def test_an_already_initialised_repo_with_a_workflow_gets_a_new_beside_it_and_exit_0(
        tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    init(repo, capsys, "--github-workflow")

    code, out = init(repo, capsys, "--github-workflow")

    assert code == 0
    assert "already initialised" in out and "nothing was overwritten" in out


def test_an_already_initialised_repo_whose_workflow_is_refused_exits_1(tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    init(repo, capsys)
    (repo / ".github").write_text("not a directory\n", encoding="utf-8")

    code, out = init(repo, capsys, "--github-workflow")

    assert code == 1
    assert "already initialised" in out and ".github is not a directory" in out


def test_an_already_initialised_repo_without_the_flag_still_exits_1(tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    init(repo, capsys)

    code, out = init(repo, capsys)

    assert code == 1
    assert "already initialised" in out


def test_write_github_workflow_reports_where_it_went(tmp_path):
    written = scaffold.write_github_workflow(tmp_path)
    again = scaffold.write_github_workflow(tmp_path)

    assert written == scaffold.WorkflowWritten(tmp_path / WORKFLOW)
    assert again.path == tmp_path / WORKFLOW
    assert again.pending == tmp_path / WORKFLOW.with_name("curamoir-sweep.yml.new")


def test_the_init_command_asks_before_writing_the_workflow():
    body = (ROOT / "commands" / "init.md").read_text(encoding="utf-8")

    assert "--github-workflow" in body
    assert "ANTHROPIC_API_KEY" in body
    assert "ask" in body.lower()
    assert "/curamoir:routine" in body
    assert "pin a cúramóir tag or\n  commit SHA" in body
    assert "branch name is\n  accepted but discouraged" in body
    assert "Nothing runs on a schedule until they set that up." not in body


# --- never through a symlink (cm-2qp.4 security review) ------------------------------

def test_a_symlinked_github_directory_is_refused_and_nothing_is_written(tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / ".github").symlink_to(elsewhere)

    code, out = init(repo, capsys, "--github-workflow")

    assert code == 1
    assert ".github is a symlink" in out
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_workflows_directory_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows").symlink_to(elsewhere)

    try:
        scaffold.write_github_workflow(tmp_path)
    except scaffold.UnsafePath as unsafe:
        assert "workflows is a symlink" in str(unsafe)
    else:
        raise AssertionError("a symlinked .github/workflows was written through")
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_workflow_new_is_refused_and_its_target_untouched(tmp_path):
    (tmp_path / WORKFLOW).parent.mkdir(parents=True)
    (tmp_path / WORKFLOW).write_text("# mine\n", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("precious\n", encoding="utf-8")
    (tmp_path / WORKFLOW.with_name("curamoir-sweep.yml.new")).symlink_to(victim)

    try:
        scaffold.write_github_workflow(tmp_path)
    except scaffold.UnsafePath as unsafe:
        assert "curamoir-sweep.yml.new is a symlink" in str(unsafe)
    else:
        raise AssertionError("a symlinked .new was written through")
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_a_symlinked_workflow_itself_gets_a_new_beside_it_never_through_it(tmp_path):
    (tmp_path / WORKFLOW).parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("precious\n", encoding="utf-8")
    (tmp_path / WORKFLOW).symlink_to(victim)

    written = scaffold.write_github_workflow(tmp_path)

    assert written.pending is not None
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_a_symlinked_config_new_is_refused_and_its_target_untouched(tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    init(repo, capsys)
    victim = tmp_path / "victim"
    victim.write_text("precious\n", encoding="utf-8")
    (repo / ".curamoir" / "curamoir.yaml.new").symlink_to(victim)

    code, out = init(repo, capsys)

    assert code == 1
    assert "curamoir.yaml.new is a symlink" in out
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_a_symlinked_curamoir_directory_is_refused(tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / ".curamoir").symlink_to(elsewhere)

    code, out = init(repo, capsys)

    assert code == 1
    assert ".curamoir is a symlink" in out
    assert list(elsewhere.iterdir()) == []


def test_a_regular_file_where_a_directory_belongs_is_refused_plainly(tmp_path, capsys):
    repo = one_package_repo(tmp_path)
    (repo / ".github").write_text("not a directory\n", encoding="utf-8")

    code, out = init(repo, capsys, "--github-workflow")

    assert code == 1
    assert ".github is not a directory" in out
    assert (repo / ".github").read_text(encoding="utf-8") == "not a directory\n"


def test_a_regular_file_at_workflows_is_refused(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows").write_text("x\n", encoding="utf-8")

    try:
        scaffold.write_github_workflow(tmp_path)
    except scaffold.UnsafePath as unsafe:
        assert "workflows is not a directory" in str(unsafe)
    else:
        raise AssertionError("a file at .github/workflows was not refused")


def test_a_directory_at_the_new_path_is_refused_plainly(tmp_path):
    (tmp_path / WORKFLOW).parent.mkdir(parents=True)
    (tmp_path / WORKFLOW).write_text("# mine\n", encoding="utf-8")
    (tmp_path / WORKFLOW.with_name("curamoir-sweep.yml.new")).mkdir()

    try:
        scaffold.write_github_workflow(tmp_path)
    except scaffold.UnsafePath as unsafe:
        assert "curamoir-sweep.yml.new is a directory" in str(unsafe)
    else:
        raise AssertionError("a directory at the .new path was not refused")


def test_the_workflow_report_names_the_ref_variable_and_the_key(tmp_path, capsys):
    repo = one_package_repo(tmp_path)

    _, out = init(repo, capsys, "--github-workflow")

    assert "CURAMOIR_REF repository variable (a cúramóir tag or commit)" in out
    assert "ANTHROPIC_API_KEY" in out
