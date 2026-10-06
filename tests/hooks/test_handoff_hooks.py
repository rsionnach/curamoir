"""The Stop / PreCompact / SessionEnd hooks, run as real subprocesses.

Payload shapes verified against the live hook reference 2026-10-06:
  Stop         transcript_path, last_assistant_message, permission_mode, effort
  PreCompact   trigger ("manual" | "auto"), transcript_path
  SessionEnd   shares a 1.5s budget with every other SessionEnd hook

Each hook must exit 0 and emit no decision. Writing a handoff is not grounds to
block a turn, to block compaction, or to delay exit.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(module, payload, repo):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps({**payload, "cwd": str(repo)}),
        capture_output=True, text=True, env=environment, timeout=30,
    )


def prepared(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".curamoir" / "handoff" / "history").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo, check=True)
    return repo


# --- Stop ------------------------------------------------------------------

def test_stop_records_the_turn_without_writing_a_handoff(tmp_path):
    """Stop fires EVERY TURN. Writing a full handoff here would mean dozens of
    writes and dozens of model calls per session."""
    repo = prepared(tmp_path)

    result = run("curamoir.hooks.stop", {
        "hook_event_name": "Stop",
        "last_assistant_message": "Settled on failing closed in the gateway.",
    }, repo)

    assert result.returncode == 0, result.stderr
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    assert journal.is_file()
    assert "failing closed" in journal.read_text(encoding="utf-8")
    assert not (repo / ".curamoir" / "handoff" / "latest.md").exists()


def test_stop_emits_no_decision_so_it_never_blocks_a_turn(tmp_path):
    repo = prepared(tmp_path)

    result = run("curamoir.hooks.stop", {
        "hook_event_name": "Stop", "last_assistant_message": "Done.",
    }, repo)

    assert result.stdout.strip() == "", result.stdout


def test_stop_is_fast_because_it_runs_on_every_turn(tmp_path):
    repo = prepared(tmp_path)
    started = time.monotonic()

    run("curamoir.hooks.stop", {"hook_event_name": "Stop", "last_assistant_message": "x"}, repo)

    assert time.monotonic() - started < 3.0


def test_stop_on_an_uninitialised_repo_does_nothing_quietly(tmp_path):
    """cúramóir is not installed everywhere the developer works."""
    plain = tmp_path / "plain"
    plain.mkdir()

    result = run("curamoir.hooks.stop", {
        "hook_event_name": "Stop", "last_assistant_message": "hello",
    }, plain)

    assert result.returncode == 0
    assert not (plain / ".curamoir").exists()


# --- PreCompact ------------------------------------------------------------

def test_precompact_writes_the_full_handoff(tmp_path):
    """The moment that matters most: a session about to compact has already lost
    the context the handoff preserves."""
    repo = prepared(tmp_path)

    result = run("curamoir.hooks.precompact", {
        "hook_event_name": "PreCompact", "trigger": "auto",
    }, repo)

    assert result.returncode == 0, result.stderr
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    assert handoff.is_file()
    assert "cúramóir handoff" in handoff.read_text(encoding="utf-8")


def test_precompact_never_blocks_compaction(tmp_path):
    """PreCompact CAN block, via exit 2 or decision: block. Blocking compaction
    to write a note would be an absurd trade."""
    repo = prepared(tmp_path)

    result = run("curamoir.hooks.precompact", {
        "hook_event_name": "PreCompact", "trigger": "manual",
    }, repo)

    assert result.returncode == 0
    assert "block" not in result.stdout


def test_precompact_records_whether_compaction_was_manual_or_automatic(tmp_path):
    repo = prepared(tmp_path)

    run("curamoir.hooks.precompact", {"hook_event_name": "PreCompact", "trigger": "auto"}, repo)

    text = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "auto" in text


# --- SessionEnd ------------------------------------------------------------

def test_sessionend_finalises_the_handoff(tmp_path):
    repo = prepared(tmp_path)

    result = run("curamoir.hooks.sessionend", {
        "hook_event_name": "SessionEnd", "reason": "clear",
    }, repo)

    assert result.returncode == 0, result.stderr
    assert (repo / ".curamoir" / "handoff" / "latest.md").is_file()


def test_sessionend_stays_inside_its_one_and_a_half_second_budget(tmp_path):
    """SessionEnd hooks SHARE a 1.5s budget. Overrunning it does not merely slow
    exit; it risks the handoff never being written at all."""
    repo = prepared(tmp_path)
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    journal.write_text("\n".join(
        json.dumps({"at": "2026-10-06T10:00:00+00:00", "note": f"note {i}"})
        for i in range(500)
    ), encoding="utf-8")

    started = time.monotonic()
    result = run("curamoir.hooks.sessionend", {"hook_event_name": "SessionEnd"}, repo)
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert elapsed < 1.5, f"took {elapsed:.3f}s, over the shared SessionEnd budget"


def test_every_handoff_hook_survives_malformed_input(tmp_path):
    for module in ("curamoir.hooks.stop", "curamoir.hooks.precompact",
                   "curamoir.hooks.sessionend"):
        result = subprocess.run(
            [sys.executable, "-m", module], input="not json",
            capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        )
        assert result.returncode == 0, f"{module}: {result.stderr}"
