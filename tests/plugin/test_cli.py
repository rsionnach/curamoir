"""The CLI the slash commands call, and the wrapper that makes it work installed.

Slash commands do NOT receive ${CLAUDE_PLUGIN_ROOT} in their Bash environment.
Claude Code substitutes it into the command's Markdown body when it loads the
command, so the body carries the absolute path and the wrapper reads it as an
argument-free environment variable only when a hook invokes it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "curamoir"


def run_cli(*args, cwd=None, env=None):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "curamoir.cli", *args],
        capture_output=True, text=True, cwd=str(cwd or ROOT), env=environment,
    )


def test_init_scaffolds_the_target_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "__init__.py").write_text("", encoding="utf-8")

    result = run_cli("init", "--repo", str(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / ".curamoir" / "curamoir.yaml").is_file()


def test_init_reports_what_it_detected():
    """A scaffolding step that says nothing leaves the developer unsure whether
    detection worked. It must name the languages and modules it found."""
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        (repo / "pyproject.toml").write_text("", encoding="utf-8")
        (repo / "src" / "app" / "core").mkdir(parents=True)
        (repo / "src" / "app" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "src" / "app" / "core" / "__init__.py").write_text("", encoding="utf-8")

        result = run_cli("init", "--repo", str(repo))

        assert "python" in result.stdout
        assert "src/app/core" in result.stdout


def test_a_second_init_exits_non_zero_and_names_the_new_file(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    run_cli("init", "--repo", str(tmp_path))

    result = run_cli("init", "--repo", str(tmp_path))

    assert result.returncode != 0
    assert "curamoir.yaml.new" in result.stdout + result.stderr


def test_the_wrapper_script_exists_and_is_executable():
    assert WRAPPER.is_file()
    assert os.access(WRAPPER, os.X_OK)


def test_the_wrapper_runs_the_cli_without_pythonpath_set(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
    }

    result = subprocess.run(
        [str(WRAPPER), "init", "--repo", str(tmp_path)],
        capture_output=True, text=True, env=environment,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / ".curamoir" / "curamoir.yaml").is_file()


# --- the slash command ----------------------------------------------------

def test_the_init_command_file_exists_so_it_is_namespaced_automatically():
    """Commands are discovered from commands/ and namespaced under the plugin
    name, which is what makes this /curamoir:init (E1-R6)."""
    assert (ROOT / "commands" / "init.md").is_file()


def test_the_command_body_references_the_plugin_root_variable():
    """A slash command's Bash environment does NOT carry CLAUDE_PLUGIN_ROOT;
    Claude Code substitutes the variable into the Markdown body at load time, so
    the reference has to be in the body."""
    body = (ROOT / "commands" / "init.md").read_text(encoding="utf-8")

    assert "${CLAUDE_PLUGIN_ROOT}" in body


@pytest.mark.skipif(not (ROOT / "commands").is_dir(), reason="no commands dir")
def test_the_loaded_plugin_exposes_the_init_command():
    import shutil

    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed")

    result = subprocess.run(
        ["claude", "--plugin-dir", str(ROOT), "plugin", "details", "curamoir"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Commands (1)" in result.stdout or "init" in result.stdout, result.stdout
