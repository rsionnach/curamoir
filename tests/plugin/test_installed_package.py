"""cm-2qp.10: `uvx --from git+https://github.com/rsionnach/curamoir curamoir`
works in a fresh VM.

What uvx does with that is: build this project's wheel, install it with its
dependencies (PyYAML) into a venv of its own, and run the `curamoir` console
script. These tests do the same without the network: build the wheel from
this checkout (`uv build --offline`), install it into a fresh `uv venv`
(PyYAML from the local uv cache), and run everything from a temporary
working directory with no PYTHONPATH, so the source tree is not reachable:
`curamoir.__file__` must be in the venv. Then: the console scripts run;
the packaged hooks.json and templates are found (`curamoir init` scaffolds
the templates byte for byte, and writes the workflow); `curamoir run brief`
runs; the work list runs the lint as its own process (the `curamoir-lint`
console script); and the denylist --settings the installed package writes
for the fix step name a command that exists and denies `rm -rf build`, run
through a shell as Claude Code runs a command hook (tests/test_job_env.py
does the same from the checkout).

Skipped, with the reason, only when uv is absent or uv says the build
backend or PyYAML is not in its offline cache (OFFLINE_CACHE_MISS): no
install is no evidence either way. Any other build or install failure (a
force-include of a path that is gone, a dependency that cannot be
resolved) is a broken package and fails; and with `CI` set nothing skips,
since there the tools are meant to be present.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: uv's words for "the network is off and the cache lacks it" (uv 0.9:
#: "... was not found in the cache", "the network was disabled").
OFFLINE_CACHE_MISS = re.compile(r"not found in the cache|the network was disabled")


def _skip_or_fail(what, stderr):
    """Skip on a genuine offline cache miss outside CI; fail otherwise."""
    if OFFLINE_CACHE_MISS.search(stderr) and not os.environ.get("CI"):
        pytest.skip(f"{what} not possible offline from the uv cache: {stderr[-500:]}")
    pytest.fail(f"{what} failed: {stderr[-2000:]}")


def _clean_environment():
    """The test's environment without anything that points Python or uv at
    this checkout or its dev venv."""
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("PYTHON", "VIRTUAL_ENV", "UV_", "CURAMOIR_"))}
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


@pytest.fixture(scope="module")
def installed_venv(tmp_path_factory):
    """(bin directory, working directory) of a fresh venv with the wheel
    built from this checkout installed in it."""
    uv = shutil.which("uv")
    if uv is None:
        if os.environ.get("CI"):
            pytest.fail("uv is not installed in CI, so the wheel cannot be built")
        pytest.skip("uv is not installed, so the wheel cannot be built and installed")
    base = tmp_path_factory.mktemp("installed")
    environment = _clean_environment()

    def step(*argv):
        return subprocess.run([uv, *argv], capture_output=True, text=True, env=environment,
                              cwd=base, timeout=300)

    built = step("build", "--wheel", "--offline", "--out-dir", str(base / "dist"), str(ROOT))
    if built.returncode != 0:
        _skip_or_fail("building the wheel", built.stderr)
    (wheel,) = (base / "dist").glob("curamoir-*.whl")
    venv = base / "venv"
    made = step("venv", "--offline", "--quiet", str(venv))
    assert made.returncode == 0, made.stderr
    added = step("pip", "install", "--offline", "--quiet", "--python",
                 str(venv / "bin" / "python"), str(wheel))
    if added.returncode != 0:
        _skip_or_fail("installing the wheel and PyYAML", added.stderr)
    work = base / "work"
    work.mkdir()
    return venv / "bin", work


def run(installed_venv, *argv, cwd=None, **kwargs):
    bin_directory, work = installed_venv
    return subprocess.run([str(bin_directory / argv[0]), *argv[1:]], capture_output=True,
                          text=True, env=_clean_environment(), cwd=cwd or work, timeout=120,
                          **kwargs)


def python(installed_venv, code):
    """Run `code` in the installed interpreter, isolated (-I), from the work
    directory; its stdout parsed as JSON."""
    ran = run(installed_venv, "python", "-I", "-c", code)
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


@pytest.fixture
def repo(installed_venv, tmp_path):
    """A fresh git repository, initialised by the installed `curamoir init`."""
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ran = run(installed_venv, "curamoir", "init", "--repo", str(path), "--github-workflow")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return path


def test_the_console_scripts_run_with_no_source_tree(installed_venv):
    for script in ("curamoir", "curamoir-lint"):
        ran = run(installed_venv, script, "--help")
        assert ran.returncode == 0, ran.stderr
        assert "usage:" in ran.stdout


def test_curamoir_is_imported_from_the_venv_and_reads_its_packaged_files(installed_venv):
    bin_directory, _work = installed_venv
    venv = bin_directory.parent.resolve()
    seen = python(installed_venv, (
        "import json, curamoir, curamoir.plugin_files as p, curamoir.sweep as s,"
        " curamoir.fix_step as f;"
        "print(json.dumps({'file': curamoir.__file__, 'installed': p.INSTALLED,"
        " 'root': str(p.ROOT), 'hooks': p.HOOKS_JSON.is_file(),"
        " 'templates': sorted(x.name for x in p.TEMPLATES.iterdir()),"
        " 'lint': str(s.LINT), 'plugin_root': str(f.PLUGIN_ROOT)}))"))

    assert Path(seen["file"]).resolve().is_relative_to(venv)
    assert not Path(seen["file"]).resolve().is_relative_to(ROOT)
    assert seen["installed"] is True
    assert Path(seen["root"]).is_relative_to(venv) and seen["plugin_root"] == seen["root"]
    assert seen["hooks"]
    assert seen["templates"] == sorted(p.name for p in (ROOT / "templates").iterdir())
    assert Path(seen["lint"]).resolve() == (bin_directory / "curamoir-lint").resolve()


def test_init_scaffolds_the_packaged_templates(repo):
    """Byte for byte the checkout's templates: the packaged copy is found,
    not scaffold's built-in fallback text."""
    assert ((repo / ".curamoir" / "principles.md").read_bytes()
            == (ROOT / "templates" / "principles.md").read_bytes())
    assert ((repo / ".github" / "workflows" / "curamoir-sweep.yml").read_bytes()
            == (ROOT / "templates" / "github" / "curamoir-sweep.yml").read_bytes())


def test_run_brief_runs(installed_venv, repo):
    ran = run(installed_venv, "curamoir", "run", "brief", "--repo", str(repo))

    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "ran 'brief': success" in ran.stdout
    assert (repo / ".curamoir" / "briefs" / "latest.md").is_file()


def test_doctor_says_the_hooks_belong_to_the_plugin_not_that_they_are_missing(
        installed_venv, repo):
    seen = python(installed_venv, (
        "import json; from curamoir.doctor import _check_hooks;"
        f"print(json.dumps([[f.level, f.detail] for f in _check_hooks({str(repo)!r})]))"))
    ran = run(installed_venv, "curamoir", "doctor", "--repo", str(repo))

    assert seen == [["ok", "not probed: this cúramóir is an installed package, and the "
                           "hooks are registered by the Claude Code plugin, not by it"]]
    assert "Reinstall the plugin" not in ran.stdout
    assert "the hooks are registered by the Claude Code plugin" in " ".join(ran.stdout.split())


def test_the_work_list_runs_the_lint_as_its_own_process(installed_venv, repo):
    """sweep.work_list starts sweep.LINT; installed, that is the
    `curamoir-lint` console script. A report it could not get would be a
    LintUntrusted, exit 2."""
    ran = run(installed_venv, "curamoir", "sweep-work-list", "--repo", str(repo))

    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert json.loads(ran.stdout)["outcome"] == "no_work"


def test_the_installed_denylist_settings_deny_rm_rf_inside_a_job(installed_venv, repo):
    """The fix step's --settings from the installed package, each PreToolUse
    command run through /bin/sh with the repository as its working
    directory and CURAMOIR_JOB set, as the fix step's `claude` would: the
    command names the venv's interpreter (it exists, and is not the
    checkout's launcher), and it denies."""
    bin_directory, _work = installed_venv
    settings = python(installed_venv, "import json, curamoir.fix_step as f; "
                                      "print(json.dumps(f.pretooluse_settings()))")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "rm -rf build/"}, "cwd": str(repo)}

    (entry,) = settings["hooks"]["PreToolUse"]
    assert set(settings["hooks"]) == {"PreToolUse"} and entry["matcher"] == "Bash"
    (hook,) = entry["hooks"]
    argv = shlex.split(hook["command"])
    assert argv[1:] == ["-I", "-m", "curamoir.hooks.pretooluse"]
    assert Path(argv[0]).is_file() and Path(argv[0]).parent == bin_directory
    assert str(ROOT) not in hook["command"]
    environment = dict(_clean_environment(), CURAMOIR_JOB="sweep", CURAMOIR_USE_DCG="0")
    ran = subprocess.run(["/bin/sh", "-c", hook["command"]], input=json.dumps(payload),
                         capture_output=True, text=True, cwd=repo, env=environment,
                         timeout=hook["timeout"])

    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip(), "the installed hook allowed rm -rf: " + ran.stderr
    assert json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
