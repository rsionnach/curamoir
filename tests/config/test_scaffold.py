"""E1-R2: `/curamoir:init` scaffolds `.curamoir/` and detects the repo's shape.

Module detection is the consequential half. DEC-2 rejected `top-level-dirs` as
the default on evidence: all three NthLayer code repos are src-layout, where it
yields exactly one code module named `src`, leaving the review selector nothing
to choose between and flattening per-module back-off.
"""

import pytest

from curamoir.scaffold import (
    AlreadyInitialised, detect_languages, detect_modules, init_repo,
)


def make_repo(tmp_path, files):
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


# --- module detection (DEC-2) ---------------------------------------------

def test_a_src_layout_repo_yields_its_subpackages_not_the_word_src(tmp_path):
    """The exact failure DEC-2 was decided on."""
    repo = make_repo(tmp_path, {
        "pyproject.toml": "",
        "src/myapp/__init__.py": "",
        "src/myapp/telemetry/__init__.py": "",
        "src/myapp/registry/__init__.py": "",
        "src/myapp/cli.py": "",
        "tests/test_cli.py": "",
    })

    modules = detect_modules(repo)

    assert "src" not in modules
    assert "src/myapp/telemetry" in modules
    assert "src/myapp/registry" in modules


def test_loose_top_level_modules_are_grouped_rather_than_lost(tmp_path):
    """cli.py sits beside the subpackages and still needs grading."""
    repo = make_repo(tmp_path, {
        "pyproject.toml": "",
        "src/myapp/__init__.py": "",
        "src/myapp/telemetry/__init__.py": "",
        "src/myapp/cli.py": "",
    })

    modules = detect_modules(repo)

    assert "src/myapp" in modules, f"loose top-level modules dropped: {modules}"


def test_a_flat_layout_repo_is_detected_too(tmp_path):
    repo = make_repo(tmp_path, {
        "setup.py": "",
        "myapp/__init__.py": "",
        "myapp/core/__init__.py": "",
        "myapp/web/__init__.py": "",
    })

    modules = detect_modules(repo)

    assert "myapp/core" in modules
    assert "myapp/web" in modules


def test_build_artefacts_and_docs_are_never_modules(tmp_path):
    repo = make_repo(tmp_path, {
        "pyproject.toml": "",
        "src/myapp/__init__.py": "",
        "src/myapp/core/__init__.py": "",
        "build/lib/myapp/__init__.py": "",
        "src/myapp.egg-info/PKG-INFO": "",
        "docs/conf.py": "",
    })

    modules = detect_modules(repo)

    for noise in modules:
        assert "build" not in noise
        assert "egg-info" not in noise
        assert not noise.startswith("docs")


def test_a_single_flat_package_is_itself_a_module(tmp_path):
    """A package with no subpackages must still be gradeable. Returning nothing
    here would mean a small repo is never graded or reviewed at all — worse than
    the coarse granularity DEC-2 rejected."""
    repo = make_repo(tmp_path, {"pyproject.toml": "", "src/tiny/__init__.py": ""})

    modules = detect_modules(repo)

    assert modules == ["src/tiny"]


def test_a_repo_with_too_few_modules_still_yields_at_least_one(tmp_path):
    """DEC-2: with fewer than three modules the selector degrades to round-robin.
    Degraded, not empty."""
    repo = make_repo(tmp_path, {"pyproject.toml": "", "src/tiny/__init__.py": ""})

    modules = detect_modules(repo)

    assert 1 <= len(modules) < 3


# --- language detection ---------------------------------------------------

def test_python_is_detected_from_its_markers(tmp_path):
    repo = make_repo(tmp_path, {"pyproject.toml": "", "src/a/__init__.py": ""})

    assert "python" in detect_languages(repo)


def test_typescript_is_detected_from_its_markers(tmp_path):
    repo = make_repo(tmp_path, {"package.json": "{}", "tsconfig.json": "{}", "src/a.ts": ""})

    assert "typescript" in detect_languages(repo)


def test_a_polyglot_repo_reports_both(tmp_path):
    repo = make_repo(tmp_path, {
        "pyproject.toml": "", "src/a/__init__.py": "",
        "package.json": "{}", "web/app.ts": "",
    })

    languages = detect_languages(repo)

    assert {"python", "typescript"} <= set(languages)


# --- scaffolding ----------------------------------------------------------

def test_init_creates_the_documented_state_tree(tmp_path):
    repo = make_repo(tmp_path, {"pyproject.toml": "", "src/a/__init__.py": ""})

    init_repo(repo)

    base = repo / ".curamoir"
    assert (base / "curamoir.yaml").is_file()
    assert (base / "principles.md").is_file()
    assert (base / "rules").is_dir()
    assert (base / "state").is_dir()
    assert (base / "handoff" / "history").is_dir()
    assert (base / "briefs").is_dir()


def test_init_does_not_create_the_lock_or_the_pause_file(tmp_path):
    """Both are created by their owners at runtime. A scaffolded PAUSE would
    ship the repo paused; a scaffolded lock would block the first job."""
    repo = make_repo(tmp_path, {"pyproject.toml": ""})

    init_repo(repo)

    assert not (repo / ".curamoir" / "PAUSE").exists()
    assert not (repo / ".curamoir" / "state" / "lock").exists()


def test_the_written_config_validates_and_keeps_its_comments(tmp_path):
    from curamoir.config import load_config_text

    repo = make_repo(tmp_path, {"pyproject.toml": "", "src/a/__init__.py": ""})
    init_repo(repo)

    text = (repo / ".curamoir" / "curamoir.yaml").read_text(encoding="utf-8")

    assert text.count("#") >= 20, "commented defaults required by E1-R2"
    assert load_config_text(text)["version"] == 1


def test_detected_modules_are_written_into_the_config(tmp_path):
    from curamoir.config import load_config_text

    repo = make_repo(tmp_path, {
        "pyproject.toml": "",
        "src/myapp/__init__.py": "",
        "src/myapp/core/__init__.py": "",
    })

    init_repo(repo)

    config = load_config_text((repo / ".curamoir" / "curamoir.yaml").read_text())
    assert "src/myapp/core" in config["modules"]["include"]


def test_re_running_init_refuses_and_writes_a_dot_new_file(tmp_path):
    """E1-R2: it MUST NOT overwrite an existing .curamoir/; it writes
    .curamoir/curamoir.yaml.new and says so."""
    repo = make_repo(tmp_path, {"pyproject.toml": "", "src/a/__init__.py": ""})
    init_repo(repo)
    (repo / ".curamoir" / "curamoir.yaml").write_text("# hand-edited\nversion: 1\n")

    with pytest.raises(AlreadyInitialised) as caught:
        init_repo(repo)

    assert (repo / ".curamoir" / "curamoir.yaml.new").is_file()
    assert "curamoir.yaml.new" in str(caught.value)
    assert (repo / ".curamoir" / "curamoir.yaml").read_text().startswith("# hand-edited")


def test_init_never_touches_anything_outside_the_curamoir_directory(tmp_path):
    repo = make_repo(tmp_path, {"pyproject.toml": "x", "src/a/__init__.py": "y"})
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}

    init_repo(repo)

    for path, content in before.items():
        assert path.read_bytes() == content, f"init modified {path}"


def test_the_test_suite_is_never_a_gradeable_module(tmp_path):
    """DEC-2 item 3 says always exclude `tests/`. It was not excluded, so a
    flat-layout repo detected `tests/unit` as a module and init wrote it into
    modules.include — grading and review selection would then treat the test
    suite as production code."""
    repo = make_repo(tmp_path, {
        "setup.py": "",
        "myapp/__init__.py": "",
        "myapp/core/__init__.py": "",
        "tests/__init__.py": "",
        "tests/unit/__init__.py": "",
    })

    modules = detect_modules(repo)

    assert "myapp/core" in modules
    assert not any(module.startswith("tests") for module in modules), modules


def test_a_docs_heavy_repo_yields_no_prose_modules(tmp_path):
    """DEC-2's named counter-example: opensrm, whose 12 top-level dirs are mostly
    prose and assets. It had no test."""
    repo = make_repo(tmp_path, {
        "README.md": "",
        "articles/one.md": "", "assets/logo.png": "", "diagrams/a.svg": "",
        "docs/index.md": "", "plans/next.md": "", "examples/demo.md": "",
        "scripts/build.sh": "",
    })

    modules = detect_modules(repo)

    assert modules == [], modules


def test_a_legitimate_domain_module_named_fixtures_is_not_dropped(tmp_path):
    """`fixtures` and `test` were added to NEVER_MODULES and matched as exact path
    parts anywhere, so a real domain module called `fixtures` (sports, test-data
    services) vanished silently. Excluding the test SUITE is not the same as
    banning a noun."""
    repo = make_repo(tmp_path, {
        "pyproject.toml": "",
        "src/app/__init__.py": "",
        "src/app/fixtures/__init__.py": "",
        "src/app/billing/__init__.py": "",
    })

    modules = detect_modules(repo)

    assert "src/app/fixtures" in modules, modules
    assert "src/app/billing" in modules


def test_language_detection_still_sees_a_test_only_python_tree(tmp_path):
    """Excluding `tests/` from GRADING must not make the repo look like it has no
    Python at all — doctor would then report 'languages: none detected'."""
    repo = make_repo(tmp_path, {"tests/test_app.py": "def test_x(): pass\n"})

    assert "python" in detect_languages(repo)


def test_template_drift_is_detected_even_when_no_modules_are_found(tmp_path):
    """The drift check sat behind the no-modules early return, so a repo with
    nothing detected never noticed — the fault would surface on some later repo
    instead, far from its cause."""
    from curamoir.scaffold import TemplateDrift, init_repo

    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "curamoir.yaml").write_text("version: 1\nmodules:\n  include: [ ]\n", encoding="utf-8")
    repo = make_repo(tmp_path / "repo", {"README.md": "no code here"})

    with pytest.raises(TemplateDrift):
        init_repo(repo, templates=templates)
