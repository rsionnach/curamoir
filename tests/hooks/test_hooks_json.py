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


def test_the_registered_command_points_at_the_module_that_exists():
    handlers = [h for entry in pretooluse_entries() for h in entry["hooks"]]

    assert any("curamoir.hooks.pretooluse" in h["command"] for h in handlers)
