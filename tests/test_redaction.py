"""E0-R7: nothing cúramóir writes carries a secret.

Briefs, handoffs, issues and PR bodies all pass through here. The redactor is
the only write path for those four artefact kinds, so a later epic cannot
forget to call it.

Two honest limits, stated here and in docs/security.md (E13-R2b):
pattern-based redaction catches shapes it knows, and an unrecognised secret
format passes through. It reduces exposure; it does not guarantee its absence.
"""

from curamoir.redaction import redact, write_artefact


def test_an_anthropic_key_is_redacted():
    text = "export ANTHROPIC_API_KEY=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    assert "sk-ant-api03-AAAA" not in redact(text)
    assert "REDACTED" in redact(text)


def test_a_github_token_is_redacted():
    assert "ghp_" not in redact("token: ghp_0123456789abcdefghijklmnopqrstuvwxyz")


def test_an_aws_access_key_is_redacted():
    assert "AKIA" not in redact("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")


def test_a_bearer_token_is_redacted():
    assert "eyJhbGciOi" not in redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")


def test_an_assignment_to_a_secret_looking_name_is_redacted():
    for line in [
        'password = "hunter2hunter2"',
        "api_key: abcdef1234567890abcdef",
        "SECRET_TOKEN=zzzzzzzzzzzzzzzzzzzz",
    ]:
        assert "REDACTED" in redact(line), line


def test_ordinary_prose_is_left_alone():
    """A redactor that mangles briefs is a redactor people switch off."""
    text = (
        "Fixed 5 structured-logging violations in nthlayer_common/telemetry. "
        "Grade moved C to B because open P1 findings fell to zero."
    )

    assert redact(text) == text


def test_redaction_is_idempotent():
    once = redact("key: ghp_0123456789abcdefghijklmnopqrstuvwxyz")

    assert redact(once) == once


def test_a_url_password_is_redacted():
    assert "s3cr3t" not in redact("postgres://user:s3cr3t@db.example.com:5432/app")


def test_the_surrounding_line_is_preserved_so_the_artefact_stays_readable():
    """Redaction must leave enough context that the reader knows WHAT was
    removed. A brief that says only [REDACTED] is not actionable."""
    result = redact("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")

    assert "aws_access_key_id" in result


def test_a_realistic_brief_survives_redaction_unchanged():
    """False positives are the real risk: a redactor that mangles briefs is a
    redactor people switch off. This is the shape of an actual away-brief."""
    brief = """# cúramóir brief — 2026-10-05

## Since you were here
14 days away. 2 sweeps, 1 review, 1 smoke run. Spend this period: $1.40.

## What changed
- curamoir/sweep-2026-09-28 merged: 5 structured-logging fixes.
- curamoir/sweep-2026-10-05 open: 3 fixes, awaiting review.

## What was found
correctness: 1 P1 in nthlayer_common/telemetry (unbounded retry).
edge_cases: 2 P2 in nthlayer_workers/registry.

## Grades
| module | grade | delta | why |
|---|---|---|---|
| nthlayer_common | B | +1 | open P1 findings fell to zero |

## Blocked on you
promote: structured-logging to rule (3 fixes in 30 days).

## Start here
cm-7qh.4 — kill switch, est. 90 min. Lowest-numbered ready bead on the
critical path, and it unblocks the runner.
"""

    assert redact(brief) == brief


def test_a_token_mentioned_without_a_value_is_not_mangled():
    for line in [
        "The access_token field is documented in docs/security.md",
        "Set your api_key in the environment, never in config.yaml",
        "token budgets are advisory, not enforced",
    ]:
        assert redact(line) == line, line


def test_write_artefact_is_the_redacting_write_path(tmp_path):
    """E0-R7 asks that the redactor be the only write path for briefs,
    handoffs, issues and PR bodies, so a later epic cannot forget it."""
    target = tmp_path / "briefs" / "latest.md"

    write_artefact(target, "deploy key: ghp_0123456789abcdefghijklmnopqrstuvwxyz")

    written = target.read_text(encoding="utf-8")
    assert "ghp_" not in written
    assert "REDACTED" in written


def test_a_password_containing_an_ampersand_is_still_redacted():
    """Excluding & and ; from the value class to stop URL over-redaction meant a
    secret containing either was truncated — and one whose leading part fell
    under the 8-character floor was not redacted at all. `&` is a common
    password character, so this was more likely than the case it fixed, and the
    brief it leaks into may be published as a public issue (E9-R3)."""
    assert "s3cr3t" not in redact('password = "s3cr3t&p4ssw0rd!"')
    assert "secretpart" not in redact("PASSWORD=abcdefgh&secretpart")
    assert "bbbbbbbbbbbb" not in redact("api_token=aaaaaaaa;bbbbbbbbbbbb")


def test_a_query_string_is_not_swallowed_past_the_secret():
    """The case the broken fix was reaching for: redact the key's value without
    eating the rest of the query string."""
    result = redact("url=https://api.example.com/v1?api_key=abcdefghij&page=2&sort=asc")

    assert "abcdefghij" not in result
    assert "page=2" in result
    assert "sort=asc" in result


def test_query_and_generic_redaction_are_both_idempotent():
    for text in [
        "url=https://x/v1?api_key=abcdefghij&page=2",
        'password = "s3cr3t&p4ssw0rd!"',
        "api_token=aaaaaaaa;bbbbbbbbbbbb",
    ]:
        once = redact(text)
        assert redact(once) == once, text
