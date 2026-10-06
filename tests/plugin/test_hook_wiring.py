"""The hook must work when cúramóir is INSTALLED as a plugin, not only in dev.

E0-R5 shipped `python3 -m curamoir.hooks.pretooluse`, which silently assumes the
package is on the interpreter's import path. Installed as a plugin it is not:
the plugin is an unpacked directory, not a pip install. The hook would fail to
start on every Bash call — and because command hooks fail OPEN, it would protect
nothing while looking configured.

The fix is `${CLAUDE_PLUGIN_ROOT}`, which Claude Code resolves in a hook's
`command` and also exports to the hook process.
"""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks" / "hooks.json"
WRAPPER = ROOT / "scripts" / "curamoir-hook-pretooluse"


def handlers():
    config = json.loads(HOOKS.read_text(encoding="utf-8"))
    return [h for entry in config["hooks"]["PreToolUse"] for h in entry["hooks"]]


def test_the_hook_command_does_not_assume_the_package_is_importable():
    """A bare `python3 -m curamoir...` only works when PYTHONPATH already has
    src/, which is true in this repo's tests and false for an installed plugin."""
    for handler in handlers():
        command = handler["command"]
        assert "-m curamoir" not in command, (
            "hook invokes the module directly; it must go through the wrapper so "
            "the import path is set from ${CLAUDE_PLUGIN_ROOT}"
        )


def test_the_hook_command_is_rooted_at_the_plugin_root():
    for handler in handlers():
        assert "${CLAUDE_PLUGIN_ROOT}" in handler["command"]


def test_the_plugin_root_variable_is_quoted_so_a_path_with_spaces_survives():
    """An unquoted variable in a shell-form hook is a `claude plugin validate`
    warning and breaks on any path containing a space."""
    for handler in handlers():
        assert '"${CLAUDE_PLUGIN_ROOT}"' in handler["command"]


def test_the_wrapper_script_exists_and_is_executable():
    assert WRAPPER.is_file()
    assert os.access(WRAPPER, os.X_OK), "wrapper must be executable"


def test_the_wrapper_denies_a_destructive_command_with_no_pythonpath_set():
    """The real installed-plugin condition: nothing on PYTHONPATH, only
    CLAUDE_PLUGIN_ROOT pointing at the plugin directory."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard"},
        "cwd": str(ROOT),
    }
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
        "CURAMOIR_JOB": "sweep",
    }

    result = subprocess.run(
        [str(WRAPPER)], input=json.dumps(payload),
        capture_output=True, text=True, env=environment,
    )

    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "stash" in decision["permissionDecisionReason"]


def test_the_wrapper_passes_through_an_ordinary_command():
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": str(ROOT),
    }
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
        "CURAMOIR_JOB": "sweep",
    }

    result = subprocess.run(
        [str(WRAPPER)], input=json.dumps(payload),
        capture_output=True, text=True, env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
