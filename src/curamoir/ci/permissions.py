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
    """Problems with EVERY permissions block in a workflow. Empty list passes.

    Every block, at any indentation — not just the workflow-level one. A
    job-level `permissions:` OVERRIDES the workflow-level block in GitHub
    Actions, so checking only column zero meant `id-token: write` — the scope
    that unlocks OIDC cloud credentials — could be added by indenting four
    lines past the one assertion placed here to prevent exactly that.

    Returns strings rather than raising so a caller can report every problem at
    once instead of one per run.
    """
    blocks = list(re.finditer(r"^([ \t]*)permissions:[ \t]*(\S.*)?$", workflow_text, re.MULTILINE))
    if not blocks:
        return [
            "no permissions block: the workflow would inherit the repository "
            "default, which may be far wider than the three scopes cúramóir "
            "needs. Declare them explicitly."
        ]

    problems = []
    for block in blocks:
        indent = len(block.group(1).expandtabs(8))
        where = "workflow-level" if indent == 0 else f"a nested (job-level) block at column {indent}"
        inline = (block.group(2) or "").strip()
        if inline:
            problems.append(
                f"{where} permissions is set to {inline!r}; cúramóir requires an "
                "explicit per-scope block limited to "
                + ", ".join(f"{k}: {v}" for k, v in ALLOWED_PERMISSIONS.items())
            )
            continue
        for scope, value in _parse_scopes(workflow_text, block.end(), indent):
            if scope not in ALLOWED_PERMISSIONS:
                problems.append(
                    f"permission {scope!r} in {where} is outside the allowed set "
                    f"({', '.join(sorted(ALLOWED_PERMISSIONS))}); cúramóir does "
                    "not need it and must not request it."
                )
                continue
            if _RANK.get(value, 99) > _RANK[ALLOWED_PERMISSIONS[scope]]:
                problems.append(
                    f"permission {scope!r} in {where} is {value!r}, wider than "
                    f"the allowed {ALLOWED_PERMISSIONS[scope]!r}."
                )
    return problems


def _parse_scopes(text, start, block_indent=0):
    """The `scope: value` pairs nested under a permissions block.

    `block_indent` is the column of the `permissions:` key itself, so a
    job-level block ends at the next line indented no further than it.
    """
    for line in text[start:].splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= block_indent:
            break  # dedented to or past the block's own level: it has ended
        pair = re.match(r"\s+([A-Za-z0-9\-_]+):\s*(\S+)", line)
        if pair:
            yield pair.group(1), pair.group(2).strip().strip("\"'")
