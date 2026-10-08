"""What cúramóir writes about a session is as sensitive as the session was.

`write_artefact` redacts, which covers the handoff and the brief. The journal
did not go through it: `record_turn` wrote `json.dumps(entry)` directly, with
the Stop hook passing the assistant's last message verbatim, every turn, with
no length bound — into a directory `init` does not gitignore. So a token the
agent mentioned in passing was redacted out of latest.md and written in full
to journal.jsonl, one directory away.
"""

import json

from curamoir.handoff import build_handoff, record_turn
from curamoir.redaction import redact
from curamoir.scaffold import init_repo

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


def initialised(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


def test_an_unterminated_private_key_block_is_still_redacted():
    """The pattern required the END marker, so a key shown as a file excerpt or
    a diff hunk — the realistic way one reaches a brief — passed through whole."""
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxGpV3kR2mJ9pQ7dL4nB8sT1vY6wZ0cH5fX2gK7jM9aO3bN4e\n"
        "QsR8tU1vW2xY3zA4bC5dE6fG7hI8jK9lM0nO1pQ2rS3tU4vW5xY6zA7bC8dE9fG0\n"
    )

    redacted = redact(text)

    assert "MIIEowIBAAKCAQEAxGpV3kR2mJ9" not in redacted, "key material survived"


def test_a_terminated_private_key_block_is_still_redacted_as_one_unit():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxGpV3kR2mJ9pQ7dL4nB8sT1vY6wZ\n"
        "-----END RSA PRIVATE KEY-----\n"
    )

    assert "MIIEowIBAAKCAQEAxGpV3kR2mJ9" not in redact(text)


def test_the_journal_is_redacted_on_the_way_to_disk(tmp_path):
    """Written every turn by the Stop hook, and the one artefact that bypassed
    E0-R7's single write path."""
    repo = initialised(tmp_path)

    record_turn(repo, note=f"I set GITHUB_TOKEN={TOKEN} in .env for you.")

    journal = (repo / ".curamoir" / "handoff" / "journal.jsonl").read_text(encoding="utf-8")
    assert TOKEN not in journal


def test_a_journal_note_is_bounded(tmp_path):
    """The renderer only ever uses the first 60 words, but the hook stored the
    whole assistant message: one real Stop call wrote a 99,046-character line,
    and nothing anywhere prunes the file."""
    repo = initialised(tmp_path)

    record_turn(repo, note="word " * 20_000)

    line = (repo / ".curamoir" / "handoff" / "journal.jsonl").read_text(encoding="utf-8")
    assert len(line) < 4_000, f"stored {len(line)} characters for one turn"


def test_one_malformed_journal_line_does_not_discard_the_others(tmp_path):
    """`_journal` returned [] on any ValueError, so a single torn line cost
    every entry — including the next step, which E8-R1 calls required."""
    repo = initialised(tmp_path)
    for index in range(3):
        record_turn(repo, note=f"turn {index}")
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"at": "2026-10-08T10:00:00+00:00", "note": "tor\n')
    record_turn(repo, note="turn after the tear")

    handoff = build_handoff(repo)

    assert handoff is not None
    rendered = json.dumps(handoff)
    assert "turn after the tear" in rendered or "turn 2" in rendered, (
        "a single malformed line emptied the journal"
    )


def test_init_keeps_the_journal_and_lock_out_of_git(tmp_path):
    """The journal holds verbatim session text and the lock holds a pid: both
    are machine-local state, and `.curamoir/` is otherwise meant to be
    committed."""
    repo = initialised(tmp_path)

    ignore = (repo / ".curamoir" / ".gitignore").read_text(encoding="utf-8")

    assert "journal.jsonl" in ignore
    assert "lock" in ignore
