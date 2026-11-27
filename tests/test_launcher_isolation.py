"""cm-6f0: no launcher in scripts/ imports code from the working directory.

`python -m` and `python -c` put the working directory first on sys.path, and
a hook's working directory is whatever repository is open: a `curamoir/`
package or a `yaml.py` committed there ran with the developer's privileges.
Every launcher now chooses and starts Python through src/curamoir/launcher.sh,
with `-P -E` and a bootstrap that inserts the plugin's own src/.

The planted modules write a marker file rather than print, so a launcher that
swallows its output still shows that planted code ran.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SELECTION = ROOT / "src" / "curamoir" / "launcher.sh"

HOOKS = ("pretooluse", "stop", "precompact", "sessionend", "sessionstart")
LAUNCHERS = (*(f"curamoir-hook-{hook}" for hook in HOOKS), "curamoir", "curamoir-lint")

#: What each launcher is given: a hook reads a JSON payload on stdin.
ARGS = {"curamoir": ["--help"], "curamoir-lint": ["--repo", "."]}


#: Each way planted code used to run, as (vector, the module name it writes).
#: test_each_planted_vector_is_live shows each one runs without the isolation.
VECTORS = {
    "package": "curamoir",           # curamoir/ in the working directory
    "cwd-json": "json",              # a stdlib module every hook imports
    "cwd-yaml": "yaml",              # what the PyYAML probe imports
    "pythonpath-json": "json",       # under a relative PYTHONPATH entry
    "pythonpath-yaml": "yaml",
    "pythonpath-sitecustomize": "sitecustomize",  # run at startup, even by a probe
}


def planted(directory, marker, vectors=VECTORS):
    """A working directory holding the planted `vectors` (all of them by
    default): a curamoir/ package (every module a launcher runs, and its
    __init__), a json.py and a yaml.py, and a json.py, yaml.py and
    sitecustomize.py under a relative PYTHONPATH entry, `vendor`. Each writes
    its module name to `marker`."""
    body = f"open({str(marker)!r}, 'a').write(__name__ + chr(10))\n"
    if "package" in vectors:
        package = directory / "curamoir"
        (package / "hooks").mkdir(parents=True)
        for module in ("__init__", "cli", "lint", "hooks/__init__",
                       *(f"hooks/{hook}" for hook in HOOKS)):
            (package / f"{module}.py").write_text(body, encoding="utf-8")
    (directory / "vendor").mkdir(exist_ok=True)
    for vector in vectors:
        place, _, name = vector.partition("-")
        if place == "cwd":
            (directory / f"{name}.py").write_text(body, encoding="utf-8")
        elif place == "pythonpath":
            (directory / "vendor" / f"{name}.py").write_text(body, encoding="utf-8")


def launch(name, cwd, env, stdin="{}"):
    return subprocess.run([str(SCRIPTS / name), *ARGS.get(name, [])], cwd=cwd, env=env,
                          input=stdin, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("interpreters", ["host", "unversioned-only"])
@pytest.mark.parametrize("name", LAUNCHERS)
def test_no_launcher_or_probe_runs_planted_code(tmp_path, name, interpreters):
    """`unversioned-only`: PATH holds only a `python3` (a stand-in that runs
    this interpreter), so the floor probe runs too, with a sitecustomize.py
    on the relative PYTHONPATH entry that any probe without -E would run."""
    marker = tmp_path / "ran.txt"
    work = tmp_path / "work"
    work.mkdir()
    planted(work, marker)
    if interpreters == "host":
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT), "HOME": str(tmp_path)}
    else:
        env, log = stand_ins(tmp_path, ["python3"])
    env["PYTHONPATH"] = "vendor"

    done = launch(name, work, env)

    assert not marker.exists(), f"{name} ran planted code: {marker.read_text()}"
    assert done.returncode == 0, done.stderr
    if interpreters == "unversioned-only":
        assert log.read_text().splitlines()[0].startswith("python3 -P -E -c import sys;")


@pytest.mark.parametrize("name", LAUNCHERS)
def test_the_planted_package_would_run_without_the_isolation(tmp_path, name):
    """The package is live for every launcher: run as the launchers used to
    (`-m`, the working directory first, PYTHONPATH honoured), it runs."""
    marker = tmp_path / "ran.txt"
    planted(tmp_path, marker, vectors=["package"])
    module = {"curamoir": "curamoir.cli", "curamoir-lint": "curamoir.lint"}.get(
        name, "curamoir.hooks." + name.removeprefix("curamoir-hook-"))

    subprocess.run([sys.executable, "-m", module], cwd=tmp_path, input="{}",
                   env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:vendor"},
                   capture_output=True, text=True, timeout=60)

    assert "curamoir" in marker.read_text().splitlines()


@pytest.mark.parametrize("vector", [v for v in VECTORS if v != "package"])
def test_each_planted_vector_is_live(tmp_path, vector):
    """Each other vector, planted alone, runs when Python is started as it
    used to be: a hook as `-m` (json, sitecustomize), the PyYAML probe as a
    plain `-c "import yaml"` (yaml), with PYTHONPATH honoured."""
    marker = tmp_path / "ran.txt"
    planted(tmp_path, marker, vectors=[vector])
    old_way = (["-c", "import yaml"] if vector.endswith("yaml")
               else ["-m", "curamoir.hooks.stop"])

    subprocess.run([sys.executable, *old_way], cwd=tmp_path, input="{}",
                   env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:vendor"},
                   capture_output=True, text=True, timeout=60)

    assert VECTORS[vector] in marker.read_text().splitlines()


# --- which interpreter qualifies ---------------------------------------------

def stand_ins(tmp_path, names, behaviour=""):
    """A PATH holding only bash and stand-ins for `names`, each logging its
    arguments and then running this interpreter. `behaviour` is shell run
    before that, with "$@" the arguments and "$real" this interpreter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "dirname", "sed"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    log = tmp_path / "calls.log"
    for name in names:
        script = bin_dir / name
        script.write_text(f'#!/bin/sh\nreal="{sys.executable}"\n'
                          f'printf "%s\\n" "{name} $*" >> "{log}"\n{behaviour}\n'
                          'exec "$real" "$@"\n', encoding="utf-8")
        script.chmod(0o755)
    return {"PATH": str(bin_dir), "HOME": str(tmp_path), "CLAUDE_PLUGIN_ROOT": str(ROOT)}, log


#: A python3 that is below the floor but has -P (3.11): it runs the floor
#: probe's own code, as a real 3.11.9 would, so the floor the probe states
#: decides (a probe whose floor drifted to 3.11 would let it through).
BELOW_THE_FLOOR = ('case "$4" in *version_info*) exec "$real" -P -E -c '
                   '"$(printf %s "$4" | sed "s/sys.version_info/(3, 11, 9)/")" ;; esac')
#: A python3 with no -P at all (before 3.11): Python exits 2 on the unknown flag.
NO_SAFE_PATH = 'case "$1" in -P) echo "Unknown option: -P" >&2; exit 2 ;; esac'


@pytest.mark.parametrize("behaviour", [BELOW_THE_FLOOR, NO_SAFE_PATH])
@pytest.mark.parametrize("hook", HOOKS)
def test_with_no_interpreter_that_qualifies_a_hook_exits_0_with_no_decision(tmp_path, hook,
                                                                             behaviour):
    env, log = stand_ins(tmp_path, ["python3"], behaviour)

    done = launch(f"curamoir-hook-{hook}", tmp_path, env)

    assert (done.returncode, done.stdout) == (0, "")
    # Only the probe ran: never a hook module.
    assert all("curamoir.hooks" not in line for line in log.read_text().splitlines())


@pytest.mark.parametrize(("name", "code"), [("curamoir", 1), ("curamoir-lint", 2)])
@pytest.mark.parametrize("behaviour", [BELOW_THE_FLOOR, NO_SAFE_PATH])
def test_with_no_interpreter_that_qualifies_the_cli_and_lint_say_so_in_one_line(
        tmp_path, name, code, behaviour):
    env, _ = stand_ins(tmp_path, ["python3"], behaviour)

    done = launch(name, tmp_path, env)

    assert done.returncode == code and done.stdout == ""
    (line,) = done.stderr.splitlines()
    assert "needs Python 3.12 or later" in line


@pytest.mark.skipif(not shutil.which("python3.9"), reason="needs a real python3.9 on PATH")
def test_a_real_interpreter_without_safe_path_is_skipped_not_blocking(tmp_path):
    """The case the probe exists for: a real 3.9 as python3 exits 2 on -P,
    which a hook must never pass on (exit 2 blocks the tool call)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(shutil.which("python3.9"))
    for tool in ("bash", "dirname"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "CLAUDE_PLUGIN_ROOT": str(ROOT)}

    done = launch("curamoir-hook-pretooluse", tmp_path, env,
                  stdin='{"tool_name": "Bash", "tool_input": {"command": "ls"}}')

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_versioned_interpreter_is_taken_unprobed(tmp_path):
    """A hook runs on every Bash call: a versioned name is at or above the
    floor by name and is started once, with no probe before it."""
    env, log = stand_ins(tmp_path, ["python3.12", "python3"])

    done = launch("curamoir-hook-stop", tmp_path, env)

    assert done.returncode == 0, done.stderr
    (call,) = log.read_text().splitlines()
    assert call.startswith("python3.12 -P -E -c ") and call.endswith(" curamoir.hooks.stop")


def test_an_unversioned_interpreter_is_probed_once_then_started(tmp_path):
    env, log = stand_ins(tmp_path, ["python3"])

    done = launch("curamoir-hook-stop", tmp_path, env)

    assert done.returncode == 0, done.stderr
    probe, start = log.read_text().splitlines()
    assert probe == ("python3 -P -E -c import sys; "
                     "raise SystemExit(sys.version_info < (3, 12))")
    assert start.endswith(" curamoir.hooks.stop")


def test_the_pyyaml_probe_uses_the_same_flags(tmp_path):
    env, log = stand_ins(tmp_path, ["python3.14"])

    launch("curamoir", tmp_path, env)

    assert "python3.14 -P -E -c import yaml" in log.read_text().splitlines()


# --- one place starts Python ---------------------------------------------------

def test_every_file_in_scripts_is_one_of_the_seven_launchers():
    assert sorted(p.name for p in SCRIPTS.iterdir()) == sorted(LAUNCHERS)


@pytest.mark.parametrize("name", LAUNCHERS)
def test_no_launcher_starts_python_outside_the_selection_file(name):
    """Only src/curamoir/launcher.sh names an interpreter, probes one or
    execs one; each launcher sources it and calls its functions."""
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    sourced = [line for line in code if re.search(r'^\s*\. "[^"]*/src/curamoir/launcher\.sh"',
                                                  line)]
    assert len(sourced) == 1, f"{name} does not source the selection file once"
    for line in code:
        # The selection file's own name and functions are what a launcher may say.
        # CURAMOIR_PYTHON is set and used only inside those functions.
        bare = re.sub(r"curamoir_\w+", "", line.replace("curamoir/launcher.sh", ""))
        assert not re.search(r"python|\bexec\b|-m curamoir|\s-c\s", bare, re.IGNORECASE), (
            f"{name} starts an interpreter itself: {line!r}")
    assert re.search(r"^\s*curamoir_exec_module ", text, re.MULTILINE)


# --- doctor ---------------------------------------------------------------------

def test_doctor_says_pythonpath_is_ignored_only_when_it_is_set():
    from curamoir.doctor import _check_pythonpath

    (finding,) = _check_pythonpath({"PYTHONPATH": "vendor"})
    assert finding.level == "warning" and "ignored" in finding.detail
    assert _check_pythonpath({}) == [] and _check_pythonpath({"PYTHONPATH": ""}) == []


def test_doctor_resolves_the_interpreter_the_launchers_would_take(monkeypatch):
    """An unversioned name that fails the probe is skipped, as the launchers
    skip it; with none that qualifies, the first that exists is reported, so
    the version probe can say what is wrong with it."""
    from curamoir import doctor

    found = {"python3": "/x/python3", "python": "/x/python"}
    monkeypatch.setattr(doctor.shutil, "which", found.get)
    monkeypatch.setattr(doctor, "_qualifies", lambda path: path == "/x/python")
    assert doctor._wrapper_interpreter() == ("/x/python", True)

    monkeypatch.setattr(doctor, "_qualifies", lambda path: False)
    assert doctor._wrapper_interpreter() == ("/x/python3", False)

    found["python3.13"] = "/x/python3.13"
    assert doctor._wrapper_interpreter() == ("/x/python3.13", True)


@pytest.mark.parametrize(("name", "code"), [
    *((f"curamoir-hook-{hook}", 0) for hook in HOOKS), ("curamoir", 1), ("curamoir-lint", 2)])
def test_a_missing_selection_file_is_guarded(tmp_path, name, code):
    """A source of a missing file that went on would end in exit 1, which
    the lint reads as violations: each launcher stops at the source instead,
    a hook with no decision, the CLI and the lint with one line."""
    copy = tmp_path / "plugin"
    shutil.copytree(SCRIPTS, copy / "scripts")
    shutil.copytree(ROOT / "src", copy / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "src" / "curamoir" / "launcher.sh").unlink()
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(copy)}

    done = subprocess.run([str(copy / "scripts" / name), *ARGS.get(name, [])], cwd=tmp_path,
                          env=env, input="{}", capture_output=True, text=True, timeout=60)

    assert done.returncode == code and done.stdout == ""
    if code:
        assert "cannot load" in done.stderr.splitlines()[-1]
    else:
        # The guard itself, not the next line's `|| exit 0` after a
        # `command not found`: a hook says nothing.
        assert done.stderr == ""


# --- the root is the launcher's own location (cm-6f0, edge cases) -----------------

def planted_root(directory, marker):
    """A plugin root planted under `directory/rel`: its launcher.sh and every
    module write the marker. A launcher that took its root from
    CLAUDE_PLUGIN_ROOT=rel would source and run these."""
    src = directory / "rel" / "src" / "curamoir"
    planted(directory / "rel" / "src", marker)
    (src / "launcher.sh").write_text(
        f"echo planted-launcher >> {shlex.quote(str(marker))}\n"
        "curamoir_choose_python() { CURAMOIR_PYTHON=true; }\n"
        "curamoir_no_python_message() { :; }\n"
        f"curamoir_exec_module() {{ echo planted-exec >> {shlex.quote(str(marker))}; exit 0; }}\n",
        encoding="utf-8")


RM_RF = '{"hook_event_name": "PreToolUse", "tool_name": "Bash", ' \
        '"tool_input": {"command": "rm -rf build/"}, "cwd": "."}'


@pytest.mark.parametrize("given", ["relative", "elsewhere"])
@pytest.mark.parametrize("name", LAUNCHERS)
def test_no_launcher_takes_its_root_from_claude_plugin_root(tmp_path, name, given):
    """Relative, CLAUDE_PLUGIN_ROOT would be read against the working
    directory (the open repository); absolute, it may name another copy of
    the code. Either way the launcher runs its own copy, and pretooluse
    still denies `rm -rf`."""
    marker = tmp_path / "ran.txt"
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").mkdir()
    planted_root(work, marker)
    other = tmp_path / "other"
    other.mkdir()
    planted_root(other, marker)
    root = "rel" if given == "relative" else str(other / "rel")
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": root, "HOME": str(tmp_path),
           "CURAMOIR_JOB": "sweep"}
    env.pop("PYTHONPATH", None)

    done = launch(name, work, env, stdin=RM_RF if name.endswith("pretooluse") else "{}")

    assert not marker.exists(), f"{name} ran planted code: {marker.read_text()}"
    assert done.returncode == 0, done.stderr
    if name.endswith("pretooluse"):
        decision = json.loads(done.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize("name", [n for n in LAUNCHERS if n != "curamoir-lint"])
def test_no_launcher_reads_claude_plugin_root(name):
    """The lint never did; since cm-6f0 no launcher does. Comments may name it."""
    code = [line for line in (SCRIPTS / name).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]
    assert not any("CLAUDE_PLUGIN_ROOT" in line for line in code)


def test_doctor_reports_no_qualifying_interpreter_as_an_error_whatever_the_version_probe_says(
        tmp_path, monkeypatch):
    """A python3 too old for -I (before 3.4) cannot report its version to
    doctor's probe, which reads as a warning; but no launcher would run it,
    so /curamoir:doctor must exit non-zero."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "dirname"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    # Unknown -P (the launchers' probe) and unknown -I (doctor's): exit 2.
    script = bin_dir / "python3"
    script.write_text('#!/bin/sh\necho "Unknown option: $1" >&2\nexit 2\n', encoding="utf-8")
    script.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    from curamoir.scaffold import init_repo
    init_repo(repo)
    from curamoir.doctor import diagnose
    monkeypatch.setenv("PATH", str(bin_dir))
    (python,) = [f for f in diagnose(repo) if f.check == "python3"]
    # Before cm-6f0's fix this was a warning: "unreadable version". (The hook
    # probes are errors too here, rightly: no hook can run.)
    assert python.level == "error" and "qualifies" in python.detail, python

    done = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); from curamoir.cli import main; "
         "raise SystemExit(main(['doctor', '--repo', sys.argv[2]]))",
         str(ROOT / "src"), str(repo)],
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=120)

    assert done.returncode != 0, done.stdout
