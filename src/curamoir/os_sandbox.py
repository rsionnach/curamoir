"""The operating-system sandbox the E3-R4 test run executes in (cm-p2f).

The test gate (green_gate) runs the project's test command on code the fix
agent just edited: without an OS sandbox that is the agent getting a shell
by another route. wrap() prefixes a command's argv so the kernel confines
it: writes only inside the sandbox clone and the run's own directory (its
HOME and TMPDIR); no reads under the user's home but the paths the run
needs (`read_only`); no network (loopback only on request); no host Unix
socket; no service that acts outside the sandbox.

- **macOS**: `sandbox-exec -p <profile> -D CLONE=... -D RUN=... -D ...`,
  the profile being os_sandbox.sb (shipped beside this module: network,
  Unix sockets, Mach services, signals, writes) plus the rules wrap()
  builds for this run (_darwin_rules): reads under the user's home denied
  except `read_only`, CLONE and RUN (and the metadata of the directories
  above them, so a path can be resolved), and loopback when `loopback`.
  Every path is a `-D` parameter, never spliced into the profile text.
  sandbox-exec is deprecated but present on every macOS release, and has no
  supported command-line successor (Chromium and Bazel use it).
- **Linux**: bubblewrap (`bwrap`), deny by default: a fresh root holding
  only the system directories (/usr, /bin, /sbin, /lib*), the parts of /etc
  a runtime reads (_LINUX_ETC), a fresh /tmp, /dev and /proc, `read_only`
  (never the home directory itself or anything holding it) read-only, and
  the clone and the run's directory writable; new network, pid and IPC
  namespaces (`--unshare-net`: a private loopback that reaches nothing of
  the host's, and no host abstract socket). Nothing else of the host is
  there, so no host socket (/run, /var/run, the home directory) either.
  bwrap needs unprivileged user namespaces; where they are off (a hardened
  kernel, Ubuntu 24.04's AppArmor restriction without bwrap's profile), it
  exits non-zero and self_test() says so. This line has NOT been run on
  this project's development machine (macOS); cm-xy9.7 verifies it on
  Linux, and its self-test is what proves it wherever it runs.
- **Anything else**, or the tool missing: OsSandboxUnavailable. Nothing
  here falls back to running unsandboxed: the gate refuses instead.

Before a gate's first run, self_test() runs a probe through the SAME wrap()
argv path (_CHECKS lists what each check proves). Any deviation (including
the sandbox tool failing to start, as sandbox-exec does inside another
sandbox) is OsSandboxUnavailable naming the check.

Commands are argv lists throughout, never a shell string: the research on
this bead found that a command smuggled through a string ran unsandboxed.

Limits, stated: on macOS, reads OUTSIDE the user's home stay open (system
libraries, /opt, /usr/local, other volumes), and a run can still read
what it was granted under the home (the reused venv). A machine that is
offline passes the network and DNS checks without proving the sandbox
denies them; the other checks still prove the sandbox is on.
"""

import contextlib
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

#: The static part of the macOS profile, shipped as data beside this module.
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "os_sandbox.sb")

#: How long the self-test's probe may take: each connect gives up after 3s.
_SELF_TEST_TIMEOUT = 30

#: Linux: the system directories bound read-only (a link, as on a merged-/usr
#: system, is recreated as the same link).
_LINUX_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
#: Linux: what of /etc a runtime reads (the loader, users and groups, time,
#: certificates, alternatives). Bound only where present.
_LINUX_ETC = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/passwd",
              "/etc/group", "/etc/nsswitch.conf", "/etc/localtime", "/etc/hosts",
              "/etc/ssl", "/etc/ca-certificates", "/etc/pki", "/etc/alternatives")

#: The self-test's probe, run by /bin/sh. $1 a file to create in the clone,
#: $2 one to create outside the clone and the run's directory, $3 the
#: interpreter that runs the rest (the sweep's own, so there always is one),
#: then the Python half's arguments. Prints one `name=value` line per check.
#: `set -C` (noclobber) makes `>` fail on an existing file, so the probe
#: never truncates one.
_PROBE = r"""
if (set -C; : > "$1") 2>/dev/null; then echo clone=ok; else echo clone=denied; fi
if (set -C; : > "$2") 2>/dev/null; then echo outside=ok; else echo outside=denied; fi
if [ "$(uname)" = Darwin ]; then
    if /bin/launchctl version >/dev/null 2>&1; then echo launchctl=ran; else echo launchctl=denied; fi
fi
python=$3
shift 3
exec "$python" -I -S -c "$SOCKET_PROBE" "$@"
"""

#: The probe's Python half: argv is the outside socket, a socket path in the
#: run's directory, and a file or directory under the user's home to read.
_SOCKET_PROBE = """
import os, socket, sys
outside, inside, home = sys.argv[1:4]

def check(name, attempt, good, bad):
    try:
        attempt()
    except OSError:
        print(f"{name}={bad}")
    else:
        print(f"{name}={good}")

def tcp():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(("1.1.1.1", 53))

def unix_outside():
    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    u.settimeout(3)
    u.connect(outside)

def unix_inside():
    # Relative to its directory: a socket's path is limited to about 100
    # bytes, and the run's directory may be deep.
    os.chdir(os.path.dirname(inside))
    name = os.path.basename(inside)
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(name)
        server.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3)
        client.connect(name)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass

def loopback():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    socket.create_connection(server.getsockname(), timeout=3).close()

def dns():
    socket.getaddrinfo("example.com", 443)

def home_read():
    if os.path.isdir(home):
        os.listdir(home)
    else:
        open(home, "rb").read()

def launchservices():
    import ctypes
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    port = ctypes.c_uint(0)
    bootstrap = ctypes.c_uint.in_dll(system, "bootstrap_port")
    if system.bootstrap_look_up(bootstrap, b"com.apple.coreservices.launchservicesd",
                                ctypes.byref(port)) != 0:
        raise OSError("look-up refused")

check("net", tcp, "connected", "blocked")
check("unix_outside", unix_outside, "connected", "blocked")
check("unix_inside", unix_inside, "ok", "denied")
check("loopback", loopback, "ok", "denied")
check("dns", dns, "resolved", "failed")
check("home_read", home_read, "read", "denied")
if sys.platform == "darwin":
    check("launchservices", launchservices, "reached", "denied")
"""


class OsSandboxUnavailable(Exception):
    """No OS sandbox can confine the test run here (unsupported platform,
    tool missing, or self_test() found it not confining). The gate turns it
    into a refusal: the test command is never run unsandboxed."""


def wrap(argv, *, clone, run_dir, read_only=(), loopback=False):
    """`argv` prefixed so it runs confined (module docstring): writes only
    in `clone` and `run_dir` (both resolved to real paths), reads under the
    user's home only of `read_only` and those two, and no network but, on
    macOS with `loopback`, 127.0.0.1/::1 (Linux's private loopback is always
    there and reaches nothing of the host's). A new list;
    OsSandboxUnavailable where no sandbox is available."""
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
        try:
            with open(PROFILE, encoding="utf-8") as handle:
                static = handle.read()
        except OSError as error:
            raise OsSandboxUnavailable(f"the sandbox profile {PROFILE} cannot be read from "
                                       f"the installed package: {error}") from error
        rules, params = _darwin_rules(clone, run_dir, read_only, loopback)
        line = [tool, "-p", static + rules, "-D", f"CLONE={clone}", "-D", f"RUN={run_dir}"]
        for name, value in params:
            line += ["-D", f"{name}={value}"]
        return [*line, *argv]
    if sys.platform.startswith("linux"):
        tool = shutil.which("bwrap")
        if tool is None:
            raise OsSandboxUnavailable("bubblewrap (bwrap) is not installed; install it "
                                       "(e.g. `apt install bubblewrap`)")
        line = [tool]
        for path in _LINUX_SYSTEM:
            if os.path.islink(path):
                line += ["--symlink", os.readlink(path), path]
            elif os.path.isdir(path):
                line += ["--ro-bind", path, path]
        for path in _LINUX_ETC:
            line += ["--ro-bind-try", path, path]
        line += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        for path in _granted(read_only):
            line += ["--ro-bind-try", path, path]
        return [*line, "--bind", clone, clone, "--bind", run_dir, run_dir,
                "--unshare-net", "--unshare-pid", "--unshare-ipc",
                "--die-with-parent", "--new-session", "--chdir", clone, "--", *argv]
    raise OsSandboxUnavailable(f"no OS sandbox is supported on {sys.platform!r} (only macOS "
                               "sandbox-exec and Linux bwrap)")


def homes():
    """The user's home directories, real paths: the account's (pwd) and
    $HOME when it differs; never `/`."""
    found = []
    candidates = [os.environ.get("HOME")]
    with contextlib.suppress(KeyError):
        candidates.insert(0, pwd.getpwuid(os.getuid()).pw_dir)
    for value in candidates:
        if value and os.path.isabs(value) and os.path.isdir(value):
            real = os.path.realpath(value)
            if real != "/" and real not in found:
                found.append(real)
    return found


def _granted(paths):
    """The `read_only` paths a run may read: absolute, existing, real, and
    never `/` nor one that is or holds a home directory (granting that would
    grant everything in it). Sorted and deduplicated."""
    found = set()
    for path in paths:
        if not path or not os.path.isabs(path) or not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        if real == "/" or any(_inside(home, real) for home in homes()):
            continue
        found.add(real)
    return sorted(found)


def _darwin_rules(clone, run_dir, read_only, loopback):
    """(SBPL text, [(param, path)]) for this run: reads under each home
    denied, then allowed again for each granted path under it, CLONE and
    RUN, and the metadata of every directory between the home and those (a
    path cannot be resolved without it, and a listing is not metadata).
    Later rules win in SBPL, so the allows follow the denies."""
    rules, params = [], []

    def param(prefix, value):
        name = f"{prefix}{len(params)}"
        params.append((name, value))
        return f'(param "{name}")'

    under = []
    for home in homes():
        rules.append(f"(deny file-read* (subpath {param('HOME', home)}))")
        allowed = [p for p in [*_granted(read_only), clone, run_dir] if _inside(p, home)]
        under.extend((home, path) for path in allowed)
    metadata = set()
    for home, path in under:
        rules.append(f"(allow file-read* (subpath {param('READ', path)}))")
        parent = os.path.dirname(path)
        while _inside(parent, home) and parent not in metadata:
            metadata.add(parent)
            parent = os.path.dirname(parent)
    for directory in sorted(metadata):
        rules.append(f"(allow file-read-metadata (literal {param('META', directory)}))")
    if loopback:
        rules += ['(allow network-bind network-inbound (local ip "localhost:*"))',
                  '(allow network-outbound (remote ip "localhost:*"))']
    return "\n" + "\n".join(rules) + "\n", params


def _inside(path, root):
    return os.path.commonpath([root, path]) == root


def self_test(clone, run_dir, env=None, read_only=(), loopback=False):
    """Prove, through wrap() with the same `read_only` and `loopback`, that
    the sandbox confines here: every check of _CHECKS. `env` is the probe's
    environment (default the current one). OsSandboxUnavailable naming the
    failed check; nothing is left behind either way."""
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
    home_target, made = _home_target()
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
                     in_outside, python, listening, inside_socket, home_target or "/nonexistent"],
                    clone=clone_real, run_dir=run_real, loopback=loopback,
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
        why = _failed_check(said, loopback=loopback, home=home_target is not None)
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
        for path in (in_clone, in_outside, listening, inside_socket, made):
            if path:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
        shutil.rmtree(outside, ignore_errors=True)


def _home_target():
    """(what the probe tries to read under the user's home, the file made
    for it or None): a fresh file in the account's home when it is
    writable, else the home directory itself (a listing). (None, None)
    with no home at all."""
    found = homes()
    if not found:
        return None, None
    try:
        handle, path = tempfile.mkstemp(prefix=".curamoir-read-probe-", dir=found[0])
    except OSError:
        return found[0], None
    with os.fdopen(handle, "w") as out:
        out.write("a canary the sandboxed self-test must not read\n")
    return path, path


#: The probe's checks: (name, the value a confining sandbox gives, what any
#: other value means). What each proves: `clone`, that the sandbox lets the
#: run write where it must; `outside`, that writes elsewhere are denied;
#: `net`, that no remote host is reachable; `unix_outside`, that a host
#: daemon's socket (docker, an ssh agent) is not; `unix_inside`, that the
#: run's own sockets (multiprocessing, node IPC) work; `dns`, that no name
#: resolves (mDNSResponder, an exfiltration channel); `home_read`, that the
#: user's home (~/.ssh, keychains, tokens) is unreadable; `launchctl` and
#: `launchservices` (macOS), that neither a launchd job nor an app can be
#: started outside the sandbox. `loopback` is checked against the setting.
_CHECKS = (
    ("clone", "ok", "a write inside the clone did not succeed"),
    ("outside", "denied", "a write outside the clone and the run's directory was not denied"),
    ("net", "blocked", "an outbound connection to 1.1.1.1:53 was not denied"),
    ("unix_outside", "blocked", "a connection to a Unix socket outside the clone and the "
                                "run's directory was not denied"),
    ("unix_inside", "ok", "a Unix socket inside the run's directory did not work"),
    ("dns", "failed", "a host name was resolved"),
)
_HOME_CHECK = ("home_read", "denied", "a file under the user's home directory was readable")
_DARWIN_CHECKS = (
    ("launchctl", "denied", "/bin/launchctl ran (it could submit a job outside the sandbox)"),
    ("launchservices", "denied", "LaunchServices (com.apple.coreservices.launchservicesd) "
                                 "was reachable: `open` could start an app outside the sandbox"),
)


def _failed_check(said, loopback=False, home=True, platform=None):
    """Which check `said` (the probe's name=value lines) fails, or ""."""
    if not said:
        return "the sandboxed probe printed nothing, so the sandbox did not start"
    checks = list(_CHECKS)
    if home:
        checks.append(_HOME_CHECK)
    if (platform or sys.platform) == "darwin":
        checks.extend(_DARWIN_CHECKS)
        checks.append(("loopback", "ok", "loopback did not work though sweep.test_loopback "
                                         "is true") if loopback else
                      ("loopback", "denied", "loopback was reachable though "
                                             "sweep.test_loopback is false"))
    for name, wanted, why in checks:
        if name not in said:
            return f"the {name} check did not run (the probe failed inside the sandbox)"
        if said[name] != wanted:
            return why
    return ""
