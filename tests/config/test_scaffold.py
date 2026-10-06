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
