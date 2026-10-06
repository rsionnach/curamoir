"""E8-R3: SessionStart injects the handoff, and the brief when it is newer.

Verified against the live hook reference 2026-10-06:
  input   source ("startup" | "resume" | "clear" | "compact" | "fork"), and on
          resume/fork also seconds_since_last_response, context_tokens, ...
  output  hookSpecificOutput.additionalContext — wrapped in a system reminder and
          inserted before the first prompt
  limit   10,000 CHARACTERS per string. Beyond that Claude Code spills it to a
          file and substitutes a path plus a 2,000-character preview, so
          overrunning does not merely cost tokens, it changes what Claude sees.

This is G2 at its most literal — "reduce re-entry cost after absence to a single
short read" — so the budget is the feature, not a safeguard.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(payload, repo):
    return subprocess.run(
        [sys.executable, "-m", "curamoir.hooks.sessionstart"],
        input=json.dumps({**payload, "cwd": str(repo)}),
        capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
    )


def context_of(result):
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def prepared(tmp_path, handoff=None, brief=None, brief_newer=False):
    repo = tmp_path / "repo"
    (repo / ".curamoir" / "handoff").mkdir(parents=True)
    (repo / ".curamoir" / "briefs").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    if handoff is not None:
        path = repo / ".curamoir" / "handoff" / "latest.md"
        path.write_text(handoff, encoding="utf-8")
        when = (now - timedelta(hours=2)).timestamp()
        os.utime(path, (when, when))
    if brief is not None:
        path = repo / ".curamoir" / "briefs" / "latest.md"
        path.write_text(brief, encoding="utf-8")
        offset = timedelta(hours=1) if brief_newer else timedelta(hours=5)
        when = (now - offset).timestamp()
        os.utime(path, (when, when))
    return repo


def test_the_handoff_is_injected_on_a_fresh_session(tmp_path):
    repo = prepared(tmp_path, handoff="# handoff\n\nbranch: feat/x\n")

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    assert context is not None
    assert "feat/x" in context


def test_the_brief_is_injected_when_it_is_newer_than_the_handoff(tmp_path):
    repo = prepared(
        tmp_path, handoff="# handoff\n\nold state\n",
        brief="# brief\n\nStart here: b-2\n", brief_newer=True,
    )

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    assert "Start here: b-2" in context


def test_an_older_brief_is_not_injected(tmp_path):
    """"the away-brief if newer than the handoff". A stale brief would contradict
    the handoff and cost tokens to do it."""
    repo = prepared(
        tmp_path, handoff="# handoff\n\nfresh state\n",
        brief="# brief\n\nstale brief content\n", brief_newer=False,
    )

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    assert "stale brief content" not in context
    assert "fresh state" in context


def test_the_injection_points_at_the_full_files(tmp_path):
    """Truncation must never be silent data loss."""
    repo = prepared(tmp_path, handoff="# handoff\n\nstate\n")

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    assert ".curamoir/handoff/latest.md" in context


def test_the_injection_stays_within_the_token_budget(tmp_path):
    repo = prepared(tmp_path, handoff="# handoff\n\n" + ("a decision line\n" * 4000))

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    # ~4 characters per token, the standard rough approximation; the default
    # budget is 1,500 tokens.
    assert len(context) <= 1500 * 4 + 400, f"{len(context)} characters"


def test_the_injection_never_approaches_the_ten_thousand_character_hard_limit(tmp_path):
    """Beyond 10,000 characters Claude Code spills the context to a file and
    substitutes a path plus a preview, which silently changes what Claude sees."""
    repo = prepared(tmp_path, handoff="x" * 50_000)

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    assert len(context) < 10_000


def test_truncation_says_that_it_truncated(tmp_path):
    repo = prepared(tmp_path, handoff="# handoff\n\n" + ("a decision line\n" * 4000))

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    assert "truncated" in context.lower()


def test_nothing_is_injected_when_there_is_no_handoff_or_brief(tmp_path):
    """A first-ever session has nothing to say, and saying so would waste tokens
    on every session until the first handoff exists."""
    repo = prepared(tmp_path)

    assert context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo)) is None


def test_nothing_is_injected_on_resume_of_a_recent_session(tmp_path):
    """On resume the context is still largely present. Re-injecting state the
    session already has is pure cost."""
    repo = prepared(tmp_path, handoff="# handoff\n\nstate\n")

    result = run({
        "hook_event_name": "SessionStart", "source": "resume",
        "seconds_since_last_response": 120,
    }, repo)

    assert context_of(result) is None


def test_the_handoff_is_injected_on_resume_after_a_long_gap(tmp_path):
    """A resume hours later is a return, whatever the source field calls it."""
    repo = prepared(tmp_path, handoff="# handoff\n\nstate\n")

    result = run({
        "hook_event_name": "SessionStart", "source": "resume",
        "seconds_since_last_response": 86_400,
    }, repo)

    assert context_of(result) is not None


def test_the_handoff_is_injected_after_a_compaction(tmp_path):
    """Compaction is exactly when the handoff earns its keep: the PreCompact hook
    wrote it moments ago precisely because the context was about to be lost."""
    repo = prepared(tmp_path, handoff="# handoff\n\nstate\n")

    result = run({"hook_event_name": "SessionStart", "source": "compact"}, repo)

    assert context_of(result) is not None


def test_the_context_is_factual_not_imperative(tmp_path):
    """The docs say to write context as factual statements rather than system
    instructions. A hook that issues orders is a hook that fights the user."""
    repo = prepared(tmp_path, handoff="# handoff\n\nstate\n")

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    for imperative in ("you must", "you should", "always ", "never "):
        assert imperative not in context.lower(), imperative


def test_malformed_input_injects_nothing_and_exits_clean(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "curamoir.hooks.sessionstart"], input="not json",
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_the_token_budget_is_read_from_config(tmp_path):
    """E8-R3 says "configurable". It was configurable only in the Python
    signature: main() called build_context(repo) and never read the config, and
    writing the key would have been rejected as unknown."""
    from curamoir.hooks.sessionstart import token_budget_for

    repo = prepared(tmp_path, handoff="# handoff\n\nstate\n")
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        "version: 1\nhandoff:\n  session_start_token_budget: 300\n", encoding="utf-8")

    assert token_budget_for(repo) == 300


def test_the_budget_from_config_actually_bounds_the_injection(tmp_path):
    repo = prepared(tmp_path, handoff="# handoff\n\n" + ("a decision line\n" * 2000))
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        "version: 1\nhandoff:\n  session_start_token_budget: 200\n", encoding="utf-8")

    context = context_of(run({"hook_event_name": "SessionStart", "source": "startup"}, repo))

    assert len(context) <= 200 * 4 + 400, f"{len(context)} characters"
