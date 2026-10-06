"""E1-R1 + E1-R6: the plugin is installable, and its commands are namespaced.

Verified against the live manifest and marketplace references (2026-10-06), not
against recollection — the spec flags these fields as changing between releases.

`claude plugin validate --strict` is the authoritative check, so the tests that
matter most shell out to it rather than re-implementing its rules here. The
hand-written assertions cover the facts we rely on elsewhere in the codebase.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- plugin.json -----------------------------------------------------------

def test_the_manifest_lives_at_the_documented_path():
    assert MANIFEST.is_file()


def test_the_plugin_name_is_curamoir_in_kebab_case():
    """Components namespace under `name`, so this is what makes commands appear
    as /curamoir:<name> (E1-R6). It also must not read as one of Anthropic's own."""
    name = load(MANIFEST)["name"]

    assert name == "curamoir"
    assert name == name.lower()
    for forbidden in (" ", "@", ":", "/", "\\"):
        assert forbidden not in name
    for reserved_prefix in ("claude-", "anthropic-", "anthropics-", "cc-plugin-"):
        assert not name.startswith(reserved_prefix)


def test_the_manifest_sets_the_metadata_that_strict_validation_requires():
    """version, description and author are warnings when missing, and warnings
    fail under --strict, which is what we want in CI."""
    manifest = load(MANIFEST)

    assert manifest["version"]
    assert manifest["description"]
    assert manifest["author"]["name"]


def test_every_component_path_starts_with_dot_slash_and_exists():
    """Manifest component paths must be `./`-prefixed, inside the plugin root,
    and present, or the component silently does not load."""
    manifest = load(MANIFEST)
    component_keys = (
        "skills", "commands", "agents", "hooks", "outputStyles", "workflows",
    )
    for key in component_keys:
        value = manifest.get(key)
        if value is None:
            continue
        paths = [value] if isinstance(value, str) else value
        for path in paths:
            if not isinstance(path, str):
                continue
            assert path.startswith("./"), f"{key}: {path!r} must start with ./"
            assert ".." not in path, f"{key}: {path!r} must not traverse"
            assert (ROOT / path).exists(), f"{key}: {path!r} does not exist"


def test_the_default_hooks_file_is_present_so_it_merges_without_a_manifest_key():
    """`hooks` MERGES with hooks/hooks.json rather than replacing it, so the
    denylist hook loads from the default location with no manifest key."""
    assert (ROOT / "hooks" / "hooks.json").is_file()


def test_the_manifest_does_not_set_commands_which_would_disable_the_default_scan():
    """Setting `commands` REPLACES the default commands/ scan. Later beads add
    command files to commands/, so the key must stay unset or they vanish."""
    assert "commands" not in load(MANIFEST)


# --- marketplace.json ------------------------------------------------------

def test_the_marketplace_file_lives_at_the_documented_path():
    assert MARKETPLACE.is_file()


def test_the_marketplace_declares_the_required_fields():
    marketplace = load(MARKETPLACE)

    assert marketplace["name"]
    assert marketplace["owner"]["name"]
    assert isinstance(marketplace["plugins"], list) and marketplace["plugins"]
    assert marketplace["description"], "missing description is a --strict failure"


def test_the_entry_points_at_the_repository_root():
    """The plugin IS this repository, so the entry's source is the marketplace
    root itself. A relative source only resolves for github/git/file/directory
    marketplace sources, which is how `marketplace add owner/repo` arrives."""
    entry = load(MARKETPLACE)["plugins"][0]

    assert entry["name"] == "curamoir"
    assert entry["source"] == "."


# --- the authoritative check ----------------------------------------------

@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_claude_plugin_validate_passes_strict():
    """The real validator, in strict mode, so unrecognized fields and missing
    metadata fail here rather than surprising a user at install time."""
    for target in (str(ROOT), str(MANIFEST)):
        result = subprocess.run(
            ["claude", "plugin", "validate", target, "--strict"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"{target}\n" + result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_the_plugin_actually_loads_from_a_directory():
    """E1-R1's dev-loading half: `claude --plugin-dir ./`.

    Asserting on the component inventory rather than just an exit code, because
    a plugin can load while silently contributing nothing — which is exactly
    what a mis-pathed hook looks like.
    """
    result = subprocess.run(
        ["claude", "--plugin-dir", str(ROOT), "plugin", "details", "curamoir"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout
    assert "curamoir" in output
    assert "0.1.0" in output
    # The denylist hook (E0-R5) must be among the loaded components.
    assert "PreToolUse" in output, f"PreToolUse hook not loaded:\n{output}"


ALWAYS_ON_TOKEN_BUDGET = 600


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_the_plugin_stays_within_its_always_on_token_budget():
    """cúramóir runs on one subscription against a $15 ceiling, so a plugin that
    taxes every session before doing any work undercuts its own premise.

    Measured: each command costs ~40 tok always-on (its name and description, so
    the model knows it exists) and ~470 on-invoke (the body, paid only when it
    fires). Thirteen commands is therefore ~520 always-on, and the budget here is
    600 — set deliberately close, so adding a fourteenth command or padding a
    description fails this test rather than quietly taxing every session.
    """
    import re

    result = subprocess.run(
        ["claude", "--plugin-dir", str(ROOT), "plugin", "details", "curamoir"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    match = re.search(r"Always-on:\s*~?(\d+)\s*tok", result.stdout)
    assert match, f"could not read always-on cost from:\n{result.stdout}"

    always_on = int(match.group(1))
    assert always_on <= ALWAYS_ON_TOKEN_BUDGET, (
        f"always-on context cost is {always_on} tok, over the "
        f"{ALWAYS_ON_TOKEN_BUDGET} tok budget"
    )
