"""A dependency that is present but not working must not deny everything.

Every case here is a tool or a file that exists and misbehaves, which is harder
than absence: absence is checked for, misbehaviour is assumed away. The shared
failure shape is a guard that answers "deny" or "nothing" when the honest answer
is "I could not tell", and the consequence is either a guard that blocks all
work — which gets switched off — or an injection that silently vanishes.
"""

import os
import stat

import pytest

from curamoir.ci.permissions import check_workflow_permissions
from curamoir.hooks.pretooluse import _delegate_to_dcg
from curamoir.hooks.sessionstart import build_context, token_budget_for
from curamoir.scaffold import init_repo


@pytest.fixture
def fake_dcg(tmp_path, monkeypatch):
    """A `dcg` on PATH that runs and fails for its own reasons."""
    def install(exit_code, message):
        directory = tmp_path / "fakebin"
        directory.mkdir(exist_ok=True)
        script = directory / "dcg"
        script.write_text(
            f'#!/bin/sh\necho "{message}" >&2\nexit {exit_code}\n', encoding="utf-8"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
        monkeypatch.setenv("CURAMOIR_USE_DCG", "1")
    return install


def test_a_dcg_that_cannot_reach_a_verdict_falls_back_rather_than_denying(fake_dcg):
    """Exit 2 with a usage error on stderr is "I do not understand you", not
    "this command is dangerous". Read as a refusal, a dcg one version out of
    date denied `git status`, `git add -A` and `pytest -q` alike."""
    fake_dcg(2, 'error: unknown subcommand "check"')

    assert _delegate_to_dcg("git status") is None


def test_a_dcg_that_refuses_with_output_is_still_honoured(fake_dcg):
    """The fallback must not become a way to ignore dcg."""
    fake_dcg(3, "dcg: rm -rf is destructive")

    decision = _delegate_to_dcg("rm -rf /")

    assert decision is not None and decision.deny


@pytest.mark.parametrize("key", ['"id-token"', "'id-token'"])
def test_a_quoted_scope_key_does_not_evade_the_permissions_assertion(key):
    """The one assertion guarding against OIDC cloud credentials required an
    UNQUOTED key, and a non-matching line was skipped in silence — so two quote
    characters defeated it, exactly as indentation used to."""
    workflow = (
        "name: x\npermissions:\n  contents: read\n"
        f"  {key}: write\njobs: {{}}\n"
    )

    assert check_workflow_permissions(workflow), f"{key} passed the check"


def test_a_scope_line_the_parser_cannot_read_is_reported_not_skipped():
    """Skipping an unparseable line inside a permissions block is how an
    evasion stays invisible."""
    workflow = "name: x\npermissions:\n  contents: read\n  this line has no colon\njobs: {}\n"

    assert check_workflow_permissions(workflow)


def test_a_tiny_configured_budget_falls_back_rather_than_emitting_a_fragment(tmp_path):
    """`session_start_token_budget: 1` survived validation, drove `room`
    negative, truncated every block to empty and then sliced the header
    mid-word: four characters of mojibake, with no pointer to the files."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    config = tmp_path / ".curamoir" / "curamoir.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "session_start_token_budget: 1500", "session_start_token_budget: 1"
        ),
        encoding="utf-8",
    )

    assert token_budget_for(tmp_path) >= 100


@pytest.mark.parametrize("budget", [1, True, 0.5])
def test_an_unusable_budget_value_is_not_taken_literally(budget, monkeypatch):
    from curamoir.hooks import sessionstart

    monkeypatch.setattr(sessionstart, "config_value", lambda *a, **k: budget)

    assert sessionstart.token_budget_for("ignored") >= 100


def test_a_handoff_with_invalid_utf8_still_injects_something(tmp_path):
    """`_read` catches OSError, but `read_text` raises UnicodeDecodeError — a
    ValueError — for a write torn mid-codepoint. `main`'s blanket except then
    swallowed it, so SessionStart injected nothing and said nothing."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    latest = tmp_path / ".curamoir" / "handoff" / "latest.md"
    latest.write_bytes(b"# cu\xffram\xf3ir handoff\n\n- **branch:** main\n")

    context = build_context(tmp_path)

    assert context and "handoff" in context.lower()
