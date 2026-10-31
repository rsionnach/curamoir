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

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Never modules: build artefacts, packaging metadata, docs, and vendored code.
# Two different questions, so two lists. NOT_SOURCE answers "is this code at
# all?"; NEVER_MODULES answers "should this code be graded?".

# Not source in any sense: generated, vendored, or tooling state.
NOT_SOURCE = (
    "build", "dist", "site-packages", "node_modules", "venv", ".venv",
    "__pycache__", ".git", ".curamoir", ".tox", ".mypy_cache", ".pytest_cache",
)

# Real source the sweep should still fix, but not GRADEABLE. DEC-2 item 3: a
# test suite graded as production code would skew every module's grade and spend
# review passes on it. `docs` is here rather than in NOT_SOURCE because a docs
# directory can legitimately contain Python (conf.py, examples).
NEVER_MODULES = (*NOT_SOURCE, "tests", "docs")
PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt")
TYPESCRIPT_MARKERS = ("tsconfig.json", "package.json")


class TemplateDrift(Exception):
    """The bundled config template no longer has the anchor init writes into."""


class AlreadyInitialised(Exception):
    """`.curamoir/` exists. The new config was written alongside, not over."""


class UnsafePath(Exception):
    """A directory init writes into, or a file it writes, is a symlink.

    Init writes beside files the developer owns (`curamoir.yaml.new`,
    `curamoir-sweep.yml.new`), and a repository can commit any of those
    paths as a symlink: followed, the write would land wherever the link
    points (cm-2qp.4 security review). So the directories are checked, and
    every file is opened with O_NOFOLLOW; nothing is written through a link.
    """


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
            and not _is_not_gradeable(child)
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


#: `.curamoir/` is meant to be committed — the config, the principles and the
#: rules are the repo's shared memory. Three things in it are not: the journal
#: holds verbatim session text, the lock holds a pid and a hostname, and the
#: pending config is a diff artefact. Writing this at init rather than
#: documenting it means the journal cannot be committed by accident on the first
#: session, which is the only session where nobody has thought about it yet.
GITIGNORE = """\
# Machine-local cúramóir state. Everything else here is meant to be committed.
handoff/journal.jsonl
state/lock
curamoir.yaml.new
# A writer killed mid-write leaves one of these; it is never content.
*.tmp
"""


def init_repo(repo, templates=None):
    """Create `.curamoir/`, writing a config that reflects what was detected.

    Raises AlreadyInitialised if `.curamoir/` is present, having first written
    the config it would have created to `curamoir.yaml.new`.
    """
    repo = Path(repo)
    templates = Path(templates) if templates else _bundled_templates()
    base = repo / ".curamoir"
    config_text = _render_config(repo, templates)

    if base.exists() or base.is_symlink():
        _refuse_symlinked(repo, base)
        pending = base / "curamoir.yaml.new"
        _write_no_follow(repo, pending, config_text)
        raise AlreadyInitialised(
            f"{base} already exists; nothing was overwritten. The config this run "
            f"would have written is at {pending.relative_to(repo)} — diff it against "
            "curamoir.yaml and merge by hand if you want the changes."
        )

    for directory in ("rules", "state", "handoff/history", "briefs"):
        (base / directory).mkdir(parents=True, exist_ok=True)

    (base / "curamoir.yaml").write_text(config_text, encoding="utf-8")
    _copy_template(templates / "principles.md", base / "principles.md")
    (base / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    # PAUSE and state/lock are deliberately NOT created: a scaffolded PAUSE would
    # ship the repo paused, and a scaffolded lock would block the first job.
    return base


#: Where `init --github-workflow` writes the scheduled sweep (cm-2qp.4),
#: relative to the repository root, and its template under templates/.
WORKFLOW_PATH = (".github", "workflows", "curamoir-sweep.yml")
WORKFLOW_TEMPLATE = ("github", "curamoir-sweep.yml")


@dataclass(frozen=True)
class WorkflowWritten:
    """Where the workflow went: `path`, and `pending` when a workflow was
    already there and the template went beside it instead."""

    path: Path
    pending: Optional[Path] = None


def write_github_workflow(repo, templates=None):
    """Write the GitHub Actions sweep (templates/github/curamoir-sweep.yml)
    to `.github/workflows/curamoir-sweep.yml`: a WorkflowWritten.

    Only when the developer asked for it (`curamoir init --github-workflow`,
    which /curamoir:init runs on a yes): the workflow spends API money on
    a schedule, so it is never a default. Never over an existing file, for
    the reason init never overwrites the config: a hand-tuned workflow (its
    cron, its ref) must survive a habitual re-run. The template then goes
    to `curamoir-sweep.yml.new` beside it, which GitHub does not run (it
    reads only `.yml` and `.yaml` there), and `pending` says so.
    Independent of `.curamoir/`, so a repository initialised before the
    template existed can still ask for it.
    """
    repo = Path(repo)
    templates = Path(templates) if templates else _bundled_templates()
    text = templates.joinpath(*WORKFLOW_TEMPLATE).read_text(encoding="utf-8")
    path = repo.joinpath(*WORKFLOW_PATH)
    for depth in range(1, len(WORKFLOW_PATH)):
        directory = repo.joinpath(*WORKFLOW_PATH[:depth])
        _refuse_symlinked(repo, directory)
        directory.mkdir(exist_ok=True)
    if os.path.lexists(path):
        pending = path.with_name(path.name + ".new")
        _write_no_follow(repo, pending, text)
        return WorkflowWritten(path, pending)
    _write_no_follow(repo, path, text, exclusive=True)
    return WorkflowWritten(path)


def _refuse_symlinked(repo, directory):
    """UnsafePath when `directory` is a symlink (UnsafePath's docstring), or
    something other than a directory where init must make or use one."""
    if directory.is_symlink():
        raise UnsafePath(f"{directory.relative_to(repo)} is a symlink, so init writes "
                         "nothing into it; replace it with a real directory first.")
    if os.path.lexists(directory) and not directory.is_dir():
        raise UnsafePath(f"{directory.relative_to(repo)} is not a directory, so init "
                         "writes nothing into it; move it aside first.")


def _write_no_follow(repo, path, text, exclusive=False):
    """Write `text` to `path`, never through a symlink: O_NOFOLLOW refuses a
    link at `path` itself (UnsafePath). `exclusive` also refuses any file
    already there (O_EXCL), for a first write that must not overwrite."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError as error:
        if os.path.islink(path):
            raise UnsafePath(f"{path.relative_to(repo)} is a symlink, so init does not "
                             "write through it; remove it and run init again.") from None
        if os.path.isdir(path):
            raise UnsafePath(f"{path.relative_to(repo)} is a directory, so init cannot "
                             "write that file; remove it and run init again.") from None
        raise error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


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
        if not container.is_dir() or _is_not_gradeable(container):
            continue
        for child in sorted(container.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists() and not _is_not_gradeable(child):
                roots.append(child)
    return roots


def _is_not_gradeable(path):
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
        # This is the ONLY branch that runs today: `templates/principles.md`
        # does not exist yet (Epic 2 ships it). So the text has to stand on its
        # own — it is the first file a new user opens, and the previous version
        # sent them to `docs/principles-authoring.md` and an `examples/`
        # directory, neither of which exists.
        destination.write_text(
            "# Golden principles\n\n"
            "No principles yet. A starter set ships with Epic 2, which has not "
            "landed.\n\n"
            "Write your own: one principle per section, each with\n\n"
            "- an `id` a lint rule can cite (e.g. `no-bare-except`),\n"
            "- the rule as one imperative sentence,\n"
            "- why it matters in this repository,\n"
            "- a short example of a violation and its fix.\n\n"
            "`/curamoir:doctor` reports how many it can parse.\n",
            encoding="utf-8",
        )
