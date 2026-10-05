"""E0-R8: least privilege for the CI workflow.

The workflow template requests only `contents: write`, `pull-requests: write`
and `issues: write`. No org-wide tokens.

This module owns the constraint and the check. The template itself is E10-R2's
to author — and Epic 10 is precisely where the pressure to add "just one more
scope" will come from, which is why the assertion lives here with the other
guardrails rather than only in Epic 10's own tests.

Deliberately dependency-free: parsing the handful of lines a permissions block
can contain does not justify making PyYAML a hard dependency of the guardrail
layer, which E2-R5 wants runnable standalone.
"""

import re

ALLOWED_PERMISSIONS = {
    "contents": "write",
    "pull-requests": "write",
    "issues": "write",
}

# Write is the widest any scope may be; read is narrower and therefore fine.
_RANK = {"none": 0, "read": 1, "write": 2}


def check_workflow_permissions(workflow_text):
    """Problems with a workflow's permissions block. Empty list means it passes.

    Returns strings rather than raising so a caller can report every problem at
    once instead of one per run.
    """
    block_match = re.search(
        r"^permissions:[ \t]*(\S.*)?$", workflow_text, re.MULTILINE
    )
    if block_match is None:
        return [
            "no permissions block: the workflow would inherit the repository "
            "default, which may be far wider than the three scopes cúramóir "
            "needs. Declare them explicitly."
        ]

    inline = (block_match.group(1) or "").strip()
    if inline:
        return [
            f"permissions is set to {inline!r}; cúramóir requires an explicit "
            "per-scope block limited to "
            + ", ".join(f"{k}: {v}" for k, v in ALLOWED_PERMISSIONS.items())
        ]

    problems = []
    for scope, value in _parse_scopes(workflow_text, block_match.end()):
        if scope not in ALLOWED_PERMISSIONS:
            problems.append(
                f"permission {scope!r} is outside the allowed set "
                f"({', '.join(sorted(ALLOWED_PERMISSIONS))}); cúramóir does not "
                "need it and must not request it."
            )
            continue
        if _RANK.get(value, 99) > _RANK[ALLOWED_PERMISSIONS[scope]]:
            problems.append(
                f"permission {scope!r} is {value!r}, wider than the allowed "
                f"{ALLOWED_PERMISSIONS[scope]!r}."
            )
    return problems


def _parse_scopes(text, start):
    """The `scope: value` pairs indented under the permissions block."""
    for line in text[start:].splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line[:1].isspace():
            break  # dedented: the block has ended
        pair = re.match(r"\s+([A-Za-z0-9\-_]+):\s*(\S+)", line)
        if pair:
            yield pair.group(1), pair.group(2).strip().strip("\"'")
