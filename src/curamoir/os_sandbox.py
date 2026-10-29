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
  Unix sockets, Mach services, signals, hard links, writes) plus the rules
  wrap() builds for this run (_darwin_rules): reads under the user's home
  (and its /System/Volumes/Data alias) denied except what granted()
  allows, CLONE and RUN (and the metadata of the directories above them,
  so a path can be resolved); each masked file denied again; and loopback
  when `loopback`. Hard links only within CLONE and RUN, so no file of the
  home's gets an allowed second name. Every path is a `-D` parameter,
  never spliced into the profile text. sandbox-exec is deprecated but
  present on every macOS release, and has no supported command-line
  successor (Chromium and Bazel use it).
- **Linux**: bubblewrap (`bwrap`), deny by default: a fresh root holding
  only the system directories (/usr, /bin, /sbin, /lib*), the parts of /etc
  a runtime reads (_LINUX_ETC), what granted() allows of `read_only`
  (read-only, each masked file covered by /dev/null), a fresh /dev, /proc
  and /tmp, and the clone and the run's directory writable (wrap() gives
  the order and why); new network, pid and IPC namespaces
  (`--unshare-net`: a private loopback that reaches nothing of the host's,
  and no host abstract socket). Nothing else of the host is there, so no
  host socket (/run, /var/run, the home directory) either. bwrap needs
  unprivileged user namespaces; where they are off (a hardened kernel,
  Ubuntu 24.04's AppArmor restriction without bwrap's profile), it exits
  non-zero and self_test() says so. This line has NOT been run on this
  project's development machine (macOS); cm-xy9.7 verifies it on Linux,
  and its self-test is what proves it wherever it runs.
- **Anything else**, or the tool missing: OsSandboxUnavailable. Nothing
  here falls back to running unsandboxed: the gate refuses instead.

Before a gate's first run, self_test() runs a probe through the SAME wrap()
argv path (_CHECKS lists what each check proves). Any deviation (including
the sandbox tool failing to start, as sandbox-exec does inside another
sandbox) is OsSandboxUnavailable naming the check.

Commands are argv lists throughout, never a shell string: the research on
this bead found that a command smuggled through a string ran unsandboxed.

What of `read_only` a run may read is decided in one place, granted():
its docstring is the one full list of the rules, and REFUSAL_RULE says
them to the user. A grant's files that may hold a token (_MASKED: pip.conf,
.condarc, .npmrc, conda-meta/state) stay unreadable (_masked): denied on
macOS, covered by /dev/null on Linux.

Limits, stated: on macOS, reads OUTSIDE the user's home stay open (system
libraries, /opt, /usr/local, other volumes), and a run can still read what
it was granted under the home (the reused venv; never a home, nor anything
in or holding a credential tree). A machine that is offline passes the
network and DNS checks without proving the sandbox denies them; the other
checks still prove the sandbox is on. Grants are walked once, before a
gate's runs (_walk): a token file the user's own processes add to a grant
mid-gate (`npm install`, `pip config set --site` in the source checkout) is
not masked for the rest of that gate; nothing inside the sandbox can write
a grant.
"""

import contextlib
import contextvars
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

#: Linux: where a conda root is looked for under the system directories
#: bwrap binds whole (its children: /usr/local/miniconda3), as no grant
#: need name it to be visible. One deeper is not looked for (a stated
#: limit); macOS's reads outside the home are open anyway.
_SYSTEM_CONDA_PARENTS = ("/usr", "/usr/local")

#: Where the sandbox tools are taken from: fixed system locations, never the
#: sweep's PATH (a directory early on it could hold a `sandbox-exec` that
#: confines nothing). /usr/local/bin is root's on a Linux system.
_SANDBOX_EXEC = ("/usr/bin/sandbox-exec",)
_BWRAP = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap")

#: macOS's Data volume, where /Users (and /private, /opt, /usr/local) really
#: live: a home is also reachable as /System/Volumes/Data/<home>, through
#: the firmlink. On macOS 14.2 the kernel matches the canonical /Users path
#: whichever spelling was opened (tested: a read through the alias, through
#: /.vol/<device>/<inode> and through /Volumes/Macintosh HD is denied by the
#: /Users rule, and a rule naming the alias matches nothing), so the alias
#: rule _darwin_rules adds is a second line should a release match the
#: path as given; self_test's `home_alias` check proves the alias denied.
_DATA_VOLUME = "/System/Volumes/Data"

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
#: in the run's directory to hard-link it to, its path through the Data
#: volume's firmlink ("" where there is none), and a masked file in a
#: granted venv or prefix ("" where there is none).
_SOCKET_PROBE = """
import os, socket, sys
outside, inside, home, link_to, alias, masked = sys.argv[1:7]

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

def masked_read():
    # Linux masks with /dev/null: an empty read is the mask (the file read
    # is one the host sees non-empty).
    if not open(masked, "rb").read():
        raise OSError("read as empty: masked")

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
check("masked_read", masked_read, "read", "denied")
if sys.platform == "darwin":
    check("launchservices", launchservices, "reached", "denied")
"""


class OsSandboxUnavailable(Exception):
    """No OS sandbox can confine the test run here (unsupported platform,
    tool missing, or self_test() found it not confining). The gate turns it
    into a refusal: the test command is never run unsandboxed."""


#: The walks of the gate in progress (walks_cached), or None: wrap() is
#: called for the self-test, the canary and each run, on the same grants.
_walks = contextvars.ContextVar("curamoir_os_sandbox_walks", default=None)


@contextlib.contextmanager
def walks_cached():
    """Within it, each grant is walked (_walk) once: green_gate holds one
    for a whole gate, wrap() and self_test() (as their decorator) one for
    their own call. Nested, the outer one's walks are used. Safe for a
    gate's length: a run cannot write to a grant (writes go only to the
    clone and the run's directory)."""
    if _walks.get() is not None:
        yield
        return
    token = _walks.set({})
    try:
        yield
    finally:
        _walks.reset(token)


@walks_cached()
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
        # The mounts, in order. bwrap applies them in turn and a later one
        # covers an earlier one at the same or an enclosing path, so the
        # order is the policy: (1) the system directories and (2) the parts
        # of /etc, read-only; (3) the grants, read-only; (4) each masked
        # file, /dev/null over it, after the grant holding it; (5) the run's
        # own /dev, /proc and /tmp, after the grants, so nothing granted can
        # stand in for them (granted() refuses those trees besides); (6) the
        # clone and the run's directory, writable, last: mkdtemp makes both
        # in the system temporary directory, usually /tmp, so they must come
        # after (5)'s tmpfs or it would hide them.
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
        grants = granted(read_only)
        for path in grants:
            line += ["--ro-bind-try", path, path]
        # --ro-bind, not -try: a mask that cannot be made fails the run
        # rather than leaving the file readable.
        for path in _masked(grants):
            line += ["--ro-bind", "/dev/null", path]
        line += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        return [*line, "--bind", clone, clone, "--bind", run_dir, run_dir,
                "--unshare-net", "--unshare-pid", "--unshare-ipc",
                "--die-with-parent", "--new-session", "--chdir", clone, "--", *argv]
    raise OsSandboxUnavailable(f"no OS sandbox is supported on {sys.platform!r} (only macOS "
                               "sandbox-exec and Linux bwrap)")


def _require_home():
    """OsSandboxUnavailable when no home directory is found: granted() then
    has none to refuse (nor its credential trees), and on macOS reads under
    it cannot be denied."""
    if not homes():
        raise OsSandboxUnavailable("no home directory was found, so reads under it cannot "
                                   "be denied, nor a grant of it or its credentials refused")


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
#: is ever granted (granted()). All of ~/.config: gh, gcloud, git's
#: credential store and many more keep tokens there, and no version
#: manager installs a toolchain there (mise's live in ~/.local/share/mise).
#: All of ~/Library: keychains, cookies, application tokens (a macOS
#: framework Python's user base, ~/Library/Python/X.Y, is refused with it).
#: pip's and conda's user configuration (an index's or a channel's token)
#: is named too, though ~/.config and ~/Library already hold some of it.
_CREDENTIAL_TREES = tuple(os.path.join(*parts.split("/")) for parts in (
    ".ssh", ".aws", ".gnupg", ".config", ".local/share/keyrings", "Library", ".netrc",
    ".docker", ".kube", ".pypirc", ".pip", ".config/pip", "Library/Application Support/pip",
    ".condarc"))


#: Under a home, where version managers keep their installed versions:
#: what is IN one of these (a version, ~/.pyenv/versions/3.12.1, or
#: anything in it) is on the allowlist (_allowlisted; granted() has the
#: whole rule set). Never the manager's whole root (~/.cargo holds
#: credentials.toml, ~/.pyenv and ~/.asdf their shims and plugins), and
#: never the directory itself: it holds every version, one of which may be a
#: conda root (versions/miniconda3-latest). So a shim (pyenv's, asdf's,
#: volta's) that reads its manager's root is not granted and fails: the
#: base is red or the command not runnable.
_TOOLCHAIN_DIRS = tuple(os.path.join(*parts.split("/")) for parts in (
    ".pyenv/versions", ".nvm/versions", ".asdf/installs", ".volta/tools", ".rustup/toolchains",
    ".rbenv/versions", ".sdkman/candidates", ".local/share/uv/python",
    ".local/share/mise/installs"))
#: Under a home, directories of tools rather than versions, granted
#: themselves as well as anything in them.
_TOOL_DIRS = (os.path.join(".cargo", "bin"),)

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

#: granted()'s rules, said to the user where a refusal is reported (the
#: directories named from the constants, so the message cannot drift from
#: them).
REFUSAL_RULE = (
    "under a home only an installed version in a toolchain directory ("
    + ", ".join("~/" + directory for directory in _TOOLCHAIN_DIRS) + "), "
    + ", ".join("~/" + directory for directory in _TOOL_DIRS)
    + ", a venv, a conda environment's bin or lib, or a node_modules, or anything in one, "
    "is granted; of a conda environment anywhere only its bin and lib; never a home or "
    "anything holding one, nor anything in or holding a credential tree (~/.ssh, "
    "~/.config, ~/Library, ...) or a socket or kernel directory (/tmp, /run, /dev, "
    "/proc, ...); nor a directory holding a conda environment or a nested venv, holding "
    "a directory that cannot be listed, or too large to check")

#: Of a conda environment (a prefix holding conda-meta/: a conda root, or
#: one of its envs/<name>), all that is ever granted: the root holds
#: .condarc (channel tokens), conda-meta/state (`conda env config vars`),
#: etc/conda/activate.d, pkgs and the other environments.
_CONDA_GRANTED = ("bin", "lib")

#: Files that may hold an index's or a channel's token, masked wherever
#: they are in a granted path (_masked): pip's site configuration
#: ($VIRTUAL_ENV/pip.conf, pip.ini on Windows), an upload's .pypirc,
#: conda's (and mamba's) per-prefix .condarc, condarc and .mambarc and its
#: environment variables (conda-meta/state), npm's global configuration
#: (<prefix>/etc/npmrc) and a project's .npmrc. And every file in a
#: directory of _MASKED_DIRS: conda's per-prefix condarc.d/.
_MASKED = (".condarc", "condarc", ".mambarc", "pip.conf", "pip.ini", ".pypirc", ".npmrc",
           os.path.join("conda-meta", "state"), os.path.join("etc", "npmrc"))
_MASKED_DIRS = ("condarc.d",)

#: A grant's walk (_walk): how deep and over how many entries in all it
#: reads before it gives up. A grant it gives up on is refused, never
#: granted partly checked. A scandir walk reads about 3-6us an entry (a uv
#: CPython, ~2,500 entries, in under 10ms on macOS 14), and each grant is
#: walked once a gate (walks_cached). The cap is sized for a monorepo's
#: node_modules (150-400k entries is common): about 12s at worst, once; a
#: tree beyond it is refused, and the refusal says it was too large to check.
_WALK_DEPTH = 64
_WALK_ENTRIES = 2_000_000


def granted(paths):
    """Those of `paths` (wrap()'s `read_only`) a run may read, resolved,
    sorted and deduplicated. This docstring is THE list of the rules: the
    module docstring, REFUSAL_RULE and green_gate point here. Each path
    goes through, in order:

    1. It must be absolute and exist; anything else is dropped.
    2. It is resolved (_canonical: realpath, and on macOS the kernel's own
       spelling).
    3. In a conda environment it is translated (_conda_parts): the prefix
       becomes its bin and lib, and anything else of the environment is
       dropped. Here, the one place every grant passes, so a conda root is
       never granted whoever asked for it.
    4. It is never `/`.
    5. It is refused by where it is (_refused_by_place): in or holding a
       socket or kernel tree; a home, or holding one; in or holding a
       credential tree (_CREDENTIAL_TREES); or under a home but not on the
       allowlist (_allowlisted): in a toolchain directory (_TOOLCHAIN_DIRS,
       never the directory itself), ~/.cargo/bin or in it (_TOOL_DIRS), a
       venv (a directory holding pyvenv.cfg) or in one, a conda
       environment's bin or lib or in one (never the home's own), or a
       node_modules or in one. Outside every home, nothing more is asked.
    6. It is refused by what it holds (_walk gives None): a conda
       environment anywhere in it; a venv anywhere but its top or
       envs/<name>; a directory that cannot be listed; or more than
       _WALK_DEPTH deep or _WALK_ENTRIES entries ("too large to check").
       A grant in a system directory is not walked (_walk says why).

    Step 5 compares by identity (device, inode) of the path and its
    parents, so neither case (`~/.SSH` on a case-insensitive volume) nor an
    alias (/System/Volumes/Data/Users/...) slips past. A refused path is
    left out silently: the run cannot read it, and fails if it needed it
    (green_gate names it, through ungranted()). What stays unreadable
    inside a grant is _masked's."""
    found = set()
    every_home = homes()
    for path in paths:
        if not path or not os.path.isabs(path) or not os.path.exists(path):
            continue
        for real in _conda_parts(_canonical(path)):
            if real == "/" or _refused_by_place(real, every_home):
                continue
            if _walk(real) is not None:
                found.add(real)
    return sorted(found)


def _conda_parts(path):
    """`path` (resolved) as the list of what of it may be granted:

    - outside any conda environment: [path];
    - the environment's prefix (_conda_prefix) itself: its bin and lib
      (_CONDA_GRANTED), those that exist;
    - in the environment: [path] if it is in its bin or lib, else []
      (conda-meta, etc, pkgs, envs, include, .condarc are never granted)."""
    prefix = _conda_prefix(path)
    if prefix is None:
        return [path]
    parts = [os.path.join(prefix, name) for name in _CONDA_GRANTED]
    if path == prefix:
        return [_canonical(part) for part in parts if os.path.isdir(part)]
    return [path] if any(_inside(path, part) for part in parts) else []


def _conda_prefix(path):
    """The conda environment `path` is or is in (the nearest directory at or
    above it holding conda-meta/), or None."""
    while True:
        if os.path.isdir(os.path.join(path, "conda-meta")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _walk(grant):
    """What `grant` (a resolved directory or file) holds, as granted()
    step 6 and _masked need it. Two answers:

    - None: the grant is REFUSED. The walk found a conda environment
      (conda-meta) anywhere in it, whose root would be granted, which
      _conda_parts never allows; or a venv (pyvenv.cfg) anywhere but the
      grant's top and its envs/<name> (pyenv-virtualenv's venvs in a
      version), whose own pip.conf would need masking though no grant of a
      toolchain needs it; or a directory it cannot list (an unlistable one
      may still be read by name); or it passed _WALK_DEPTH or
      _WALK_ENTRIES.
    - a tuple: the grant may be read, and these are the files in it to
      mask (_MASKED, _MASKED_DIRS; resolved), possibly none.

    No link is followed: what one points to is read by its own path, which
    is denied unless granted (and walked) itself, or outside the home on
    macOS (open, as stated). Two kinds of grant are not walked. A single
    file has nothing in it: it is its own mask when it is a token file. A
    grant inside a system directory (_LINUX_SYSTEM) is visible to the run
    whatever is decided here (Linux binds the whole system directory;
    macOS leaves reads outside the home open), so refusing it would hide
    nothing: only the token files at its top are masked (_fixed_masks),
    and it is never refused for holding a conda environment or a venv."""
    cache = _walks.get()
    if cache is not None and grant in cache:
        return cache[grant]
    if not os.path.isdir(grant):
        # A file granted by name: masked when it is itself a token file
        # (a venv's pip.conf asked for directly), so granting it reads
        # nothing.
        found = (grant,) if _is_masked(tuple(grant.split(os.sep))) else ()
    elif _in_system(grant):
        found = tuple(_fixed_masks(grant))
    else:
        found = _walked(grant)
    if cache is not None:
        cache[grant] = found
    return found


def _walked(grant):
    """_walk's walk of the directory `grant`: a tuple, or None (refused)."""
    found, pending, entries = set(), [(grant, ())], 0
    while pending:
        directory, parts = pending.pop()
        try:
            with os.scandir(directory) as listing:
                children = list(listing)
        except OSError:
            return None
        entries += len(children)
        if entries > _WALK_ENTRIES:
            return None
        for child in children:
            if child.name == "conda-meta":
                return None
            if child.name == "pyvenv.cfg" and parts and \
                    not (len(parts) == 2 and parts[0] == "envs"):
                return None
            try:
                is_directory = child.is_dir(follow_symlinks=False)
            except OSError:
                return None
            if is_directory:
                if len(parts) + 1 > _WALK_DEPTH:
                    return None
                pending.append((child.path, (*parts, child.name)))
            elif _is_masked((*parts, child.name)) and os.path.isfile(child.path):
                found.add(_canonical(child.path))
    return tuple(sorted(found))


def _is_masked(parts):
    """Whether a file at `parts` (its path's components) is one of _MASKED
    or in one of _MASKED_DIRS."""
    if len(parts) >= 2 and parts[-2] in _MASKED_DIRS:
        return True
    return any(tuple(name.split(os.sep)) == tuple(parts[-len(name.split(os.sep)):])
               for name in _MASKED)


def _fixed_masks(place):
    """The masked files (_MASKED, every file of _MASKED_DIRS) at `place`'s
    top, resolved: where pip, conda and npm look in a prefix."""
    found = []
    for name in _MASKED:
        candidate = os.path.join(place, name)
        if os.path.isfile(candidate):
            found.append(_canonical(candidate))
    for name in _MASKED_DIRS:
        with contextlib.suppress(OSError):
            directory = os.path.join(place, name)
            found += [_canonical(os.path.join(directory, entry))
                      for entry in os.listdir(directory)
                      if os.path.isfile(os.path.join(directory, entry))]
    return found


def _in_system(path):
    """Whether `path` (resolved) is in one of _LINUX_SYSTEM (by its real
    path: /bin is /usr/bin on a merged-/usr system)."""
    return any(os.path.isdir(root) and _inside(path, os.path.realpath(root))
               for root in _LINUX_SYSTEM)


def _masked(grants):
    """The files a run must not read though they are visible to it,
    resolved, sorted; only files that exist. Three sources:

    - those _walk found in each of `grants`;
    - those at the top of the conda environment (_conda_prefix) each grant
      is in, and of each of its envs/<name> (_fixed_masks: .condarc,
      conda-meta/state). On macOS for every such prefix: outside the home
      its top is readable. On Linux only for a prefix in a system
      directory, which bwrap binds whole: any other prefix is visible only
      through grants, and of it granted() grants only bin and lib, so its
      top files are not in the sandbox to mask;
    - on Linux, those of a conda root under _SYSTEM_CONDA_PARENTS, visible
      with the system directory though no grant names it.

    Masks are by path: a hard link to a masked file is not masked, but
    writes to a grant are denied, so no run can make one."""
    linux = sys.platform.startswith("linux")
    found = set()
    prefixes = set()
    for path in grants:
        found.update(_walk(path) or ())
        prefix = _conda_prefix(path)
        if prefix is not None and (not linux or _in_system(prefix)):
            prefixes.add(prefix)
    if linux:
        for parent in _SYSTEM_CONDA_PARENTS:
            with contextlib.suppress(OSError):
                prefixes.update(os.path.join(parent, name) for name in os.listdir(parent)
                                if os.path.isdir(os.path.join(parent, name, "conda-meta")))
    for prefix in prefixes:
        places = [prefix]
        with contextlib.suppress(OSError):
            envs = os.path.join(prefix, "envs")
            places += [os.path.join(envs, name) for name in os.listdir(envs)]
        for place in places:
            found.update(_fixed_masks(place))
    return sorted(found)


def ungranted(paths):
    """Those of `paths` (absolute, existing; resolved, sorted, deduplicated)
    that granted() refuses: what a run cannot read, to name in a refusal."""
    return sorted({_canonical(path) for path in paths
                   if path and os.path.isabs(path) and os.path.exists(path)
                   and not granted([path])})


def _refused_by_place(path, every_home):
    """Whether `path` (resolved) is refused for where it is: granted()
    step 5. Not the whole check: a conda environment's parts (step 3) and
    what the path holds (step 6, _walk) are decided elsewhere. Refused, in
    this order, when it is:

    1. in or holding a socket or kernel tree (_SOCKET_TREES,
       _KERNEL_TREES), by either spelling of it (the tree, or its
       /System/Volumes/Data alias on macOS);
    2. a home of `every_home`, or a directory holding one;
    3. in or holding one of a home's credential trees (_near_credentials);
    4. under a home but not on its allowlist (_allowlisted)."""
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
    """Whether `path` (under `home`) is on the home's allowlist (granted()
    step 5): in a toolchain directory (_TOOLCHAIN_DIRS, not the directory
    itself), ~/.cargo/bin or in it (_TOOL_DIRS), or a venv, a conda
    environment's bin or lib, or a node_modules, or in one."""
    me = _identity(path)
    for directory in _TOOLCHAIN_DIRS:
        identity = _identity(os.path.join(home, directory))
        if identity is not None and identity in lineage and identity != me:
            return True  # in it, never the directory itself
    for directory in _TOOL_DIRS:
        identity = _identity(os.path.join(home, directory))
        if identity is not None and identity in lineage:
            return True
    while _identity(path) not in (home_id, None):
        parent = os.path.dirname(path)
        if os.path.basename(path) == "node_modules" or \
                os.path.isfile(os.path.join(path, "pyvenv.cfg")):
            return True
        # Never the home's own bin or lib, though it hold a conda-meta/.
        if os.path.basename(path) in _CONDA_GRANTED and _identity(parent) != home_id and \
                os.path.isdir(os.path.join(parent, "conda-meta")):
            return True
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
    path cannot be resolved without it, and a listing is not metadata);
    then each masked file of every grant (_masked) denied again, wherever
    it is. Later rules win in SBPL, so the allows follow the denies and the
    masks the allows."""
    rules, params = [], []

    def param(prefix, value):
        name = f"{prefix}{len(params)}"
        params.append((name, value))
        return f'(param "{name}")'

    grants = granted(read_only)
    under = []
    for home in homes():
        rules.append(f"(deny file-read* (subpath {param('HOME', home)}))")
        alias = _DATA_VOLUME + home
        if not _inside(home, _DATA_VOLUME) and os.path.isdir(alias):
            rules.append(f"(deny file-read* (subpath {param('ALIAS', alias)}))")
        allowed = [p for p in [*grants, clone, run_dir] if _inside(p, home)]
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
    for path in _masked(grants):
        rules.append(f"(deny file-read* (literal {param('MASK', path)}))")
    if loopback:
        rules += ['(allow network-bind network-inbound (local ip "localhost:*"))',
                  '(allow network-outbound (remote ip "localhost:*"))']
    return "\n" + "\n".join(rules) + "\n", params


def _inside(path, root):
    return os.path.commonpath([root, path]) == root


@walks_cached()
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
    refused = ungranted(interpreter)
    if refused:
        raise OsSandboxUnavailable(
            f"the sweep's Python ({python}) cannot be granted to the sandbox: "
            f"{', '.join(refused)} is refused, so the self-test's probe cannot run in it; "
            "run the sweep with a Python installed outside your home or as a toolchain's "
            f"installed version (or a venv made from one). The rule: {REFUSAL_RULE}")
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
        masked = _mask_target([*read_only, *interpreter])
        if any(_inside(os.path.realpath(outside), root) for root in (clone_real, run_real)):
            raise OsSandboxUnavailable(f"the self-test's outside directory {outside} is inside "
                                       "the clone or the run's directory, so it proves nothing")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(listening)
        server.listen(1)
        argv = wrap(["/bin/sh", "-c", _PROBE, "curamoir-os-sandbox-probe", in_clone,
                     in_outside, python, listening, inside_socket, home_target or "/nonexistent",
                     link_to, alias, masked],
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
                            link=made is not None, alias=bool(alias), mask=bool(masked))
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


def _mask_target(read_only):
    """A masked file (_masked) of what `read_only` grants for the probe to
    try to read: one the host reads non-empty, so an empty read inside is
    the mask (Linux's /dev/null) and not the file. "" where there is none:
    the check is then not asked, as nothing is masked to prove."""
    for path in _masked(granted(read_only)):
        try:
            with open(path, "rb") as handle:
                if handle.read(1):
                    return path
        except OSError:
            continue
    return ""


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
#: where that alias exists); `masked_read`, that a masked file in a granted
#: venv or prefix (pip.conf, .condarc) is unreadable though its directory is
#: granted (asked when one exists); `launchctl` and `launchservices`
#: (macOS), that neither a launchd job nor an app can be started outside
#: the sandbox; `loopback` (macOS), that 127.0.0.1 is reachable exactly when
#: asked for.
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
_MASK_CHECK = ("masked_read", "denied", "a file masked in a granted venv or prefix (pip.conf, "
                                       ".condarc, ...: an index's or a channel's token) was "
                                       "readable")
_DARWIN_CHECKS = (
    ("launchctl", "denied", "/bin/launchctl ran (it could submit a job outside the sandbox)"),
    ("launchservices", "denied", "LaunchServices (com.apple.coreservices.launchservicesd) "
                                 "was reachable: `open` could start an app outside the sandbox"),
)


def _failed_check(said, loopback=False, home=True, platform=None, link=False, alias=False,
                  mask=False):
    """Which check `said` (the probe's name=value lines) fails, or "".
    `home`, `link`, `alias` and `mask` say whether the probe had a home to
    read, a file of it to link, an alias of it to read and a masked file to
    read."""
    if not said:
        return "the sandboxed probe printed nothing, so the sandbox did not start"
    checks = list(_CHECKS)
    if home:
        checks.append(_HOME_CHECK)
    if link:
        checks.append(_LINK_CHECK)
    if alias:
        checks.append(_ALIAS_CHECK)
    if mask:
        checks.append(_MASK_CHECK)
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
