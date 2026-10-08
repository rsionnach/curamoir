"""cúramóir's Claude Code hooks: one module per hook event, plus the command
policy they share.

The event modules are addressed by module path from `scripts/curamoir-hook-*`
(`python3 -m curamoir.hooks.pretooluse`), so they are deliberately NOT
re-exported here — a move would silently break the wrappers. Unlike `guards/`,
these are plain functions rather than `JobContext` callables: a hook is not a
gateway guard and never runs behind one.
"""

from curamoir.hooks._entry import in_curamoir_job
from curamoir.hooks.policy import judge_command
from curamoir.hooks.verdict import Category, Decision, Rule

__all__ = ["Category", "Decision", "Rule", "in_curamoir_job", "judge_command"]
