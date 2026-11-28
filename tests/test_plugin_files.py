"""cm-2qp.10: where cúramóir finds its plugin's own files.

Run from the plugin checkout (what Claude Code loads, what scripts/curamoir
runs, and the editable install these tests run under) every path is the one
the modules computed before (`Path(__file__).resolve().parents[2]` and what
sits beside src/), and the fix step's denylist --settings are byte-for-byte
what they were: these tests pin that. Installed from a wheel (`uvx --from
git+https://github.com/rsionnach/curamoir curamoir`), the files come from the
copy the wheel carries instead; tests/plugin/test_installed_package.py
builds a wheel and proves that end to end. Here only the hook command the
installed layout writes into the --settings is unit-tested, since the
checkout can never take that branch by itself.
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from curamoir import doctor, fix_step, plugin_files, scaffold, sweep

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- the checkout: unchanged ------------------------------------------------------------

def test_from_the_checkout_the_root_is_the_checkout():
    assert not plugin_files.INSTALLED
    assert plugin_files.ROOT == REPO_ROOT
    assert plugin_files.HOOKS_JSON == REPO_ROOT / "hooks" / "hooks.json"
    assert plugin_files.TEMPLATES == REPO_ROOT / "templates"
    assert plugin_files.PYTHON_EXAMPLES == REPO_ROOT / "examples" / "python"
    assert plugin_files.SCRIPTS == REPO_ROOT / "scripts"


def test_from_the_checkout_every_reader_gets_the_path_it_had_before():
    """Each was `Path(__file__).resolve().parents[2] / ...` in its own module."""
    assert fix_step.PLUGIN_ROOT == REPO_ROOT
    assert sweep.LINT == REPO_ROOT / "scripts" / "curamoir-lint"
    assert scaffold._bundled_templates() == REPO_ROOT / "templates"
    assert doctor._registered_hook_events() == list(
        json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"])


def test_from_the_checkout_the_denylist_settings_are_the_wrapper_as_before():
    """The local path is pinned: the command is the checkout's launcher
    script, the root shell-quoted in place of `"${CLAUDE_PLUGIN_ROOT}"`, and
    the rest of the entry (matcher, type, timeout) as hooks.json has it."""
    registered = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    expected = {"hooks": {"PreToolUse": [
        dict(entry, hooks=[dict(hook, command=shlex.quote(str(REPO_ROOT)) + hook["command"][
            len('"${CLAUDE_PLUGIN_ROOT}"'):]) for hook in entry["hooks"]])
        for entry in registered["hooks"]["PreToolUse"]]}}

    assert fix_step.pretooluse_settings() == expected
    (entry,) = expected["hooks"]["PreToolUse"]
    assert entry["hooks"][0]["command"] == (
        shlex.quote(str(REPO_ROOT)) + "/scripts/curamoir-hook-pretooluse")


def test_the_package_carries_no_copy_of_the_plugin_files_in_the_checkout():
    """The copy is made by the wheel build (pyproject's force-include), never
    committed: a second copy in src/ would drift from hooks/ and templates/."""
    assert not (REPO_ROOT / "src" / "curamoir" / "_plugin").exists()


# --- layout detection: plugin_files.installed() -----------------------------------------

def plugin_layout(base, *parts, checkout_hooks=False):
    package = base.joinpath(*parts)
    (package / "_plugin").mkdir(parents=True)
    if checkout_hooks:
        (base / "hooks").mkdir()
        (base / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    return package


def test_a_package_with_the_copy_outside_a_checkout_is_installed(tmp_path):
    package = plugin_layout(tmp_path, "lib", "python3.12", "site-packages", "curamoir")

    assert plugin_files.installed(package)


def test_a_stray_copy_in_the_checkouts_src_is_still_the_checkout(tmp_path):
    """A `src/curamoir/_plugin/` left by hand beside the checkout's own
    hooks/hooks.json: reading it would be reading a stale second copy."""
    package = plugin_layout(tmp_path, "src", "curamoir", checkout_hooks=True)

    assert not plugin_files.installed(package)


def test_a_copy_in_a_src_directory_with_no_checkout_hooks_beside_it_is_installed(tmp_path):
    """Being under `src/` alone is not a checkout: the guard needs the
    checkout's hooks/hooks.json two levels up too."""
    package = plugin_layout(tmp_path, "src", "curamoir")

    assert plugin_files.installed(package)


def test_a_package_without_the_copy_is_never_installed(tmp_path):
    package = tmp_path / "site-packages" / "curamoir"
    package.mkdir(parents=True)

    assert not plugin_files.installed(package)


# --- the installed layout's hook command ------------------------------------------------

def installed_command(python=sys.executable):
    settings = fix_step.pretooluse_settings(installed=True, python=python)
    (entry,) = settings["hooks"]["PreToolUse"]
    (hook,) = entry["hooks"]
    return entry, hook


def is_a_deny_claude_code_acts_on(returncode, stdout):
    """Whether a command hook that exited `returncode` having printed
    `stdout` blocks the PreToolUse call it was asked about: Claude Code
    reads a hook's JSON only on exit 0, and a PreToolUse decision only from
    a `hookSpecificOutput` whose `hookEventName` is PreToolUse. Anything
    else (a deny under another event name, a deny with exit 1, no JSON)
    lets the call through.

    Written here from that reading, not by calling fix_step's own check of
    a hook's answer: the tests that use it (here and tests/test_job_env.py)
    judge what the fix step loads, and must not ask it for the verdict."""
    if returncode != 0:
        return False
    try:
        answer = json.loads(stdout)
    except ValueError:
        return False
    specific = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
    return (isinstance(specific, dict) and specific.get("hookEventName") == "PreToolUse"
            and specific.get("permissionDecision") == "deny")


def _answer(decision="deny", event="PreToolUse"):
    return json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                              "permissionDecision": decision}})


@pytest.mark.parametrize("returncode, stdout, acted_on", [
    (0, _answer(), True),
    (0, _answer(event="preToolUse"), False),
    (0, _answer(event="PostToolUse"), False),
    (0, json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}}), False),
    (1, _answer(), False),
    (2, _answer(), False),
    (0, _answer(decision="allow"), False),
    (0, _answer(decision="ask"), False),
    (0, "", False),
    (0, "denied", False),
    (0, json.dumps([_answer()]), False),
    (0, json.dumps({"hookSpecificOutput": "deny"}), False),
    (0, json.dumps({"decision": "block"}), False),
], ids=["the-real-shape", "event-in-another-case", "another-event", "no-event", "exit-1",
        "exit-2", "allow", "ask", "nothing", "not-json", "a-list", "output-a-string",
        "the-legacy-shape"])
def test_only_exit_0_with_a_pretooluse_deny_is_a_deny_claude_code_acts_on(
        returncode, stdout, acted_on):
    """cm-60c (R5 provenance): the helper the acceptance tests judge a
    hook's answer by is held to both things it was written for, the exit
    code and the event name, so it cannot drift back to reading
    `permissionDecision` alone with every test that uses it still green."""
    assert is_a_deny_claude_code_acts_on(returncode, stdout) is acted_on


def test_installed_the_hook_runs_the_module_with_the_running_interpreter():
    """No launcher script and no checkout: the interpreter running the sweep
    has cúramóir importable (it is running it), so the hook is that
    interpreter running the hook's module. `-I` so the clone (the hook's
    working directory) is not put on sys.path and no PYTHONPATH sends the
    import elsewhere."""
    entry, hook = installed_command()

    assert shlex.split(hook["command"]) == [
        sys.executable, "-I", "-m", "curamoir.hooks.pretooluse"]
    registered = json.loads(plugin_files.HOOKS_JSON.read_text(encoding="utf-8"))
    (plugin_entry,) = registered["hooks"]["PreToolUse"]
    assert entry["matcher"] == plugin_entry["matcher"] == "Bash"
    assert {k: v for k, v in hook.items() if k != "command"} == {
        k: v for k, v in plugin_entry["hooks"][0].items() if k != "command"}


def test_installed_an_interpreter_path_with_spaces_and_quotes_is_one_word(tmp_path):
    """A stand-in at such a path that runs this interpreter, so the `-I`
    import check passes and only the quoting is under test."""
    python = tmp_path / "it's a \"venv\"" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
                      encoding="utf-8")
    python.chmod(0o755)
    python = str(python)

    _entry, hook = installed_command(python)

    assert shlex.split(hook["command"])[0] == python


@pytest.mark.parametrize("python", ["", None])
def test_installed_with_no_interpreter_to_name_the_denylist_cannot_load(python):
    """sys.executable can be empty or None (an embedded interpreter): then
    there is no command to write, and a fix step whose denylist cannot be
    loaded never runs."""
    with pytest.raises(ValueError, match="interpreter"):
        fix_step.pretooluse_settings(installed=True, python=python)


@pytest.mark.parametrize("command", [
    '"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir-hook-nosuchhook',
    '"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir-hook-pretooluse --flag',
    '"${CLAUDE_PLUGIN_ROOT}"/scripts/other-launcher',
    '"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir-hook-../policy',
], ids=["no-such-module", "arguments", "not-a-hook-launcher", "traversal"])
def test_installed_a_command_with_no_module_to_run_is_refused(tmp_path, monkeypatch, command):
    """Only the exact launcher form maps to a module; anything else would be
    a guess at what to run in its place, and a guessed denylist is no
    denylist. Refused, so the fix step never runs."""
    hooks_json = tmp_path / "hooks.json"
    hooks_json.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": command,
                                       "timeout": 5}]}]}}), encoding="utf-8")
    monkeypatch.setattr(plugin_files, "HOOKS_JSON", hooks_json)

    with pytest.raises(ValueError):
        fix_step.pretooluse_settings(installed=True, python=sys.executable)


def test_installed_the_command_denies_rm_rf_inside_a_job(tmp_path):
    """The command the installed layout writes, run through a shell as Claude
    Code runs a command hook, with the payload's `cwd` a cúramóir repository
    and CURAMOIR_JOB set, denies `rm -rf build/`. (Here the interpreter is
    the test's own; the wheel test runs the installed one.)"""
    (tmp_path / ".curamoir").mkdir()
    _entry, hook = installed_command()
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "rm -rf build/"}, "cwd": str(tmp_path)}

    ran = subprocess.run(["/bin/sh", "-c", hook["command"]], input=json.dumps(payload),
                         capture_output=True, text=True, cwd=tmp_path, timeout=hook["timeout"],
                         env={"PATH": "/usr/bin:/bin", "CURAMOIR_JOB": "sweep",
                              "CURAMOIR_USE_DCG": "0"})

    assert ran.returncode == 0, ran.stderr
    assert is_a_deny_claude_code_acts_on(ran.returncode, ran.stdout), ran.stdout


#: Where a `curamoir` package that is not cúramóir's can sit when the hook
#: runs: committed in the clone (the hook's working directory), and in a
#: directory PYTHONPATH names.
PLANTED_IN = ("working-directory", "pythonpath")


def planted_package(directory, marker, label):
    """A `curamoir` package in `directory` whose hooks/pretooluse.py, run
    as the hook, says nothing and exits 0 (which ALLOWS the call). Every
    module of it appends `label` and its name to `marker` when imported,
    so a run that reached it shows, whatever became of its output."""
    body = f"open({str(marker)!r}, 'a').write({label!r} + ' ' + __name__ + chr(10))\n"
    (directory / "curamoir" / "hooks").mkdir(parents=True)
    for module in ("__init__", "hooks/__init__", "hooks/pretooluse"):
        (directory / "curamoir" / f"{module}.py").write_text(body, encoding="utf-8")


def ask_about_rm_rf(argv_or_command, cwd, repo, pythonpath=None):
    """Run a hook (a shell command as hooks carry it, or an argv) in `cwd`,
    inside a job, with a PreToolUse payload for `rm -rf build/` in `repo`."""
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "rm -rf build/"}, "cwd": str(repo)}
    env = {"PATH": "/usr/bin:/bin", "CURAMOIR_JOB": "sweep", "CURAMOIR_USE_DCG": "0"}
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    argv = (["/bin/sh", "-c", argv_or_command] if isinstance(argv_or_command, str)
            else argv_or_command)
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=60)


def test_installed_the_command_denies_rm_rf_whatever_curamoir_the_clone_plants(tmp_path):
    """cm-60c (R5 provenance): what `-I` is in the command for, run rather
    than read. The clone (the hook's working directory) holds a `curamoir`
    package whose pretooluse module allows everything, and PYTHONPATH names
    a second directory holding another; the installed command still runs
    cúramóir's own hook, which denies, and neither planted package is
    imported. (The next test shows each planted package is what runs
    without `-I`.)"""
    marker = tmp_path / "ran.txt"
    clone, elsewhere = tmp_path / "clone", tmp_path / "elsewhere"
    (clone / ".curamoir").mkdir(parents=True)
    planted_package(clone, marker, "working-directory")
    planted_package(elsewhere, marker, "pythonpath")
    _entry, hook = installed_command()

    ran = ask_about_rm_rf(hook["command"], clone, clone, pythonpath=elsewhere)

    assert is_a_deny_claude_code_acts_on(ran.returncode, ran.stdout), (
        f"the hook did not deny rm -rf: exit {ran.returncode}, stdout {ran.stdout!r}, "
        f"stderr {ran.stderr!r}, planted modules imported: "
        f"{marker.read_text() if marker.exists() else 'none'}")
    assert not marker.exists(), f"a planted curamoir was imported: {marker.read_text()}"


@pytest.mark.parametrize("planted_in", PLANTED_IN)
def test_each_planted_curamoir_is_what_runs_without_dash_i(tmp_path, planted_in):
    """The fixture above is live: the installed command with `-I` taken
    out and nothing else changed, run with one planted package alone (in
    the working directory, or in a directory PYTHONPATH names with the
    working directory empty of it), imports the planted pretooluse module
    as the hook and denies nothing."""
    marker = tmp_path / "ran.txt"
    clone, elsewhere = tmp_path / "clone", tmp_path / "elsewhere"
    (clone / ".curamoir").mkdir(parents=True)
    elsewhere.mkdir()
    planted_package(clone if planted_in == "working-directory" else elsewhere, marker, planted_in)
    _entry, hook = installed_command()
    with_dash_i = shlex.split(hook["command"])
    without = [word for word in with_dash_i if word != "-I"]
    assert with_dash_i == [without[0], "-I", *without[1:]]

    ran = ask_about_rm_rf(without, clone, clone,
                          pythonpath=elsewhere if planted_in == "pythonpath" else None)

    assert f"{planted_in} __main__" in marker.read_text().splitlines(), ran.stderr
    assert (ran.returncode, ran.stdout) == (0, "")
    assert not is_a_deny_claude_code_acts_on(ran.returncode, ran.stdout)


def test_installed_an_interpreter_that_cannot_import_the_hook_under_dash_i_is_refused(tmp_path):
    """`-I` implies `-s`: a cúramóir reachable only through the user's
    site-packages imports here and not in the hook, which would then fail
    open. So the import is checked by running the interpreter as the hook
    will; a stand-in that cannot import (exits 1) is refused, as is one
    that does not exist."""
    cannot = tmp_path / "python"
    cannot.write_text("#!/bin/sh\necho 'ModuleNotFoundError' >&2\nexit 1\n", encoding="utf-8")
    cannot.chmod(0o755)

    with pytest.raises(ValueError, match=r"-I cannot import curamoir\.hooks\.pretooluse"):
        fix_step.pretooluse_settings(installed=True, python=str(cannot))
    with pytest.raises(ValueError, match="cannot run"):
        fix_step.pretooluse_settings(installed=True, python=str(tmp_path / "absent"))


def test_installed_the_import_is_checked_under_dash_i(tmp_path):
    """A stand-in that runs this interpreter except when `-I` is among its
    arguments, where it fails as a cúramóir reachable only through user
    site-packages would: the check must use `-I`, so it is refused."""
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = -I ] && exit 1; done\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)

    with pytest.raises(ValueError, match="cannot import"):
        fix_step.pretooluse_settings(installed=True, python=str(python))


# --- doctor -----------------------------------------------------------------------------

def test_doctor_does_not_probe_hooks_from_an_installed_package(tmp_path, monkeypatch):
    """An installed package is not the Claude Code plugin and carries no
    launcher scripts: one plain finding, not five false "missing" errors."""
    monkeypatch.setattr(plugin_files, "INSTALLED", True)

    (finding,) = doctor._check_hooks(tmp_path)

    assert finding.level == "ok"
    assert "installed package" in finding.detail
