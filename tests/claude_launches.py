"""No module but fix_step.py names the `claude` binary, except where an
allow-list entry says why (cm-c60, user decision 2026-11-14).

The refusal in fix_step.fix_step_argv (no shell-capable tool without an OS
sandbox or a hosted runner) holds only for a `claude -p` that fix_step
starts. A job that started `claude` some other way would get Claude Code's
default toolset, Bash included, whatever its flags. Following every way an
argv can reach a process cannot be done statically, so this scan keys on
the one thing every such launch needs: the binary's name, written somewhere
in src/curamoir/.

**The rule.** Every str and bytes constant under src/curamoir/, the literal
parts of f-strings included and docstrings left out, is checked. One is
flagged if it is exactly `claude`, ends in `/claude`, or holds `claude` as a
command word followed by a flag or by the end of the string (`claude -p`,
`timeout 60 claude -p`, `env X=1 claude`). Words that only contain it
(`claude_settings`, `.claude/settings.json`, `code.claude.com`,
`claude-code`) are not, and nor is a lone dash after it (`claude - the
agent`). A sentence that ends in the word (`could not reach claude`) is
flagged too, since it reads as a command word at the end; so is an
f-string whose literal part ends in it (`f"the claude {kind} binary"`
checks `"the claude "`): rephrase it, or give it an entry.

**The allow-list.** fix_step.py is allowed whole: it is where the agent is
started. Anything else needs an entry in ALLOWED: the file, the qualified
name of the enclosing function or class (for a parameter default or a
string inside one) or the module-level constant, how many mentions it
covers, and a one-line reason. There is no
data-flow tracking. Adding an entry is the review point: the person or gate
that adds one judges whether the new caller still starts the agent only
through fix_step.run_fix_step.

**The limits.** A name the scan cannot read is not seen: one built at run
time (`"clau" + "de"`, `getattr`), or read from config or the environment.
Those are deliberate, not the accident a future job would make. A new shape
found later goes into this list as a follow-up bead, not into the rule.
"""
import ast
import pathlib
import re
from typing import NamedTuple

#: The one module that may name the binary anywhere: it starts the agent.
SANCTIONED = "fix_step.py"

#: (file under src/curamoir/, qualified name of the enclosing function or
#: class, or the module-level constant) -> (how many mentions it covers, why
#: it may name the binary). The count is exact: a second mention added in an
#: allowed function (a direct launch beside its default) fails, and so does
#: an entry that matches fewer mentions than it says (a stale one), so the
#: list never silently allows a later string.
ALLOWED = {
    ("sweep_job.py", "CLAUDE"): (
        1, "the default binary, handed to run_fix_step as tools.claude; never launched here"),
    ("tier1.py", "_call_scan"): (
        1, "the claude= default it passes on to run_fix_step"),
    ("tier1.py", "run_tier1_scan"): (
        1, "the claude= default it passes on to run_fix_step through _call_scan"),
}

#: `claude` as a command word: at the start or after a space or a shell
#: separator, and followed by a flag (a dash and then a letter or digit, so
#: prose like "claude - the agent" is not one) or by the end.
_COMMAND_WORD = re.compile(
    r"(?:^|[\s;&|(`'\"])claude(?:\.exe)?(?=\s+--?[A-Za-z0-9]|\s*$)")


def names_claude(value):
    """True for a constant that names the binary: exactly `claude`, a path
    ending in `/claude`, or `claude` as a command word before a flag or the
    end."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return (value == "claude" or value.endswith("/claude")
            or bool(_COMMAND_WORD.search(value)))


class Mention(NamedTuple):
    """A constant outside fix_step.py that names the binary; `shown` is its
    repr, as a failure message prints it."""

    module: str
    line: int
    owner: str
    shown: str


def _docstrings(tree):
    """The constants that are docstrings: the first statement of a module,
    class or function, when it is a bare string."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.add(first.value)
    return found


def _owned_constants(tree):
    """(owner, constant) for every constant in `tree`: the owner is the
    qualified name of the enclosing functions and classes (`run_tier1_scan`,
    `Job.go`, `outer.inner`), or, at module level, the name being assigned
    (`CLAUDE = "claude"`), else "<module>". Qualified, so an entry for one
    function does not cover a same-named method elsewhere in the file."""
    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = child.name if owner == "<module>" else f"{owner}.{child.name}"
                yield from visit(child, qualified)
                continue
            inner = owner
            if owner == "<module>" and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                inner = names[0] if names else owner
            if isinstance(child, ast.Constant):
                yield owner, child
            yield from visit(child, inner)

    yield from visit(tree, "<module>")


def claude_mentions(trees):
    """A Mention for each constant outside fix_step.py that names the binary,
    allowed or not, in module and line order."""
    found = []
    for module, tree in trees.items():
        if module == SANCTIONED:
            continue
        docstrings = _docstrings(tree)
        for owner, constant in _owned_constants(tree):
            if (isinstance(constant.value, (str, bytes)) and constant not in docstrings
                    and names_claude(constant.value)):
                found.append(Mention(module, constant.lineno, owner, repr(constant.value)))
    return sorted(found)


def _by_owner(trees):
    owners = {}
    for mention in claude_mentions(trees):
        owners.setdefault((mention.module, mention.owner), []).append(mention)
    return owners


def unallowed(trees, allowed=None):
    """The mentions ALLOWED (or `allowed`) does not cover: every mention of
    an owner with no entry, and every mention of an owner that has more than
    its entry's count (which one is new cannot be told, so all are shown)."""
    allowed = ALLOWED if allowed is None else allowed
    return sorted(m for key, mentions in _by_owner(trees).items()
                  if len(mentions) > allowed.get(key, (0, ""))[0] for m in mentions)


def stale(trees, allowed=None):
    """The allow-list entries that match fewer mentions than their count."""
    allowed = ALLOWED if allowed is None else allowed
    owners = _by_owner(trees)
    return sorted(key for key, (count, _reason) in allowed.items()
                  if len(owners.get(key, [])) < count)


def failure_message(mentions):
    lines = [f"  {m.module}:{m.line} in {m.owner}: {m.shown}" for m in mentions]
    return ("These name the claude binary outside fix_step.py:\n" + "\n".join(lines) + "\n"
            "Start the agent through fix_step.run_fix_step, which builds its argv with the "
            "refusals. If this is a new caller's default for a binary it hands to "
            "run_fix_step, add an entry to ALLOWED in tests/claude_launches.py with "
            "its count and a one-line reason.")


def package_trees(root):
    """{path relative to root: parsed module} for every .py file under root."""
    root = pathlib.Path(root)
    return {path.relative_to(root).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.py"))}
