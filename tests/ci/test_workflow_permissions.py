"""E0-R8: the CI workflow requests only the three permissions it needs.

The permissions block itself lands in templates/workflows/curamoir.yml, which
E10-R2 authors. This bead owns the CONSTRAINT and the assertion that guards it,
so a later edit to the template cannot quietly widen scope.

The assertion lives with the guardrails rather than only in Epic 10's tests,
for exactly that reason: Epic 10 is where the pressure to add "just one more
scope" will come from.
"""

import pytest

from curamoir.ci.permissions import ALLOWED_PERMISSIONS, check_workflow_permissions


def test_the_allowed_set_is_exactly_the_three_from_the_spec():
    assert ALLOWED_PERMISSIONS == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
    }


def test_a_workflow_with_exactly_those_permissions_passes():
    workflow = """
name: curamoir
permissions:
  contents: write
  pull-requests: write
  issues: write
on:
  schedule:
    - cron: "0 3 * * 1"
"""
    assert check_workflow_permissions(workflow) == []


def test_a_widened_scope_is_rejected_and_named():
    workflow = """
permissions:
  contents: write
  pull-requests: write
  issues: write
  packages: write
"""
    problems = check_workflow_permissions(workflow)

    assert len(problems) == 1
    assert "packages" in problems[0]


def test_write_all_is_rejected():
    """`permissions: write-all` is the shape this guardrail exists to stop."""
    problems = check_workflow_permissions("permissions: write-all\n")

    assert problems
    assert any("write-all" in problem for problem in problems)


def test_a_missing_permissions_block_is_rejected():
    """No block means the workflow inherits the repository default, which may be
    far wider than these three. Silence is not least privilege."""
    problems = check_workflow_permissions("name: curamoir\non:\n  schedule: []\n")

    assert problems
    assert any("no permissions block" in problem.lower() for problem in problems)


def test_escalating_an_allowed_permission_beyond_write_is_rejected():
    problems = check_workflow_permissions(
        "permissions:\n  contents: admin\n  pull-requests: write\n  issues: write\n"
    )

    assert problems
    assert any("contents" in problem for problem in problems)


def test_narrower_than_required_is_allowed():
    """Read-only is narrower, not wider. A guardrail against over-reach must not
    also forbid under-reach, or it stops being about least privilege."""
    problems = check_workflow_permissions(
        "permissions:\n  contents: read\n  pull-requests: write\n  issues: write\n"
    )

    assert problems == []


def test_the_shipped_template_satisfies_the_constraint():
    """Skipped until E10-R2 authors the template. When it exists this test is
    the thing that keeps it honest."""
    from pathlib import Path

    template = Path(__file__).resolve().parents[2] / "templates" / "workflows" / "curamoir.yml"
    if not template.exists():
        pytest.skip("templates/workflows/curamoir.yml not authored yet (E10-R2)")

    assert check_workflow_permissions(template.read_text(encoding="utf-8")) == []
