"""E1-R2: scaffold `.curamoir/` in a target repo, and detect the repo's shape.

Module detection is the part that matters. Modules are the unit of grading
(E7-R1) and of review selection (E5-R3), so wrong granularity does not merely
inconvenience those epics — it makes them meaningless. DEC-2 settled the default
on evidence: `top-level-dirs` collapses a src-layout repo to one module named
`src`, which leaves a selector configured for two-plus-one nothing to choose
between and flattens per-module back-off, the main cost control in Epic 5.

Init never overwrites. A developer who has hand-tuned `.curamoir/curamoir.yaml`
and re-runs init by habit must not lose it, so a second run writes
`curamoir.yaml.new` beside it and says so.
"""

from pathlib import Path

# Never modules: build artefacts, packaging metadata, docs, and vendored code.
# Two different questions, so two different lists. Conflating them dropped a
# legitimate domain module called `fixtures` and made a test-only tree look like
# it contained no Python at all.

# Not source code in any sense: generated, vendored, or tooling state.
NOT_SOURCE = (
    "build", "dist", "site-packages", "node_modules", "venv", ".venv",
    "__pycache__", ".git", ".curamoir", ".tox", ".mypy_cache", ".pytest_cache",
)

# Additionally not GRADEABLE, though still real source the sweep should fix.
# DEC-2 item 3: a test suite graded as production code would skew every module's
# grade and spend review passes on it. `docs` is here rather than in NOT_SOURCE
# because a docs directory can legitimately contain Python (conf.py, examples).
NEVER_MODULES = NOT_SOURCE + ("tests", "docs")
PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt")
TYPESCRIPT_MARKERS = ("tsconfig.json", "package.json")


class TemplateDrift(Exception):
    """The bundled config template no longer has the anchor init writes into."""


class AlreadyInitialised(Exception):
    """`.curamoir/` exists. The new config was written alongside, not over."""


def detect_languages(repo):
    """Languages present, by marker file and then by source extension."""
    repo = Path(repo)
    languages = []

    if any((repo / marker).exists() for marker in PYTHON_MARKERS) or _has_suffix(repo, ".py"):
        languages.append("python")
    if any((repo / marker).exists() for marker in TYPESCRIPT_MARKERS) or _has_suffix(repo, ".ts"):
        languages.append("typescript")
    return languages


def detect_modules(repo):
    """Gradeable modules, newest-convention-first.

    Strategy `auto`: find the importable package root, handling both `src/<pkg>/`
    and flat `<pkg>/` layouts, and take that package's immediate subpackages.
    Loose top-level modules beside them are grouped under the package itself, so
    a `cli.py` sitting next to subpackages is still graded rather than dropped.
    """
    repo = Path(repo)
    modules = []

    for package in _package_roots(repo):
        subpackages = [
            child for child in sorted(package.iterdir())
            if child.is_dir() and (child / "__init__.py").exists()
            and not _is_noise(child)
        ]
        modules.extend(str(child.relative_to(repo)) for child in subpackages)

        # The package itself is a module when it holds loose .py files beside its
        # subpackages, and ALSO when it has no subpackages at all — otherwise a
        # single flat package yields zero modules and nothing is ever graded,
        # which is worse than coarse granularity.
        loose = [
            child for child in package.iterdir()
            if child.is_file() and child.suffix == ".py" and child.name != "__init__.py"
        ]
        if loose or not subpackages:
            modules.append(str(package.relative_to(repo)))

    return modules


def init_repo(repo, templates=None):
    """Create `.curamoir/`, writing a config that reflects what was detected.

    Raises AlreadyInitialised if `.curamoir/` is present, having first written
    the config it would have created to `curamoir.yaml.new`.
    """
    repo = Path(repo)
    templates = Path(templates) if templates else _bundled_templates()
    base = repo / ".curamoir"
    config_text = _render_config(repo, templates)

    if base.exists():
        pending = base / "curamoir.yaml.new"
        pending.write_text(config_text, encoding="utf-8")
        raise AlreadyInitialised(
            f"{base} already exists; nothing was overwritten. The config this run "
            f"would have written is at {pending.relative_to(repo)} — diff it against "
            "curamoir.yaml and merge by hand if you want the changes."
        )

    for directory in ("rules", "state", "handoff/history", "briefs"):
        (base / directory).mkdir(parents=True, exist_ok=True)

    (base / "curamoir.yaml").write_text(config_text, encoding="utf-8")
    _copy_template(templates / "principles.md", base / "principles.md")
    # PAUSE and state/lock are deliberately NOT created: a scaffolded PAUSE would
    # ship the repo paused, and a scaffolded lock would block the first job.
    return base


def _render_config(repo, templates):
    """The template with detected modules substituted into `modules.include`.

    Edited as text rather than parsed and re-dumped, because E1-R2 requires
    "commented defaults" and a YAML round-trip would discard every comment.
    """
    text = (templates / "curamoir.yaml").read_text(encoding="utf-8")
    sentinel = "  include: []"
    # Checked BEFORE the no-modules early return: a repo with nothing detected
    # would otherwise never notice that the template had drifted, so the drift
    # would only surface on some later repo.
    if sentinel not in text:
        # Loudly, not silently: a reformatted template would otherwise ship
        # include: [] and leave grading with nothing to grade.
        raise TemplateDrift(
            f"templates/curamoir.yaml no longer contains {sentinel!r}, so detected "
            "modules cannot be written into it. Restore the line or update "
            "scaffold._render_config."
        )

    modules = detect_modules(repo)
    if not modules:
        return text

    rendered = "\n".join(f"    - {module}" for module in modules)
    return text.replace(sentinel, f"  include:\n{rendered}", 1)


def _package_roots(repo):
    """Importable package directories, preferring a `src/` layout."""
    containers = [repo / "src"] if (repo / "src").is_dir() else [repo]
    roots = []
    for container in containers:
        if not container.is_dir() or _is_noise(container):
            continue
        for child in sorted(container.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists() and not _is_noise(child):
                roots.append(child)
    return roots


def _is_noise(path):
    """Whether `path` is excluded from being a gradeable module."""
    name = path.name
    return (
        name in NEVER_MODULES
        or name.endswith(".egg-info")
        or name.startswith(".")
    )


def _has_suffix(repo, suffix):
    """Whether the repo contains real source with this extension.

    Uses NOT_SOURCE, not NEVER_MODULES: `tests/` is excluded from grading but a
    test-only tree is still a Python repo, and saying otherwise made doctor
    report "languages: none detected".
    """
    for path in repo.rglob(f"*{suffix}"):
        if not any(part in NOT_SOURCE for part in path.parts):
            return True
    return False


def _bundled_templates():
    return Path(__file__).resolve().parents[2] / "templates"


def _copy_template(source, destination):
    if source.exists():
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        destination.write_text(
            "# Golden principles\n\n"
            "No principles yet. See docs/principles-authoring.md, or copy a "
            "starter set from the plugin's examples/ directory.\n",
            encoding="utf-8",
        )
