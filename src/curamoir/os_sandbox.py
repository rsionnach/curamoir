"""The operating-system sandbox the E3-R4 test run executes in (cm-p2f).

The test gate (green_gate) runs the project's test command on code the fix
agent just edited: without an OS sandbox that is the agent getting a shell
by another route. wrap() prefixes a command's argv so the kernel confines
it: writes only inside the sandbox clone and the run's own directory (its
HOME and TMPDIR), no network beyond loopback, and no host Unix socket.

- **macOS**: `sandbox-exec -f os_sandbox.sb -D CLONE=... -D RUN=...` with
  the profile shipped beside this module (os_sandbox.sb says what it
  allows). sandbox-exec is deprecated but present on every macOS release,
  and has no supported command-line successor (Chromium and Bazel use it).
  Unix-socket connects are allowed only to sockets under CLONE and RUN.
- **Linux**: bubblewrap (`bwrap`): the root filesystem read-only, new
  network, pid and IPC namespaces (`--unshare-net`: a private loopback
  only, and no host abstract socket), and an empty tmpfs over every place a
  host daemon's Unix socket lives (/tmp, /run, /var/run when it is not a
  link to /run, the user's HOME, XDG_RUNTIME_DIR): a socket file on a
  read-only bind can still be connected to, so the ones that matter (the
  docker socket, an ssh or gpg agent, dbus, a database) are hidden rather
  than made read-only. What the run needs from under them (`read_only`:
  the reused venv and its interpreter, PATH, version managers) is bound
  back read-only, never one of the masked roots itself; then the clone and
  the run's directory, writable. bwrap needs unprivileged user
  namespaces; where they are off (a hardened kernel, Ubuntu 24.04's
  AppArmor restriction without bwrap's profile), it exits non-zero and
  self_test() says so.
- **Anything else**, or the tool missing: OsSandboxUnavailable. Nothing
  here falls back to running unsandboxed: the gate refuses instead.

Before a gate's first run, self_test() runs a probe through the SAME wrap()
argv path and requires that a write in the clone succeeds, a write in a
directory outside both the clone and the run's directory fails, an
outbound TCP connect to 1.1.1.1:53 fails, a connect to a Unix socket
listening outside fails (a host daemon's socket stands for it), and a
socket made inside the run's directory works. Any deviation (including the
sandbox tool failing to start, as sandbox-exec does inside another
sandbox) is OsSandboxUnavailable naming the check.

Commands are argv lists throughout, never a shell string: the research on
this bead found that a command smuggled through a string ran unsandboxed.

Limits, stated: reads are not confined on macOS (the reused venv and
system libraries must be readable, and so is the rest of the user's disk),
so a test can read a file it cannot send anywhere: the network and host
sockets are denied, and the only thing that leaves the clone is the patch
of the fixed paths, built from bytes held before any run (green_gate). A
machine that is offline passes the network check without proving the
sandbox denies it; the write and socket checks still prove the sandbox is
on. The Linux line has not been run on this project's development machine
(macOS): its self-test is what proves it on the machine that runs it.
"""

import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

#: The macOS profile, shipped as data beside this module.
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "os_sandbox.sb")

#: How long the self-test's probe may take: each connect gives up after 3s.
_SELF_TEST_TIMEOUT = 30

#: The self-test's probe, run by /bin/sh. $1 is a file to create in the
#: clone, $2 one to create outside both the clone and the run's directory,
#: $3 an interpreter (the sweep's own, so there always is one), $4 a Unix
#: socket listening outside, $5 a socket path to make inside the run's
#: directory. Prints one `name=value` line per check. `set -C` (noclobber)
#: makes `>` fail on an existing file, so the probe never truncates one.
_PROBE = r"""
if (set -C; : > "$1") 2>/dev/null; then echo clone=ok; else echo clone=denied; fi
if (set -C; : > "$2") 2>/dev/null; then echo outside=ok; else echo outside=denied; fi
exec "$3" -I -S -c "$SOCKET_PROBE" "$4" "$5"
"""

#: The probe's Python half: the network and the Unix sockets.
_SOCKET_PROBE = """
import os, socket, sys
outside, inside = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
try:
    s.connect(("1.1.1.1", 53))
except OSError:
    print("net=blocked")
else:
    print("net=connected")
u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
u.settimeout(3)
try:
    u.connect(outside)
except OSError:
    print("unix_outside=blocked")
else:
    print("unix_outside=connected")
# Relative to its directory: a socket's path is limited to about 100 bytes,
# and the run's directory may be deep.
os.chdir(os.path.dirname(inside))
name = os.path.basename(inside)
try:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(name)
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    client.connect(name)
except OSError as error:
    print("unix_inside=denied", error, file=sys.stderr)
    print("unix_inside=denied")
else:
    print("unix_inside=ok")
finally:
    try:
        os.unlink(name)
    except OSError:
        pass
"""


class OsSandboxUnavailable(Exception):
    """No OS sandbox can confine the test run here (unsupported platform,
    tool missing, or self_test() found it not confining). The gate turns it
    into a refusal: the test command is never run unsandboxed."""


def wrap(argv, *, clone, run_dir, read_only=()):
    """`argv` prefixed so it runs confined to `clone` and `run_dir` (writes)
    with no network but loopback and no host Unix socket. Both are resolved
    to real paths. `read_only` names paths the run must read that Linux's
    masks would hide (macOS hides nothing from reads, so ignores it). A new
    list; OsSandboxUnavailable where no sandbox is available."""
    if isinstance(argv, (str, bytes)):
        raise ValueError("wrap takes an argv list, never a command string")
    argv = list(argv)
    if not argv or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("wrap takes a non-empty argv list of strings")
    clone, run_dir = os.path.realpath(str(clone)), os.path.realpath(str(run_dir))
    if sys.platform == "darwin":
        tool = shutil.which("sandbox-exec")
        if tool is None:
            raise OsSandboxUnavailable("sandbox-exec is not on PATH (macOS ships it as "
                                       "/usr/bin/sandbox-exec)")
        if not os.path.isfile(PROFILE):
            raise OsSandboxUnavailable(f"the sandbox profile {PROFILE} is missing from the "
                                       "installed package")
        return [tool, "-f", PROFILE, "-D", f"CLONE={clone}", "-D", f"RUN={run_dir}", *argv]
    if sys.platform.startswith("linux"):
        tool = shutil.which("bwrap")
        if tool is None:
            raise OsSandboxUnavailable("bubblewrap (bwrap) is not installed; install it "
                                       "(e.g. `apt install bubblewrap`)")
        masked = _masked_roots()
        line = [tool, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for root in masked:
            line += ["--tmpfs", root]
        for path in _read_only_binds(read_only, masked):
            line += ["--ro-bind-try", path, path]
        return [*line, "--bind", clone, clone, "--bind", run_dir, run_dir,
                "--unshare-net", "--unshare-pid", "--unshare-ipc",
                "--die-with-parent", "--new-session", "--chdir", clone, "--", *argv]
    raise OsSandboxUnavailable(f"no OS sandbox is supported on {sys.platform!r} (only macOS "
                               "sandbox-exec and Linux bwrap)")


def _masked_roots():
    """Linux: the directories hidden under an empty tmpfs because a host
    daemon's Unix socket lives there (module docstring). Real paths, outer
    first, none of them `/` and none inside another."""
    roots = ["/tmp", "/run"]
    if os.path.isdir("/var/run") and not os.path.islink("/var/run"):
        roots.append("/var/run")
    for value in (os.path.expanduser("~"), os.environ.get("XDG_RUNTIME_DIR")):
        if value and os.path.isabs(value) and os.path.isdir(value):
            roots.append(os.path.realpath(value))
    found = []
    for root in roots:
        if root != "/" and not any(_inside(root, kept) for kept in found):
            found.append(root)
    return found


def _read_only_binds(paths, masked):
    """The `read_only` paths worth binding back: absolute, existing, real,
    under a masked root (elsewhere the read-only root shows them already),
    and never one that is or holds a masked root (binding HOME back would
    unmask every socket in it). Sorted, so a parent is bound before its
    child."""
    found = set()
    for path in paths:
        if not path or not os.path.isabs(path) or not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        if any(_inside(root, real) for root in masked):
            continue
        if any(_inside(real, root) for root in masked):
            found.add(real)
    return sorted(found)


def _inside(path, root):
    return os.path.commonpath([root, path]) == root


def self_test(clone, run_dir, env=None, read_only=()):
    """Prove, through wrap(), that the sandbox confines here: a write in
    `clone` succeeds, a write in a fresh directory outside `clone` and
    `run_dir` fails, an outbound connect to 1.1.1.1:53 fails, a connect to
    a Unix socket listening in that outside directory fails, and a socket
    made inside `run_dir` works. `env` is the probe's environment (default
    the current one); `read_only` goes to wrap() with the sweep's own
    interpreter, which runs the socket checks, added. OsSandboxUnavailable
    naming the failed check; nothing is left behind either way."""
    clone_real, run_real = os.path.realpath(str(clone)), os.path.realpath(str(run_dir))
    # Short: a Unix socket's path is limited to about 100 bytes, so a deep
    # TMPDIR falls back to /tmp (outside the clone and the run's directory
    # all the same, which the check below holds it to).
    outside = tempfile.mkdtemp(prefix="cm-osbx-")
    if len(os.path.join(outside, "s")) > 100:
        os.rmdir(outside)
        outside = tempfile.mkdtemp(prefix="cm-osbx-", dir="/tmp")
    token = secrets.token_hex(6)
    in_clone = os.path.join(clone_real, f".curamoir-os-sandbox-probe-{token}")
    in_outside = os.path.join(outside, "probe")
    listening = os.path.join(outside, "s")
    inside_socket = os.path.join(run_real, f"s{token}")
    server = None
    try:
        if any(_inside(os.path.realpath(outside), root) for root in (clone_real, run_real)):
            raise OsSandboxUnavailable(f"the self-test's outside directory {outside} is inside "
                                       "the clone or the run's directory, so it proves nothing")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(listening)
        server.listen(1)
        python = os.path.realpath(sys.executable)
        argv = wrap(["/bin/sh", "-c", _PROBE, "curamoir-os-sandbox-probe", in_clone,
                     in_outside, python, listening, inside_socket],
                    clone=clone_real, run_dir=run_real,
                    read_only=[*read_only, os.path.dirname(python),
                               os.path.realpath(sys.base_prefix)])
        probe_env = dict(os.environ if env is None else env, SOCKET_PROBE=_SOCKET_PROBE)
        try:
            done = subprocess.run(argv, cwd=clone_real, env=probe_env, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=_SELF_TEST_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            raise OsSandboxUnavailable(f"the sandbox self-test could not run: {error}") from error
        said = dict(line.split("=", 1) for line in
                    done.stdout.decode("utf-8", "replace").splitlines() if "=" in line)
        why = _failed_check(said)
        if why:
            detail = done.stderr.decode("utf-8", "replace").strip()[-500:]
            raise OsSandboxUnavailable(
                f"the sandbox self-test failed: {why} (exit {done.returncode}"
                + (f": {detail}" if detail else "") + ")")
    except OSError as error:
        raise OsSandboxUnavailable(f"the sandbox self-test could not be set up: {error}") \
            from error
    finally:
        if server is not None:
            server.close()
        for path in (in_clone, in_outside, listening, inside_socket):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        shutil.rmtree(outside, ignore_errors=True)


#: The probe's checks: (name, the value a confining sandbox gives, what any
#: other value means).
_CHECKS = (
    ("clone", "ok", "a write inside the clone did not succeed"),
    ("outside", "denied", "a write outside the clone and the run's directory was not denied"),
    ("net", "blocked", "an outbound connection to 1.1.1.1:53 was not denied"),
    ("unix_outside", "blocked", "a connection to a Unix socket outside the clone and the "
                                "run's directory was not denied"),
    ("unix_inside", "ok", "a Unix socket inside the run's directory did not work"),
)


def _failed_check(said):
    """Which check `said` (the probe's name=value lines) fails, or ""."""
    if not said:
        return "the sandboxed probe printed nothing, so the sandbox did not start"
    for name, wanted, why in _CHECKS:
        if name not in said:
            return f"the {name} check did not run (the probe failed inside the sandbox)"
        if said[name] != wanted:
            return why
    return ""
