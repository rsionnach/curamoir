"""Where cúramóir finds its plugin's own files: hooks/hooks.json, templates/
and the scripts (cm-2qp.10).

The one place that answers it, for every module that used to work it out
for itself as `Path(__file__).resolve().parents[2]`: the fix step (the
denylist --settings), the sweep (the lint runner), scaffold (the templates)
and doctor (the hooks it probes). There are two layouts:

- **The plugin checkout**: what Claude Code loads as the plugin, what
  scripts/curamoir runs with `src/` on PYTHONPATH, and what an editable
  install (`uv run` here) imports. The package sits in `src/curamoir/`, and
  hooks/, templates/ and scripts/ sit beside `src/`. ROOT is the checkout,
  exactly the path each module computed before, so nothing on the local
  path moves.
- **An installed wheel**: `uvx --from git+https://github.com/rsionnach/curamoir
  curamoir`, which is how the Actions workflow and the Routine prompt run
  cúramóir in a fresh VM with no checkout of it. The wheel carries
  hooks/hooks.json and templates/ inside the package, at
  `curamoir/_plugin/` (pyproject's force-include), and ROOT is that
  directory. The scripts are not carried: they are bash launchers that put a
  checkout's `src/` on PYTHONPATH and pick a `python3` from PATH, neither of
  which an installed package has or needs. What the scripts do is reached
  another way there: the lint is the `curamoir-lint` console script beside
  the interpreter (LINT), and the denylist hook is the running interpreter
  with the hook's module (fix_step.pretooluse_settings).

Which layout is decided once (installed): an install is a package that
carries the copy and is not a checkout's `src/curamoir/`. A checkout never
carries one, since the copy is made by the wheel build and is not
committed (a test holds that); but a stray `src/curamoir/_plugin/` left
there by hand is still the checkout, because the package sits in `src/`
beside the checkout's own hooks/hooks.json, and reading a stale copy in
place of the checkout's would be the drift the rule exists to avoid.

Syntax that imports on 3.9, like sweep.py, which imports it: the hook and
CLI wrappers in a checkout may run an older `python3`.
"""

import sysconfig
from pathlib import Path

#: This package's own directory.
PACKAGE = Path(__file__).resolve().parent

#: Where a wheel puts its copy of the plugin's files (pyproject.toml,
#: `[tool.hatch.build.targets.wheel.force-include]`).
PACKAGED = PACKAGE / "_plugin"


def installed(package):
    """Whether `package` (a curamoir package directory) is an installed
    wheel rather than the plugin checkout's `src/curamoir/` (module
    docstring)."""
    checkout = package.parent.name == "src" and (
        package.parents[1] / "hooks" / "hooks.json").is_file()
    return (package / "_plugin").is_dir() and not checkout


#: Whether this is an installed wheel rather than the plugin checkout.
INSTALLED = installed(PACKAGE)

#: The directory hooks/ and templates/ are in: the packaged copy when
#: installed, else the checkout (src/curamoir/ is two levels under it).
ROOT = PACKAGED if INSTALLED else PACKAGE.parents[1]

#: The hook registrations Claude Code reads for the plugin.
HOOKS_JSON = ROOT / "hooks" / "hooks.json"

#: The files `curamoir init` scaffolds from.
TEMPLATES = ROOT / "templates"

#: The launcher scripts, in the checkout only: None when installed, since
#: a wheel carries none (doctor does not probe the hooks there).
SCRIPTS = None if INSTALLED else ROOT / "scripts"

#: The standalone lint runner the sweep calls as a separate process: the
#: checkout's scripts/curamoir-lint, which CI runs; installed, the
#: `curamoir-lint` console script in the environment's scripts directory
#: (the `bin/` of the venv uvx made). A console script starts Python with
#: its own directory, not the working directory, at sys.path[0], so the
#: linted repository is not importable as the linter, which is what the
#: checkout's wrapper `cd`s into `src/` to prevent.
LINT = (Path(sysconfig.get_path("scripts")) / "curamoir-lint" if INSTALLED
        else ROOT / "scripts" / "curamoir-lint")
