"""hooks/hooks.json registers the denylist hook correctly.

DEC-5 findings this encodes:
- `matcher` scopes the hook so it is not invoked for non-Bash tools.
- A SHORT explicit timeout. The default is 600s, and a timed-out command hook
  fails OPEN, so an unbounded timeout means a hung hook silently stops
  protecting while also stalling the tool call for ten minutes.
"""

import json
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "hooks" / "hooks.json"


def load():
    return json.loads(HOOKS.read_text(encoding="utf-8"))


def pretooluse_entries():
    return load()["hooks"]["PreToolUse"]


def test_hooks_json_is_valid_json():
    assert load()


def test_the_denylist_hook_is_scoped_to_bash():
    matchers = [entry.get("matcher") for entry in pretooluse_entries()]

    assert "Bash" in matchers


def test_the_denylist_hook_has_a_short_explicit_timeout():
    """Unbounded would mean a hung hook stalls every Bash call for the 600s
    default while, because hooks fail open, protecting nothing."""
    handlers = [h for entry in pretooluse_entries() for h in entry["hooks"]]
    timeouts = [h.get("timeout") for h in handlers]

    assert timeouts, "no handlers registered"
    for timeout in timeouts:
        assert timeout is not None, "handler has no explicit timeout"
        assert timeout <= 10, f"timeout {timeout}s is too long for a per-call hook"


def test_every_handler_is_a_command_handler_with_a_command():
    for entry in pretooluse_entries():
        for handler in entry["hooks"]:
            assert handler["type"] == "command"
            assert handler["command"].strip()


def test_the_registered_command_points_at_an_executable_that_exists():
    """Previously this asserted the command named the module directly, which
    encoded a bug: that form only works when `curamoir` is already importable,
    which is false for an installed plugin. The command now goes through a
    wrapper rooted at ${CLAUDE_PLUGIN_ROOT} — see tests/plugin/test_hook_wiring.py.
    """
    import os

    root = HOOKS.parents[1]
    handlers = [h for entry in pretooluse_entries() for h in entry["hooks"]]

    assert handlers
    for handler in handlers:
        relative = handler["command"].replace('"${CLAUDE_PLUGIN_ROOT}"/', "")
        target = root / relative
        assert target.is_file(), f"hook command target missing: {relative}"
        assert os.access(target, os.X_OK), f"hook command target not executable: {relative}"


# --- the Epic 8 handoff hooks ---------------------------------------------

def handlers_for(event):
    config = load()
    return [h for entry in config["hooks"].get(event, []) for h in entry["hooks"]]


def test_all_three_handoff_events_are_registered():
    """Three, not the two E8-R1 names: Stop fires every turn, so it cannot be
    where the handoff is written. See the correction recorded on E8-R1."""
    for event in ("Stop", "PreCompact", "SessionEnd"):
        assert handlers_for(event), f"{event} hook not registered"


def test_the_stop_hook_has_a_very_short_timeout_because_it_runs_every_turn():
    for handler in handlers_for("Stop"):
        assert handler["timeout"] <= 3, handler


def test_the_sessionend_hook_stays_inside_the_shared_budget():
    """SessionEnd hooks share 1.5s across all of them. A generous timeout here
    would risk the handoff never being written."""
    for handler in handlers_for("SessionEnd"):
        assert handler["timeout"] <= 2, handler


def test_every_handoff_hook_command_target_exists_and_is_executable():
    import os

    root = HOOKS.parents[1]
    for event in ("Stop", "PreCompact", "SessionEnd"):
        for handler in handlers_for(event):
            relative = handler["command"].replace('"${CLAUDE_PLUGIN_ROOT}"/', "")
            target = root / relative
            assert target.is_file(), f"{event}: missing {relative}"
            assert os.access(target, os.X_OK), f"{event}: not executable {relative}"


def test_sessionstart_is_registered_with_a_short_timeout():
    """It delays the first turn of every session, so it must be quick."""
    handlers = handlers_for("SessionStart")

    assert handlers, "SessionStart hook not registered"
    for handler in handlers:
        assert handler["timeout"] <= 5, handler


def test_the_sessionstart_command_target_exists_and_is_executable():
    import os

    root = HOOKS.parents[1]
    for handler in handlers_for("SessionStart"):
        relative = handler["command"].replace('"${CLAUDE_PLUGIN_ROOT}"/', "")
        target = root / relative
        assert target.is_file() and os.access(target, os.X_OK), relative
