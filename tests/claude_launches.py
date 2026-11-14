"""Find every place outside fix_step.py where the `claude` binary reaches a
process launcher (cm-c60, user decision 2026-11-14).

The refusal in fix_step.fix_step_argv (no shell-capable tool without an OS
sandbox or a hosted runner) holds only for a `claude -p` that fix_step
starts. A job that started `claude` some other way would get Claude Code's
default toolset, Bash included, whatever its flags. So this scan keys on the
binary, not on the flags: no module but fix_step.py may hand a `claude`
value to anything that starts a process.

What counts:

- **A launcher** is a call that starts a process from the standard library
  (`subprocess.run`, `Popen`, `call`, `check_call`, `check_output`,
  `getoutput`, `getstatusoutput`; `os.exec*`, `os.spawn*`, `os.posix_spawn*`,
  `os.system`, `os.popen`; `asyncio.create_subprocess_*`; `pty.spawn`), or a
  curamoir function that passes one of its own parameters into a launcher's
  argv (`git_guard.run_in_group` is one), found by fixpoint, so a wrapper of
  a wrapper counts too. fix_step.py's own functions never count: calling
  run_fix_step is the sanctioned way to start the agent.
- **The argv** is the call's first positional argument, or its `args`,
  `argv`, `cmd` or `command` keyword. It is followed back through the
  enclosing function's assignments, and through `append`, `extend` and
  `insert` calls on a local name, so an argv built in a local and launched
  later is still seen.
- **A `claude` value** is a name or attribute whose name contains "claude"
  (`claude`, `CLAUDE`, `tools.claude`, `sweep_job.CLAUDE`), or a string that
  is `claude`, a path ending in `/claude`, or a command line starting with
  `claude ` (for `os.system`).

The limits, stated so they are not mistaken for proof: a static scan cannot
see a binary named at run time (`getattr`, a string built from parts, a
value read from a file) or an argv built in one function and launched in
another through a curamoir helper that takes it under some other argument.
It is a tripwire for future jobs, not the boundary.
"""
import ast
import pathlib
import re

#: The one module allowed to start `claude`.
SANCTIONED = "fix_step.py"

_SUBPROCESS = {"run", "Popen", "call", "check_call", "check_output", "getoutput",
               "getstatusoutput"}
_ARGV_KEYWORDS = ("args", "argv", "cmd", "command")
_CLAUDE_STRING = re.compile(r"(?:.*/)?claude(?:\.exe)?(?:\s.*)?", re.DOTALL)


def _stdlib_launcher(func):
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return False
    module, name = func.value.id, func.attr
    return ((module == "subprocess" and name in _SUBPROCESS)
            or (module == "os" and (name.startswith(("exec", "spawn", "posix_spawn"))
                                    or name in ("system", "popen")))
            or (module == "asyncio" and name.startswith("create_subprocess"))
            or (module == "pty" and name == "spawn"))


def _called_name(func):
    return func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)


def _argv(call):
    for keyword in call.keywords:
        if keyword.arg in _ARGV_KEYWORDS:
            return keyword.value
    return call.args[0] if call.args else None


def _local_sources(function):
    """Each local name's assigned values, and what is appended to it."""
    sources = {}
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node.value:
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(target, ast.Name):
                    sources.setdefault(target.id, []).append(node.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend", "insert")
                and isinstance(node.func.value, ast.Name)):
            sources.setdefault(node.func.value.id, []).extend(node.args)
    return sources


def _built_from(expression, sources):
    """`expression` and every expression its local names were built from."""
    found, todo, seen = [], [expression], set()
    while todo:
        current = todo.pop()
        found.append(current)
        for node in ast.walk(current):
            if isinstance(node, ast.Name) and node.id not in seen:
                seen.add(node.id)
                todo.extend(sources.get(node.id, []))
    return found


def _claude_value(expression):
    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and "claude" in node.id.lower():
            return node.id
        if isinstance(node, ast.Attribute) and "claude" in node.attr.lower():
            return node.attr
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and _CLAUDE_STRING.fullmatch(node.value)):
            return repr(node.value)
    return None


def _functions(trees):
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield module, node


def _launch_calls(function, launchers):
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and (_stdlib_launcher(node.func)
                                           or _called_name(node.func) in launchers):
            argv = _argv(node)
            if argv is not None:
                yield node, argv


def argv_launchers(trees):
    """The names of curamoir functions, outside fix_step.py, that pass one of
    their parameters into a launcher's argv."""
    found = set()
    changed = True
    while changed:
        changed = False
        for module, function in _functions(trees):
            if module == SANCTIONED or function.name in found:
                continue
            arguments = function.args
            parameters = {a.arg for a in [*arguments.posonlyargs, *arguments.args,
                                          *arguments.kwonlyargs]} - {"self", "cls"}
            if arguments.vararg:
                parameters.add(arguments.vararg.arg)
            sources = _local_sources(function)
            for _call, argv in _launch_calls(function, found):
                if any(isinstance(node, ast.Name) and node.id in parameters
                       for part in _built_from(argv, sources) for node in ast.walk(part)):
                    found.add(function.name)
                    changed = True
                    break
    return found


def claude_launches(trees):
    """(module, line, launcher, the claude value) for each call outside
    fix_step.py whose argv is built from a `claude` value."""
    launchers = argv_launchers(trees)
    hits = []
    for module, function in _functions(trees):
        if module == SANCTIONED:
            continue
        sources = _local_sources(function)
        for call, argv in _launch_calls(function, launchers):
            for part in _built_from(argv, sources):
                value = _claude_value(part)
                if value:
                    hits.append((module, call.lineno, _called_name(call.func), value))
                    break
    return sorted(set(hits))


def package_trees(root):
    """{path relative to root: parsed module} for every .py file under root."""
    root = pathlib.Path(root)
    return {path.relative_to(root).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.py"))}
