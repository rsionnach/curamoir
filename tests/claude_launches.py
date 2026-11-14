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
- **The argv** is each argument that becomes the program or its arguments:
  for `subprocess`, the first positional argument and the `args`,
  `executable` and `cmd` keywords; for `os.exec*` and `os.posix_spawn*`,
  every positional argument and the `path` and `argv` keywords; for
  `os.spawn*`, every positional argument after the mode; for `os.system`,
  `os.popen` and `asyncio.create_subprocess_shell`, the command line (the
  first argument, or `command` / `cmd`); for
  `asyncio.create_subprocess_exec`, every positional argument and
  `program`; for `pty.spawn`, the first argument (`argv`); for a curamoir
  launcher, the arguments bound to the parameters that flow into its own
  launcher's argv (by position, `self` aside, and by keyword). Each is
  followed back through the enclosing scope's assignments, and through
  `append`, `extend` and `insert` calls on a local name, so an argv built in
  a local and launched later is still seen. A scope is a function, or a
  module's top level with its class bodies.
- **A `claude` value** is a name or attribute whose name contains "claude"
  (`claude`, `CLAUDE`, `tools.claude`, `sweep_job.CLAUDE`), a string that
  is `claude`, a path ending in `/claude`, or a command line starting with
  `claude ` (for `os.system`), or the result of a call to a function that
  returns a value built from a `claude` value of its own (as fix_step_argv
  does). A call's result is made of its arguments only as far as the
  function's return is: all of them for a call that reshapes its argument
  (`shutil.which`, `os.path.*`, `str`, `list`, `shlex.split`, a `join`,
  `Path`, matched by the called name, so a method of that name counts too);
  the ones bound to the parameters a curamoir function's return is built
  from (`build(binary, p)` returning `[binary, "-p", p]`), found by
  fixpoint; and none for any other call, since what a function returns is
  not what it was given (`record_fixes(..., claude=...)` returns records,
  not the binary).

The limits, stated so they are not mistaken for proof: a static scan cannot
see a binary named at run time (`getattr`, a string built from parts, a
value read from a file), an argv that reaches a launcher through a
container's attribute or a standard-library call not in the pass-through
list, which element of a tuple a name is bound to, a launcher
imported under another name (`from subprocess import run as go`), or a
nested function's use of a name from the function around it. A name that
merely contains "claude" (a `claude_settings` path passed to git, say) is
reported too: rename it.
It is a tripwire for future jobs, not the boundary.
"""
import ast
import functools
import pathlib
import re
from typing import NamedTuple

#: The one module allowed to start `claude`.
SANCTIONED = "fix_step.py"

_SUBPROCESS = {"run", "Popen", "call", "check_call", "check_output", "getoutput",
               "getstatusoutput"}
_CLAUDE_STRING = re.compile(r"(?:.*/)?claude(?:\.exe)?(?:\s.*)?", re.DOTALL)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

#: Calls whose result is their argument, reshaped: a `claude` value passed to
#: one is still the binary. Matched by the called name, so `x.join(...)` and
#: `os.path.join(...)` alike.
_PASS_THROUGH = {"which", "join", "abspath", "realpath", "expanduser", "normpath", "fspath",
                 "str", "list", "tuple", "Path", "PurePath", "split", "format", "resolve"}

# A set of slots picks out some of a call's arguments: ("pos", i) the i-th
# positional argument, ("from", i) every positional argument from the i-th
# on, ("kw", name) a keyword argument, and ("kwrest", names) every keyword
# not in `names` (what a **kwargs parameter takes). Two tables map a
# function's name to slots, and the slots mean a different thing in each: in
# a launcher table, the arguments that become the process's argv; in the
# returner table, the arguments the function's result is built from.


def _stdlib_slots(func):
    """The argv slots of a standard-library launcher, or None if `func` is not one."""
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return None
    module, name = func.value.id, func.attr
    if module == "subprocess" and name in _SUBPROCESS:
        return {("pos", 0), ("kw", "args"), ("kw", "executable"), ("kw", "cmd")}
    if module == "os":
        if name.startswith(("exec", "posix_spawn")):
            return {("from", 0), ("kw", "path"), ("kw", "argv")}
        if name.startswith("spawn"):
            return {("from", 1)}
        if name == "system":
            return {("pos", 0), ("kw", "command")}
        if name == "popen":
            return {("pos", 0), ("kw", "cmd")}
    if module == "asyncio" and name == "create_subprocess_exec":
        return {("from", 0), ("kw", "program")}
    if module == "asyncio" and name == "create_subprocess_shell":
        return {("pos", 0), ("kw", "cmd")}
    if module == "pty" and name == "spawn":
        return {("pos", 0), ("kw", "argv")}
    return None


def _called_name(func):
    return func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)


def _slots(call, launchers):
    slots = _stdlib_slots(call.func)
    return slots if slots is not None else launchers.get(_called_name(call.func))


def _arguments_in(call, slots):
    """The call's arguments that sit in `slots`. A starred positional
    argument, or a **mapping, could fill any slot, so it always counts."""
    parts = [a.value for a in call.args if isinstance(a, ast.Starred)]
    plain = [a for a in call.args if not isinstance(a, ast.Starred)]
    for kind, where in slots:
        if kind == "pos" and where < len(plain):
            parts.append(plain[where])
        elif kind == "from":
            parts.extend(plain[where:])
    names = {where for kind, where in slots if kind == "kw"}
    bound = [where for kind, where in slots if kind == "kwrest"]
    parts.extend(k.value for k in call.keywords
                 if k.arg is None or k.arg in names
                 or any(k.arg not in named for named in bound))
    return parts


@functools.cache
def _scope_nodes(scope):
    """Every node in `scope` but those of the functions nested in it, which
    are scopes of their own. Cached: a parsed node is hashable by identity,
    and the scan never changes a tree."""
    found, todo = [], list(ast.iter_child_nodes(scope))
    while todo:
        node = todo.pop()
        found.append(node)
        if not isinstance(node, _FUNCTIONS):
            todo.extend(ast.iter_child_nodes(node))
    return tuple(found)


@functools.cache
def _local_sources(scope):
    """Each local name's assigned values (an assignment, a `for` or
    comprehension target, a walrus), and what is appended to it."""
    sources = {}

    def bind(target, value):
        # Every name in a tuple or list target takes the whole value: which
        # element it gets is not followed.
        for name in ast.walk(target):
            if isinstance(name, ast.Name):
                sources.setdefault(name.id, []).append(value)

    for node in _scope_nodes(scope):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node.value:
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                bind(target, node.value)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            bind(node.target, node.iter)
        if isinstance(node, ast.NamedExpr):
            bind(node.target, node.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend", "insert")
                and isinstance(node.func.value, ast.Name)):
            sources.setdefault(node.func.value.id, []).extend(node.args)
    return sources


def _value_nodes(expression, returners):
    """The nodes the value of `expression` is made of. Into a call it goes
    only as far as the call's result is made of its arguments: all of them
    for a pass-through, those in the slots `returners` gives for a curamoir
    function that returns a value built from its parameters, and none for
    any other call."""
    todo = [expression]
    while todo:
        node = todo.pop()
        yield node
        if isinstance(node, ast.Call):
            name = _called_name(node.func)
            if name not in _PASS_THROUGH:
                todo.append(node.func)
                if name in returners:  # the arguments its result is built from
                    todo.extend(_arguments_in(node, returners[name]))
                continue
        todo.extend(ast.iter_child_nodes(node))


def _built_from(expression, sources, returners):
    """`expression` and every expression its local names were built from."""
    found, todo, seen = [], [expression], set()
    while todo:
        current = todo.pop()
        found.append(current)
        for node in _value_nodes(current, returners):
            if isinstance(node, ast.Name) and node.id not in seen:
                seen.add(node.id)
                todo.extend(sources.get(node.id, []))
    return found


def _claude_value(expression, returners, builders=frozenset()):
    for node in _value_nodes(expression, returners):
        if isinstance(node, ast.Call) and _called_name(node.func) in builders:
            return _called_name(node.func) + "()"
        if isinstance(node, ast.Name) and "claude" in node.id.lower():
            return node.id
        if isinstance(node, ast.Attribute) and "claude" in node.attr.lower():
            return node.attr
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and _CLAUDE_STRING.fullmatch(node.value)):
            return repr(node.value)
    return None


def _scopes(trees):
    """(module, scope) for every function, and for each module's top level."""
    for module, tree in trees.items():
        yield module, tree
        for node in ast.walk(tree):
            if isinstance(node, _FUNCTIONS):
                yield module, node


def _launch_calls(scope, launchers):
    """(call, the arguments that become its argv) for each launcher call in `scope`."""
    for node in _scope_nodes(scope):
        if isinstance(node, ast.Call):
            slots = _slots(node, launchers)
            if slots:
                parts = _arguments_in(node, slots)
                if parts:
                    yield node, parts


def _parameter_slots(function):
    """{parameter name: its slots}, positions counted without self or cls."""
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]
    slots = {a.arg: {("pos", i), ("kw", a.arg)} for i, a in enumerate(positional)}
    slots.update({a.arg: {("kw", a.arg)} for a in arguments.kwonlyargs})
    if arguments.vararg:
        slots[arguments.vararg.arg] = {("from", len(positional))}
    if arguments.kwarg:
        # **kwargs takes every keyword that does not name a parameter.
        named = frozenset(a.arg for a in [*positional, *arguments.kwonlyargs])
        slots[arguments.kwarg.arg] = {("kwrest", named)}
    return slots


def _functions_outside_fix_step(trees):
    for module, scope in _scopes(trees):
        if module != SANCTIONED and isinstance(scope, _FUNCTIONS):
            yield scope


def _returns(function):
    return [node.value for node in _scope_nodes(function)
            if isinstance(node, ast.Return) and node.value is not None]


def _parameters_reached(function, expressions, returners):
    """The slots of `function`'s parameters that `expressions` are built from."""
    parameters = _parameter_slots(function)
    sources = _local_sources(function)
    reached = {node.id for expression in expressions
               for built in _built_from(expression, sources, returners)
               for node in _value_nodes(built, returners)
               if isinstance(node, ast.Name) and node.id in parameters}
    return set().union(*(parameters[name] for name in reached))


def _grow(found, name, slots):
    """Add `slots` to found[name]; True if that added anything."""
    if slots - found.get(name, set()):
        found[name] = found.get(name, set()) | slots
        return True
    return False


def _fixpoint(trees, step):
    """Run step(function, found) over every function outside fix_step.py
    until no step adds to `found`, a {name: set} table the step grows (with
    slots, or, for claude_builders, a marker); a function found late can make
    an earlier one a match, hence the repeat."""
    found = {}
    changed = True
    while changed:
        changed = False
        for function in _functions_outside_fix_step(trees):
            changed |= step(function, found)
    return found


def value_returners(trees):
    """{name: slots} for the curamoir functions, outside fix_step.py, whose
    return value is built from one of their parameters: a call to one is
    made of the arguments in those slots (`build(binary, p)` returning
    `[binary, "-p", p]`), and of nothing else it was given."""
    return _fixpoint(trees, lambda function, found: _grow(
        found, function.name, _parameters_reached(function, _returns(function), found)))


def argv_launchers(trees, returners=None):
    """{name: argv slots} for the curamoir functions, outside fix_step.py,
    that pass one of their parameters into a launcher's argv. Functions that
    share a name share their slots."""
    returners = value_returners(trees) if returners is None else returners

    def step(function, found):
        argv = [part for _call, parts in _launch_calls(function, found) for part in parts]
        return _grow(found, function.name, _parameters_reached(function, argv, returners))

    return _fixpoint(trees, step)


def claude_builders(trees, returners=None):
    """The names of functions, outside fix_step.py, that return a value
    built from a `claude` value of their own (sweep_job's `_claude`, or
    fix_step_argv if it lived anywhere else): a call to one is a `claude`
    value too."""
    returners = value_returners(trees) if returners is None else returners

    def step(function, found):
        sources = _local_sources(function)
        builds = any(_claude_value(built, returners, found) for value in _returns(function)
                     for built in _built_from(value, sources, returners))
        # Only the names matter here; the marker keeps the {name: set} shape
        # _fixpoint grows, and the set of names is returned.
        return builds and _grow(found, function.name, {"returns claude"})

    return set(_fixpoint(trees, step))


class Hit(NamedTuple):
    """A call outside fix_step.py whose argv is built from a `claude` value."""

    module: str
    line: int
    launcher: str
    value: str


def claude_launches(trees):
    """A Hit for each call outside fix_step.py whose argv is built from a
    `claude` value, in module and line order."""
    returners = value_returners(trees)
    launchers = argv_launchers(trees, returners)
    builders = claude_builders(trees, returners)
    hits = set()
    for module, scope in _scopes(trees):
        if module == SANCTIONED:
            continue
        sources = _local_sources(scope)
        for call, argv in _launch_calls(scope, launchers):
            for built in (b for part in argv for b in _built_from(part, sources, returners)):
                value = _claude_value(built, returners, builders)
                if value:
                    hits.add(Hit(module, call.lineno, _called_name(call.func), value))
                    break
    return sorted(hits)


def package_trees(root):
    """{path relative to root: parsed module} for every .py file under root."""
    root = pathlib.Path(root)
    return {path.relative_to(root).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.py"))}
