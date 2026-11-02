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
    assert plugin_files.SCRIPTS == REPO_ROOT / "scripts"


def test_from_the_checkout_every_reader_gets_the_path_it_had_before():
    """Each was `Path(__file__).resolve().parents[2] / ...` in its own module."""
    assert fix_step.PLUGIN_ROOT == REPO_ROOT
    assert sweep.LINT == REPO_ROOT / "scripts" / "curamoir-lint"
    assert scaffold._bundled_templates() == REPO_ROOT / "templates"
    assert doctor._registered_hook_events() == list(
        json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"])


def test_from_the_checkout_the_denylist_settings_are_the_wrapper_as_before(tmp_path):
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


# --- the installed layout's hook command -----------------------------------------------

def installed_command(python=sys.executable):
    settings = fix_step.pretooluse_settings(installed=True, python=python)
    (entry,) = settings["hooks"]["PreToolUse"]
    (hook,) = entry["hooks"]
    return entry, hook


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
    assert json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


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


def test_a_package_without_the_copy_is_never_installed(tmp_path):
    package = tmp_path / "site-packages" / "curamoir"
    package.mkdir(parents=True)

    assert not plugin_files.installed(package)


def test_doctor_does_not_probe_hooks_from_an_installed_package(tmp_path, monkeypatch):
    """An installed package is not the Claude Code plugin and carries no
    launcher scripts: one plain finding, not five false "missing" errors."""
    monkeypatch.setattr(plugin_files, "INSTALLED", True)

    (finding,) = doctor._check_hooks(tmp_path)

    assert finding.level == "ok"
    assert "installed package" in finding.detail
