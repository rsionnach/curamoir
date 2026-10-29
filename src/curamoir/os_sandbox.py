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
  builds for this run (_darwin_rules): reads under the user's home (and
  its /System/Volumes/Data alias) denied except `read_only`, CLONE and RUN
  (and the metadata of the directories above them, so a path can be
  resolved), and loopback when `loopback`. Hard links only within CLONE
  and RUN, so no file of the home's gets an allowed second name.
  Every path is a `-D` parameter, never spliced into the profile text.
  sandbox-exec is deprecated but present on every macOS release, and has no
  supported command-line successor (Chromium and Bazel use it).
- **Linux**: bubblewrap (`bwrap`), deny by default: a fresh root holding
  only the system directories (/usr, /bin, /sbin, /lib*), the parts of /etc
  a runtime reads (_LINUX_ETC), a fresh /tmp, /dev and /proc, `read_only`
  (never the home directory itself or anything holding it, nor a socket or
  kernel tree or anything in or holding one; bound before /dev, /proc and
  /tmp, so those stay the run's own) read-only, and
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
what it was granted under the home (the reused venv; never a home, nor
anything in or holding a credential tree, _CREDENTIAL_TREES). A machine that is
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
#: run's directory, a file or directory under the user's home to read, where
#: in the run's directory to hard-link it to, and its path through the Data
#: volume's firmlink ("" where there is none).
_SOCKET_PROBE = """
import os, socket, sys
outside, inside, home, link_to, alias = sys.argv[1:6]

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

def read(path):
    if not path:
        raise OSError("nothing to read")
    if os.path.isdir(path):
        os.listdir(path)
    else:
        open(path, "rb").read()

def home_link():
    os.link(home, link_to)
    try:
        open(link_to, "rb").read()
    finally:
        os.unlink(link_to)

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
check("home_read", lambda: read(home), "read", "denied")
check("home_link", home_link, "linked", "denied")
check("home_alias", lambda: read(alias), "read", "denied")
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
    clone, run_dir = _canonical(str(clone)), _canonical(str(run_dir))
    if sys.platform == "darwin":
        tool = tool_path("darwin")
        if tool is None:
            raise OsSandboxUnavailable("/usr/bin/sandbox-exec is missing (macOS ships it "
                                       "there; no other copy is ever used)")
        _require_home()
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
        tool = tool_path("linux")
        if tool is None:
            raise OsSandboxUnavailable("bubblewrap (bwrap) is not installed in "
                                       + ", ".join(os.path.dirname(p) for p in _BWRAP)
                                       + "; install it (e.g. `apt install bubblewrap`)")
        _require_home()
        line = [tool]
        for path in _LINUX_SYSTEM:
            try:
                if os.path.islink(path):
                    line += ["--symlink", os.readlink(path), path]
                elif os.path.isdir(path):
                    line += ["--ro-bind", path, path]
            except OSError as error:
                raise OsSandboxUnavailable(f"the system directory {path} cannot be read to "
                                           f"be bound: {error}") from error
        for path in _LINUX_ETC:
            line += ["--ro-bind-try", path, path]
        # The grants before --dev, --proc and --tmpfs: a later mount covers an
        # earlier one, so the run's own /dev, /proc and /tmp always win over
        # anything granted (which _granted refuses besides).
        for path in _granted(read_only):
            line += ["--ro-bind-try", path, path]
        line += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        return [*line, "--bind", clone, clone, "--bind", run_dir, run_dir,
                "--unshare-net", "--unshare-pid", "--unshare-ipc",
                "--die-with-parent", "--new-session", "--chdir", clone, "--", *argv]
    raise OsSandboxUnavailable(f"no OS sandbox is supported on {sys.platform!r} (only macOS "
                               "sandbox-exec and Linux bwrap)")


def _require_home():
    """OsSandboxUnavailable when no home directory is found: _granted then
    has none to refuse (nor its credential trees), and on macOS reads under
    it cannot be denied."""
    if not homes():
        raise OsSandboxUnavailable("no home directory was found, so reads under it cannot "
                                   "be denied, nor a grant of it or its credentials refused")


#: Where the sandbox tools are taken from: fixed system locations, never the
#: sweep's PATH (a directory early on it could hold a `sandbox-exec` that
#: confines nothing). /usr/local/bin is root's on a Linux system.
_SANDBOX_EXEC = ("/usr/bin/sandbox-exec",)
_BWRAP = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap")


def _is_executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def tool_path(platform=None):
    """The sandbox tool for `platform` (default this one) from its fixed
    locations, or None."""
    platform = platform or sys.platform
    if platform == "darwin":
        candidates = _SANDBOX_EXEC
    elif platform.startswith("linux"):
        candidates = _BWRAP
    else:
        return None
    return next((path for path in candidates if _is_executable(path)), None)


def _canonical(path):
    """`path` resolved: realpath, and on macOS the kernel's own spelling of
    it (F_GETPATH: the case on disk, /Users not its Data-volume alias), the
    path the sandbox matches rules against. realpath where it cannot be
    opened (it does not exist, or is unreadable)."""
    real = os.path.realpath(path)
    if sys.platform != "darwin":
        return real
    try:
        import fcntl
        handle = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (ImportError, OSError):
        return real
    try:
        spelled = fcntl.fcntl(handle, fcntl.F_GETPATH, bytes(1024))
    except (OSError, AttributeError):
        return real
    finally:
        os.close(handle)
    return os.fsdecode(spelled.split(b"\0", 1)[0]) or real


def _identity(path):
    try:
        found = os.stat(path)
    except OSError:
        return None
    return found.st_dev, found.st_ino


def _lineage(path):
    """The identities (device, inode) of `path` and every directory above
    it: what `path` is inside, whatever its spelling (case, firmlink)."""
    found = set()
    while True:
        identity = _identity(path)
        if identity is not None:
            found.add(identity)
        parent = os.path.dirname(path)
        if parent == path:
            return found
        path = parent


def homes():
    """The user's home directories, real paths: the account's (pwd) and
    $HOME when it differs; never `/`."""
    found = []
    candidates = [os.environ.get("HOME")]
    with contextlib.suppress(KeyError):
        candidates.insert(0, pwd.getpwuid(os.getuid()).pw_dir)
    for value in candidates:
        if value and os.path.isabs(value) and os.path.isdir(value):
            real = _canonical(value)
            if real != "/" and real not in found:
                found.append(real)
    return found


#: Where a home keeps credentials (keys, tokens, keychains, cloud and
#: cluster logins), relative to it: nothing in one, and nothing holding one,
#: is ever granted (_granted). All of ~/.config: gh, gcloud, git's
#: credential store and many more keep tokens there, and no version
#: manager installs a toolchain there (mise's live in ~/.local/share/mise).
#: All of ~/Library: keychains, cookies, application tokens (a macOS
#: framework Python's user base, ~/Library/Python/X.Y, is refused with it).
_CREDENTIAL_TREES = (".ssh", ".aws", ".gnupg", ".config", os.path.join(".local", "share",
                     "keyrings"), "Library", ".netrc", ".docker", ".kube")


#: Under a home, the ONLY directories whose contents may be granted (an
#: allowlist; the credential trees above are a second check): where version
#: managers keep installed toolchains, never their whole root (~/.cargo
#: holds credentials.toml, ~/.pyenv and ~/.asdf shims and plugins):
#: ~/.pyenv/versions, ~/.nvm/versions, ~/.asdf/installs, ~/.volta/tools,
#: ~/.rustup/toolchains, ~/.cargo/bin, ~/.rbenv/versions,
#: ~/.sdkman/candidates, ~/.local/share/uv/python,
#: ~/.local/share/mise/installs, and conda's install roots (~/miniconda3,
#: ~/anaconda3, ~/miniforge3, ~/mambaforge: an interpreter there has no
#: pyvenv.cfg, so without them the sweep's own conda Python is refused and no
#: gate can run). Besides these, a Python venv (a directory
#: holding pyvenv.cfg, and anything in it) and anything in a `node_modules`
#: (the checkout's own, which is where the gate's PATH and NODE_PATH point).
#: A shim (pyenv's, asdf's, volta's) that reads its manager's root is not
#: granted and fails: the base is red or the command not runnable.
_TOOLCHAIN_DIRS = tuple(os.path.join(*parts.split("/")) for parts in (
    ".pyenv/versions", ".nvm/versions", ".asdf/installs", ".volta/tools", ".rustup/toolchains",
    ".cargo/bin", ".rbenv/versions", ".sdkman/candidates", ".local/share/uv/python",
    ".local/share/mise/installs", "miniconda3", "anaconda3", "miniforge3", "mambaforge"))

#: Never granted, nor anything in them, nor anything holding them (/var,
#: /private): where host daemons keep their Unix sockets, and a read-only
#: bind (Linux) does not stop connect() to a socket inode.
_SOCKET_TREES = ("/tmp", "/var/tmp", "/run", "/var/run", "/dev/shm", "/private/tmp",
                 "/private/var/tmp", "/private/var/run")
#: Never granted either, in or holding: the kernel's trees. bwrap gives the
#: run its own /dev and /proc, and a host one bound over them would be the
#: host's devices and every process's root (/proc/<pid>/root). Refused on
#: macOS too, where reads outside the home are open anyway.
_KERNEL_TREES = ("/dev", "/proc", "/sys")

#: What _granted refuses, said where a refusal is reported.
REFUSAL_RULE = ("under a home only a toolchain directory, a venv, a conda install or a "
                "node_modules is granted, never a credential tree; nor a socket or kernel "
                "directory, nor anything holding a home or one of those")


def _granted(paths):
    """The `read_only` paths a run may read: absolute, existing, resolved
    (_canonical), and never `/`, a socket or kernel tree (_SOCKET_TREES,
    _KERNEL_TREES) or anything in or holding one, a home or anything
    holding one, nor, under a home, anything but the allowlist
    (_TOOLCHAIN_DIRS, a venv, a node_modules), and never anything in or
    holding a credential tree (_CREDENTIAL_TREES). Each
    comparison is by identity (device, inode) of the path and its parents,
    so neither case (`~/.SSH` on a case-insensitive volume) nor an alias
    (/System/Volumes/Data/Users/...) slips past. A refused path is left out,
    silently: the run then cannot read it, and fails if it needed it.
    Sorted and deduplicated."""
    found = set()
    every_home = homes()
    for path in paths:
        if not path or not os.path.isabs(path) or not os.path.exists(path):
            continue
        real = _canonical(path)
        if real == "/" or _refused(real, every_home):
            continue
        found.add(real)
    return sorted(found)


def _ungranted(paths):
    """Those of `paths` (absolute, existing; resolved, sorted, deduplicated)
    that _granted refuses: what a run cannot read, to name in a refusal."""
    return sorted({_canonical(path) for path in paths
                   if path and os.path.isabs(path) and os.path.exists(path)
                   and not _granted([path])})


def _refused(path, every_home):
    lineage = _lineage(path)
    me = _identity(path)
    for tree in (*_SOCKET_TREES, *_KERNEL_TREES):
        if _in_or_holding(path, lineage, me, tree):
            return True
        # Its Data-volume spelling (macOS: /System/Volumes/Data/private/tmp),
        # so a path holding the tree only through the firmlink is caught.
        alias = _DATA_VOLUME + tree
        if os.path.exists(alias) and _in_or_holding(path, lineage, me, alias):
            return True
    for home in every_home:
        home_id = _identity(home)
        if home_id is None:
            continue
        if me in _lineage(home):
            return True  # the home itself, or a directory holding it
        if _near_credentials(path, lineage, me, home):
            return True
        if home_id in lineage and not _allowlisted(path, lineage, home, home_id):
            return True
    return False


def _allowlisted(path, lineage, home, home_id):
    """Whether `path` (under `home`) is in a toolchain directory, a venv or
    a node_modules (_TOOLCHAIN_DIRS)."""
    for directory in _TOOLCHAIN_DIRS:
        identity = _identity(os.path.join(home, directory))
        if identity is not None and identity in lineage:
            return True
    while _identity(path) not in (home_id, None):
        if os.path.basename(path) == "node_modules" or \
                os.path.isfile(os.path.join(path, "pyvenv.cfg")):
            return True
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return False


def _near_credentials(path, lineage, me, home):
    """Whether `path` is in or holds one of `home`'s credential trees."""
    return any(_in_or_holding(path, lineage, me, os.path.join(home, tree))
               for tree in _CREDENTIAL_TREES)


def _in_or_holding(path, lineage, me, tree):
    """Whether `path` (its `lineage` and identity `me`) is `tree`, is in it
    or holds it: by identity, so neither case nor an alias slips past, and
    by spelling, for a tree that does not exist (yet)."""
    identity = _identity(tree)
    if identity is not None and (identity in lineage or me in _lineage(tree)):
        return True
    return _inside(path, tree) or _inside(tree, path)


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
        alias = _DATA_VOLUME + home
        if not _inside(home, _DATA_VOLUME) and os.path.isdir(alias):
            rules.append(f"(deny file-read* (subpath {param('ALIAS', alias)}))")
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


#: macOS's Data volume, where /Users (and /private, /opt, /usr/local) really
#: live: a home is also reachable as /System/Volumes/Data/<home>, through
#: the firmlink. On macOS 14.2 the kernel matches the canonical /Users path
#: whichever spelling was opened (tested: a read through the alias, through
#: /.vol/<device>/<inode> and through /Volumes/Macintosh HD is denied by the
#: /Users rule, and a rule naming the alias matches nothing), so the alias
#: rule _darwin_rules adds is a second line should a release match the
#: path as given; self_test's `home_alias` check proves the alias denied.
_DATA_VOLUME = "/System/Volumes/Data"


def _inside(path, root):
    return os.path.commonpath([root, path]) == root


def self_test(clone, run_dir, env=None, read_only=(), loopback=False):
    """Prove, through wrap() with the same `read_only` and `loopback`, that
    the sandbox confines here: every check of _CHECKS. `env` is the probe's
    environment (default the current one). OsSandboxUnavailable naming the
    failed check; nothing is left behind either way."""
    clone_real, run_real = _canonical(str(clone)), _canonical(str(run_dir))
    # The probe runs the sweep's own interpreter: one the sandbox will not
    # grant fails every check as "did not run", so it is said plainly here.
    python = os.path.realpath(sys.executable)
    interpreter = [os.path.dirname(python), os.path.realpath(sys.base_prefix)]
    refused = _ungranted(interpreter)
    if refused:
        raise OsSandboxUnavailable(
            f"the sweep's Python ({python}) cannot be granted to the sandbox: "
            f"{', '.join(refused)} is refused ({REFUSAL_RULE}), so the self-test's probe "
            "cannot run in it; run the sweep with a Python installed elsewhere")
    token = secrets.token_hex(6)
    in_clone = os.path.join(clone_real, f".curamoir-os-sandbox-probe-{token}")
    inside_socket = os.path.join(run_real, f"s{token}")
    link_to = os.path.join(run_real, f"l{token}")
    outside = in_outside = listening = made = server = None
    try:
        outside = _outside_directory()
        in_outside = os.path.join(outside, "probe")
        listening = os.path.join(outside, "s")
        home_target, made = _home_target()
        alias = ""
        if sys.platform == "darwin" and home_target and \
                os.path.exists(_DATA_VOLUME + home_target):
            alias = _DATA_VOLUME + home_target
        if any(_inside(os.path.realpath(outside), root) for root in (clone_real, run_real)):
            raise OsSandboxUnavailable(f"the self-test's outside directory {outside} is inside "
                                       "the clone or the run's directory, so it proves nothing")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(listening)
        server.listen(1)
        argv = wrap(["/bin/sh", "-c", _PROBE, "curamoir-os-sandbox-probe", in_clone,
                     in_outside, python, listening, inside_socket, home_target or "/nonexistent",
                     link_to, alias],
                    clone=clone_real, run_dir=run_real, loopback=loopback,
                    read_only=[*read_only, *interpreter])
        probe_env = dict(os.environ if env is None else env, SOCKET_PROBE=_SOCKET_PROBE)
        try:
            done = subprocess.run(argv, cwd=clone_real, env=probe_env, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=_SELF_TEST_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            raise OsSandboxUnavailable(f"the sandbox self-test could not run: {error}") from error
        said = dict(line.split("=", 1) for line in
                    done.stdout.decode("utf-8", "replace").splitlines() if "=" in line)
        why = _failed_check(said, loopback=loopback, home=home_target is not None,
                            link=made is not None, alias=bool(alias))
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
        for path in (in_clone, in_outside, listening, inside_socket, link_to, made):
            if path:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
        if outside is not None:
            shutil.rmtree(outside, ignore_errors=True)


def _outside_directory():
    """A fresh directory for the probe's outside write and host socket, in
    the account's home: visible on the host and not masked by the sandbox's
    own view (bwrap mounts an empty /tmp, where a write or connect would
    fail for want of the path and prove nothing), and denied by both
    sandboxes. Its socket path must fit in about 100 bytes; where the home
    is unwritable or too deep, the temporary directory (then /tmp) instead,
    which on Linux proves less. OSError if none can be made."""
    for where in (*homes()[:1], None, "/tmp"):
        try:
            made = tempfile.mkdtemp(prefix=".curamoir-osbx-" if where else "cm-osbx-",
                                    dir=where)
        except OSError:
            if where is None or where == "/tmp":
                raise
            continue
        if len(os.path.join(made, "s")) <= 100:
            return made
        os.rmdir(made)
    raise OSError("no directory short enough for a Unix socket's path")


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
    try:
        os.write(handle, b"a canary the sandboxed self-test must not read\n")
    except OSError:
        # Not left in the home: the listing is asked instead, as when the
        # file cannot be made.
        with contextlib.suppress(OSError):
            os.unlink(path)
        return found[0], None
    finally:
        os.close(handle)
    return path, path


#: The probe's checks: (name, the value a confining sandbox gives, what any
#: other value means). What each proves: `clone`, that the sandbox lets the
#: run write where it must; `outside`, that writes elsewhere are denied;
#: `net`, that no remote host is reachable; `unix_outside`, that a host
#: daemon's socket (docker, an ssh agent) is not; `unix_inside`, that the
#: run's own sockets (multiprocessing, node IPC) work; `dns`, that no name
#: resolves (mDNSResponder, an exfiltration channel); `home_read`, that the
#: user's home (~/.ssh, keychains, tokens) is unreadable; `home_link`, that
#: a file of it cannot be hard-linked into the run's directory to be read
#: there (asked when the probe made a file in the home); `home_alias`, that
#: it is unreadable as /System/Volumes/Data/<path> too (asked on macOS
#: where that alias exists); `launchctl` and
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
_LINK_CHECK = ("home_link", "denied", "a hard link to a file under the user's home was made "
                                      "in the run's directory and read through it")
_ALIAS_CHECK = ("home_alias", "denied", "a file under the user's home was readable through "
                                        "the Data volume's alias (/System/Volumes/Data/...)")
_DARWIN_CHECKS = (
    ("launchctl", "denied", "/bin/launchctl ran (it could submit a job outside the sandbox)"),
    ("launchservices", "denied", "LaunchServices (com.apple.coreservices.launchservicesd) "
                                 "was reachable: `open` could start an app outside the sandbox"),
)


def _failed_check(said, loopback=False, home=True, platform=None, link=False, alias=False):
    """Which check `said` (the probe's name=value lines) fails, or "".
    `home`, `link` and `alias` say whether the probe had a home to read, a
    file of it to link and an alias of it to read."""
    if not said:
        return "the sandboxed probe printed nothing, so the sandbox did not start"
    checks = list(_CHECKS)
    if home:
        checks.append(_HOME_CHECK)
    if link:
        checks.append(_LINK_CHECK)
    if alias:
        checks.append(_ALIAS_CHECK)
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
