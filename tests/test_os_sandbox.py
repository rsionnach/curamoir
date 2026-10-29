"""cm-p2f: the E3-R4 test run executes under an OS sandbox (os_sandbox), or
not at all.

Three groups:

1. **The real sandbox** (macOS sandbox-exec here; Linux bwrap where it is
   installed). Each skip names why, and on macOS
   test_macos_has_sandbox_exec is never skipped, so a silent skip cannot
   hide the real tests on the platform this project is developed on. They
   run the gate (run_test_gate) with the real green_gate.OS_SANDBOX and
   prove: a write outside the clone fails, an outbound connect fails, a
   host Unix socket is unreachable, while a pytest run in the clone,
   loopback, HOME/TMPDIR writes and a socket in TMPDIR all work.
2. **Fail closed**: with the tool unavailable (_is_executable, sys.platform)
   the gate raises GateError and the sweep refuses TEST_GATE, and no test
   command ran (run_in_group is never called).
3. **A self-test failure** (wrap() made a pass-through, so the probe runs
   unconfined) is a refusal too, with nothing of the project's run.

Fixture provenance: the repositories are tests/test_green_gate.py's
one-commit repos (repo_of) and tests/test_sweep_job.py's committed sample
(checkout); the outside targets are tmp_path files the test creates and
checks, so what the sandbox blocked is observed, not assumed.
"""

import itertools
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from curamoir import green_gate, os_sandbox
from curamoir.git_guard import prepare_sandbox
from curamoir.green_gate import GREEN, GateError, run_test_gate
from curamoir.guards.reasons import Reason
from curamoir.job_run import REFUSED
from tests.os_sandbox_stand_in import PREFIX, Unconfined
from tests.test_green_gate import THREE, fix, repo_of
from tests.test_lint import NO_BARE_PRINT
from tests.test_sweep_job import (  # noqa: F401  (isolated_home is an autouse fixture)
    checkout,
    isolated_home,
)
from tests.test_sweep_publish import (  # noqa: F401  (quick_lookups is an autouse fixture)
    GATE,
    fixer,
    nothing_pushed,
    quick_lookups,
    sweep,
)

PY = sys.executable

if sys.platform == "darwin":
    _TOOL = "sandbox-exec"
elif sys.platform.startswith("linux"):
    _TOOL = "bwrap"
else:
    _TOOL = None

real_sandbox = pytest.mark.skipif(
    _TOOL is None or os_sandbox.tool_path() is None,
    reason=(f"no OS sandbox on {sys.platform}: these run only on macOS (sandbox-exec) or "
            "Linux with bwrap installed" if _TOOL is None else
            f"{_TOOL} is not installed in a system directory on this {sys.platform} machine"))


@pytest.fixture(autouse=True)
def tests_venv_is_the_reused_one(monkeypatch):
    """The commands here name this interpreter (PY) by its path. The OS
    sandbox lets a run read under the home only what the gate grants
    (green_gate._read_only), which follows VIRTUAL_ENV: name this venv, as
    an activated one would be, so its interpreter's install is granted."""
    if sys.prefix != sys.base_prefix:
        monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)


@pytest.fixture
def no_project_command(monkeypatch):
    """A spy on green_gate.run_in_group: every call is a run of something of
    the project's (the canary or the test command). Returns the calls."""
    calls = []
    real = green_gate.run_in_group

    def spy(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(green_gate, "run_in_group", spy)
    return calls


def gate(tmp_path, command):
    """run_test_gate on a three-file repo whose committed test command is
    `command`, with one fix; the GateResult."""
    repo = repo_of(tmp_path, THREE, command)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        return run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))


# --- 1. the real sandbox ----------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="checks macOS's sandbox-exec; this is "
                                                    f"{sys.platform}")
def test_macos_has_sandbox_exec():
    """Never skipped on macOS: the real-sandbox tests below cannot be
    skipped there by a missing tool without this failing."""
    assert os_sandbox.tool_path() == "/usr/bin/sandbox-exec", \
        "macOS without sandbox-exec: the gate would refuse"
    assert os.path.isfile(os_sandbox.PROFILE)


def test_the_gate_uses_the_real_sandbox_by_default():
    assert green_gate.OS_SANDBOX is os_sandbox


@real_sandbox
def test_the_self_test_passes_and_leaves_nothing(tmp_path):
    clone, run = tmp_path / "clone", tmp_path / "run"
    clone.mkdir()
    run.mkdir()
    os_sandbox.self_test(clone, run)
    assert list(clone.iterdir()) == [] and list(run.iterdir()) == []


@real_sandbox
def test_a_test_command_cannot_write_outside_the_clone(tmp_path, no_project_command):
    outside = tmp_path / "escaped"
    result = gate(tmp_path, f"echo pwned > {outside}; true")
    assert result.strategy == GREEN
    assert not outside.exists(), "the test command wrote outside the clone"
    assert no_project_command and all(argv[0] == os_sandbox.tool_path()
                                      for argv in no_project_command)


@real_sandbox
def test_a_conftest_writing_outside_the_clone_is_stopped(tmp_path):
    """The shape of the boundary test's canary: test code (a conftest.py the
    project's pytest imports) writes outside; the write fails, and the
    tests still pass."""
    canary = tmp_path / "canary"
    files = dict(THREE)
    files["tests/conftest.py"] = (f"try:\n    open({str(canary)!r}, 'w').write('ran')\n"
                                  "except OSError:\n    pass\n")
    files["tests/test_ok.py"] = "def test_ok():\n    assert True\n"
    repo = repo_of(tmp_path, files, f"{PY} -m pytest -q -p no:cacheprovider tests")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    assert result.strategy == GREEN, result.runs[-1].tail
    assert not canary.exists()


@real_sandbox
def test_an_outbound_connection_fails(tmp_path):
    script = tmp_path / "net.py"
    script.write_text("import socket, sys\ns = socket.socket(); s.settimeout(3)\n"
                      "try:\n    s.connect(('1.1.1.1', 53))\nexcept OSError:\n"
                      "    sys.exit(0)\nsys.exit('connected')\n")
    result = gate(tmp_path, f"{PY} {script}")
    assert result.strategy == GREEN, result.runs[-1].tail


@real_sandbox
def test_a_host_unix_socket_is_unreachable(tmp_path):
    """A daemon's socket outside the clone (the docker socket, an ssh agent)
    stands here as one this test listens on."""
    path = os.path.join(os.path.realpath(tmp_path), "d.sock")
    if len(path) > 100:
        path = os.path.join("/tmp", f"cm-{os.getpid()}.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    try:
        probe = ("import socket, sys\nu = socket.socket(socket.AF_UNIX)\n"
                 f"try:\n    u.connect({path!r})\nexcept OSError:\n    sys.exit(0)\n"
                 "sys.exit('connected')\n")
        script = tmp_path / "probe.py"
        script.write_text(probe)
        result = gate(tmp_path, f"{PY} {script}")
    finally:
        server.close()
        os.unlink(path)
    assert result.strategy == GREEN, result.runs[-1].tail


@real_sandbox
def test_home_tmpdir_and_a_local_socket_work(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, socket\n"
        "path = os.path.join(os.environ['TMPDIR'], 's')\n"
        "u = socket.socket(socket.AF_UNIX); u.bind(path); u.listen(1)\n"
        "socket.socket(socket.AF_UNIX).connect(path)\n"
        "open(os.path.join(os.environ['HOME'], 'h'), 'w').write('ok')\n")
    result = gate(tmp_path, f"{PY} {probe}")
    assert result.strategy == GREEN, result.runs[-1].tail


LOOPBACK = ("import socket, sys\n"
            "try:\n"
            "    server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1)\n"
            "    socket.create_connection(server.getsockname(), timeout=3).close()\n"
            "except OSError:\n    print('loopback=denied')\nelse:\n    print('loopback=ok')\n")


@real_sandbox
@pytest.mark.parametrize("allowed", [False, True])
def test_loopback_follows_sweep_test_loopback(tmp_path, allowed):
    """macOS denies loopback unless the base's config allows it; Linux's
    private loopback is always there (and reaches nothing of the host's)."""
    probe = tmp_path / "loopback.py"
    probe.write_text(LOOPBACK)
    extra = "  test_loopback: true\n" if allowed else ""
    repo = repo_of(tmp_path, THREE, f"{PY} {probe}", extra=extra)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    assert result.strategy == GREEN, result.runs[-1].tail
    works = allowed or sys.platform != "darwin"
    assert f"loopback={'ok' if works else 'denied'}" in result.runs[-1].tail


@pytest.mark.skipif(sys.platform != "darwin", reason="hard links into the run and the Data "
                                                    "volume's alias are macOS's paths to "
                                                    f"the home; this is {sys.platform}")
@real_sandbox
def test_the_home_is_not_reachable_by_a_hard_link_nor_its_data_volume_alias(tmp_path):
    """A file of the account's home cannot be hard-linked into the clone (and
    read through the link) nor read as /System/Volumes/Data/<path>, while a
    hard link inside the clone still works. The canary is removed after."""
    home = os_sandbox.homes()[0]
    canary = Path(home) / f".curamoir-link-probe-{os.getpid()}"
    canary.write_text("secret\n")
    try:
        probe = tmp_path / "link.py"
        probe.write_text(
            "import os, sys\n"
            "canary = sys.argv[1]\n"
            "def attempt(name, action):\n"
            "    try:\n        action(); print(name, 'worked')\n"
            "    except OSError:\n        print(name, 'denied')\n"
            "attempt('home_link', lambda: (os.link(canary, 'x'), open('x').read()))\n"
            "attempt('alias', lambda: open('/System/Volumes/Data' + canary).read())\n"
            "open('own', 'w').write('mine')\n"
            "attempt('own_link', lambda: os.link('own', 'own2'))\n")
        result = gate(tmp_path, f"{PY} {probe} {canary}")
    finally:
        canary.unlink()
    tail = result.runs[-1].tail
    assert result.strategy == GREEN, tail
    assert "home_link denied" in tail and "alias denied" in tail, tail
    assert "own_link worked" in tail, tail


@real_sandbox
def test_the_users_home_is_unreadable(tmp_path):
    """The account's home (~/.ssh, keychains, tokens), not the run's fresh
    HOME: a listing of it fails, while the granted venv still runs."""
    home = os_sandbox.homes()[0]
    result = gate(tmp_path, f"if ls {home} >/dev/null 2>&1; then exit 1; fi; {PY} -c pass")
    assert result.strategy == GREEN, result.runs[-1].tail


@pytest.mark.skipif(sys.platform != "darwin", reason="Mach services, launchd and "
                                                    f"LaunchServices are macOS's; this is "
                                                    f"{sys.platform}")
@real_sandbox
def test_no_service_acting_outside_the_sandbox_is_reachable(tmp_path):
    """launchd (launchctl), LaunchServices (`open`), AppleEvents
    (osascript) and mDNSResponder (DNS) act outside the sandbox: none is
    reachable. Each attempt is harmless if it were not denied (a version
    query, a look-up, a name resolution)."""
    probe = tmp_path / "ipc.py"
    probe.write_text(
        "import ctypes, socket, subprocess\n"
        "for tool in (['/bin/launchctl', 'version'], ['/usr/bin/osascript', '-e', '1'],\n"
        "             ['/usr/bin/open', '-h']):\n"
        "    try:\n"
        "        ran = subprocess.run(tool, capture_output=True).returncode == 0\n"
        "    except OSError:\n"
        "        ran = False\n"
        "    print(tool[0], 'ran' if ran else 'denied')\n"
        "lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')\n"
        "port = ctypes.c_uint(0); bp = ctypes.c_uint.in_dll(lib, 'bootstrap_port')\n"
        "for name in (b'com.apple.coreservices.launchservicesd', b'com.apple.mDNSResponder'):\n"
        "    print(name.decode(), 'reached' if lib.bootstrap_look_up(bp, name,\n"
        "          ctypes.byref(port)) == 0 else 'denied')\n"
        "try:\n    socket.getaddrinfo('example.com', 443); print('dns resolved')\n"
        "except OSError:\n    print('dns denied')\n")
    result = gate(tmp_path, f"{PY} {probe}")
    tail = result.runs[-1].tail
    assert result.strategy == GREEN, tail
    assert "ran" not in tail.split() and "reached" not in tail.split() and \
        "resolved" not in tail.split(), tail
    assert tail.count("denied") == 6, tail


@real_sandbox
def test_a_pytest_suite_in_the_clone_passes(tmp_path):
    files = dict(THREE)
    files["tests/test_a.py"] = ("import sys, pathlib\n"
                                "def test_a(tmp_path):\n"
                                "    (tmp_path / 'x').write_text('x')\n"
                                "    assert True\n")
    repo = repo_of(tmp_path, files, f"{PY} -m pytest -q tests")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    assert result.strategy == GREEN, result.runs[-1].tail
    assert "1 passed" in result.runs[-1].tail


# --- wrap -------------------------------------------------------------------------------

def test_wrap_on_macos_is_sandbox_exec_with_real_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: True)
    (tmp_path / "c").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "c")
    argv = os_sandbox.wrap(["/bin/sh", "-c", "x"], clone=link, run_dir=tmp_path)
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert argv[2].startswith(Path(os_sandbox.PROFILE).read_text(encoding="utf-8"))
    assert argv[3:7] == ["-D", f"CLONE={os.path.realpath(tmp_path / 'c')}",
                         "-D", f"RUN={os.path.realpath(tmp_path)}"]
    assert argv[-3:] == ["/bin/sh", "-c", "x"]
    assert "localhost" not in argv[2], "loopback is off by default"
    for rule in ('(deny file-link)', '(allow file-link (subpath (param "CLONE")))',
                 '(allow file-link (subpath (param "RUN")))'):
        assert rule in argv[2]


def test_the_macos_profile_denies_the_home_but_the_granted_paths(tmp_path, monkeypatch):
    """Every path reaches the profile as a -D parameter, never as text."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: True)
    home = tmp_path / 'ho"me'
    venv = home / "work" / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(home)])
    argv = os_sandbox.wrap(["true"], clone=tmp_path / "c", run_dir=tmp_path / "r",
                           read_only=[str(venv), str(home), "/"], loopback=True)
    profile = argv[2]
    params = dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "-D")
    assert str(home) not in profile and str(venv) not in profile
    real = os.path.realpath

    def named(prefix, path):
        [name] = [n for n, v in params.items() if n.startswith(prefix) and v == real(path)]
        return name

    assert f'(deny file-read* (subpath (param "{named("HOME", home)}")))' in profile
    # tmp_path is under /private, a firmlink: the home's Data-volume alias exists.
    alias = os_sandbox._DATA_VOLUME + real(home)
    assert os.path.isdir(alias)
    [alias_name] = [n for n, v in params.items() if n.startswith("ALIAS") and v == alias]
    assert f'(deny file-read* (subpath (param "{alias_name}")))' in profile
    assert profile.index(alias_name) < profile.index("(allow file-read*")
    assert f'(allow file-read* (subpath (param "{named("READ", venv)}")))' in profile
    for directory in (home, home / "work"):
        assert f'(allow file-read-metadata (literal (param "{named("META", directory)}")))' \
            in profile
    assert profile.count("(allow file-read* ") == 1, "the home itself was granted"
    assert profile.index("(deny file-read*") < profile.index("(allow file-read*")
    assert '(remote ip "localhost:*")' in profile


def test_wrap_on_linux_binds_only_what_the_run_needs(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: True)
    home = tmp_path / "home"
    versions = home / ".pyenv" / "versions"
    versions.mkdir(parents=True)
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(home)])
    clone, run = tmp_path / "c", tmp_path / "r"
    argv = os_sandbox.wrap(["/bin/sh"], clone=clone, run_dir=run,
                           read_only=[str(versions), str(home / ".pyenv"), str(home), "/",
                                      "relative", ""])
    clone, run = os.path.realpath(clone), os.path.realpath(run)
    assert argv[0] == "/usr/bin/bwrap" and argv[-2:] == ["--", "/bin/sh"]
    pairs = list(itertools.pairwise(argv))
    assert ("--ro-bind", "/") not in pairs, "the whole root is visible"
    assert ("--tmpfs", "/tmp") in pairs
    assert ("--ro-bind-try", os.path.realpath(versions)) in pairs
    assert ("--ro-bind-try", os.path.realpath(home / ".pyenv")) not in pairs
    assert ("--ro-bind-try", os.path.realpath(home)) not in pairs, "the home was granted"
    assert ("--ro-bind-try", "/") not in pairs
    for flag in ("--unshare-net", "--unshare-pid", "--unshare-ipc", "--die-with-parent"):
        assert flag in argv
    assert ("--bind", clone) in pairs and ("--bind", run) in pairs


@pytest.mark.parametrize("tree", [".ssh", ".aws", ".gnupg", ".config", ".config/gh",
                                  ".config/mise", ".local/share/keyrings", "Library",
                                  "Library/Python/3.12", ".netrc", ".docker", ".kube",
                                  ".kube/cache"])
def test_no_credential_tree_is_granted(tmp_path, monkeypatch, tree):
    """Nothing in or under a credential tree of a home is granted, on either
    platform: _granted drops it before wrap() builds the profile or binds."""
    home = tmp_path / "home"
    (home / tree).mkdir(parents=True)
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(home)])
    assert os_sandbox._granted([str(home / tree)]) == []


@pytest.mark.parametrize("holder", [".local", ".local/share", ".config/..", "."])
def test_no_path_holding_a_credential_tree_is_granted(tmp_path, monkeypatch, holder):
    home = tmp_path / "home"
    (home / ".local/share/keyrings").mkdir(parents=True)
    (home / ".config").mkdir()
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(home)])
    assert os_sandbox._granted([str(home / holder)]) == []


GRANTED_UNDER_A_HOME = (".pyenv/versions", ".pyenv/versions/3.12.1/lib", ".nvm/versions/node",
                        ".asdf/installs/python", ".volta/tools/image", ".rustup/toolchains/x",
                        ".cargo/bin", ".rbenv/versions/3.3", ".sdkman/candidates/java",
                        ".local/share/uv/python/cpython-3.12", ".local/share/mise/installs/node",
                        "work/proj/.venv", "work/proj/.venv/bin", "work/proj/node_modules",
                        "work/proj/node_modules/.bin", "miniconda3", "miniconda3/bin",
                        "anaconda3/lib", "miniforge3/envs/py", "mambaforge/bin")
REFUSED_UNDER_A_HOME = (".pyenv", ".pyenv/shims", ".cargo", ".cargo/registry", ".nvm",
                        ".local/share/uv", ".local/bin", ".bun/bin", "bin", "work", "work/proj",
                        "work/proj/src", ".configs")


def made_home(tmp_path, monkeypatch):
    """A fake home holding every path of both lists above (work/proj/.venv a
    venv: it holds pyvenv.cfg) and a keyring; homes() is it alone."""
    home = tmp_path / "home"
    for path in (*GRANTED_UNDER_A_HOME, *REFUSED_UNDER_A_HOME, ".local/share/keyrings"):
        (home / path).mkdir(parents=True, exist_ok=True)
    (home / "work/proj/.venv/pyvenv.cfg").write_text("")
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os_sandbox._canonical(str(home))])
    return home


@pytest.mark.parametrize("path", GRANTED_UNDER_A_HOME)
def test_under_a_home_only_a_toolchain_venv_or_node_modules_is_granted(tmp_path, monkeypatch,
                                                                       path):
    home = made_home(tmp_path, monkeypatch)
    assert os_sandbox._granted([str(home / path)]) == [os.path.realpath(home / path)]


@pytest.mark.parametrize("path", REFUSED_UNDER_A_HOME)
def test_nothing_else_under_a_home_is_granted(tmp_path, monkeypatch, path):
    """A version manager's whole root (~/.cargo holds credentials.toml), a
    shim directory, a PATH directory of the user's, a checkout outside its
    venv: none is in the allowlist."""
    home = made_home(tmp_path, monkeypatch)
    assert os_sandbox._granted([str(home / path)]) == []


case_insensitive = pytest.mark.skipif(
    not os.path.exists(os.path.realpath(tempfile.gettempdir()).upper()),
    reason="the case-variant tests need a case-insensitive file system (macOS's default)")


@case_insensitive
@pytest.mark.parametrize("variant", ["{home}/.SSH", "{home}/.CONFIG/gh", "{HOME}/.ssh",
                                     "{HOME}/work/proj", "{HOME}", "{data}/.ssh", "{data}"])
def test_a_case_or_alias_variant_of_a_refused_path_is_refused(tmp_path, monkeypatch, variant):
    """On a case-insensitive volume `~/.SSH` is ~/.ssh, and the Data
    volume's firmlink is another spelling of the home: compared by identity
    (device, inode), each is refused as its plain spelling is."""
    home = made_home(tmp_path, monkeypatch)
    (home / ".ssh").mkdir()
    (home / ".config/gh").mkdir(parents=True)
    real = os.path.realpath(home)
    path = variant.format(home=real, HOME=real.upper(), data=os_sandbox._DATA_VOLUME + real)
    assert os.path.exists(path)
    assert os_sandbox._granted([path]) == []


@case_insensitive
def test_a_case_variant_of_a_granted_path_is_granted_as_spelled_on_disk(tmp_path, monkeypatch):
    home = made_home(tmp_path, monkeypatch)
    real = os.path.realpath(home)
    assert os_sandbox._granted([real.upper() + "/.PYENV/VERSIONS"]) == \
        [os.path.join(real, ".pyenv", "versions")]


@pytest.mark.parametrize("where", ["/tmp", "/var/run", "/private/tmp"])
def test_nothing_under_tmp_or_run_is_granted(where):
    """A read-only bind does not stop connect() to a socket in it (Linux),
    and /tmp, /run and /var/run are where host daemons keep theirs."""
    if not os.path.isdir(where):
        pytest.skip(f"{where} does not exist on this machine")
    if where.endswith("tmp"):
        made = tempfile.mkdtemp(dir=where)
        try:
            assert os_sandbox._granted([made]) == []
        finally:
            os.rmdir(made)
    assert os_sandbox._granted([where]) == []


@pytest.mark.parametrize("where", ["/", "/var", "/private", "/private/var", "/dev",
                                   "/dev/null", "/proc", "/proc/self", "/sys",
                                   "/System/Volumes/Data", "/System/Volumes/Data/private"])
def test_nothing_holding_or_in_a_socket_or_kernel_tree_is_granted(monkeypatch, where):
    """/var holds /var/run (a read-only bind of it exposes the host's
    sockets on Linux), and a host /dev or /proc granted would be bound over
    the run's own (/proc/<pid>/root is the host's every file). No home, so
    none holding a home is what refuses it."""
    if not os.path.exists(where):
        pytest.skip(f"{where} does not exist on this machine")
    monkeypatch.setattr(os_sandbox, "homes", lambda: [])
    assert os_sandbox._granted([where]) == []


@pytest.mark.parametrize("trees", ["_SOCKET_TREES", "_KERNEL_TREES"])
@pytest.mark.parametrize("where", ["var", "var/run", "var/run/docker", "var/lib"])
def test_a_tree_is_refused_in_both_directions(tmp_path, monkeypatch, trees, where):
    """A stand-in tree (tmp/var/run): what holds it and what is in it are
    refused, and a sibling (var/lib) is not."""
    for path in ("var/run/docker", "var/lib"):
        (tmp_path / path).mkdir(parents=True)
    monkeypatch.setattr(os_sandbox, trees, (os.path.realpath(tmp_path / "var" / "run"),))
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(tmp_path / "home")])
    expected = [os.path.realpath(tmp_path / where)] if where == "var/lib" else []
    assert os_sandbox._granted([str(tmp_path / where)]) == expected


def linux_wrap(tmp_path, monkeypatch, read_only=()):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: True)
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(tmp_path / "home")])
    return os_sandbox.wrap(["/bin/sh"], clone=tmp_path / "c", run_dir=tmp_path / "r",
                           read_only=read_only)


def test_on_linux_the_runs_own_dev_proc_and_tmp_are_mounted_after_every_grant(
        tmp_path, monkeypatch):
    """A later bwrap mount covers an earlier one: the run's own /dev, /proc
    and /tmp come last, so nothing granted can stand in for them."""
    (tmp_path / "tool").mkdir()
    argv = linux_wrap(tmp_path, monkeypatch, read_only=[str(tmp_path / "tool")])
    grant = argv.index(os.path.realpath(tmp_path / "tool"))
    assert argv[grant - 1] == "--ro-bind-try"
    for mount in ("--dev", "--proc", "--tmpfs"):
        assert argv.index(mount) > grant, f"{mount} is mounted before a grant"


def test_on_linux_a_system_directory_that_cannot_be_read_is_unavailable(tmp_path, monkeypatch):
    """An OSError building the binds (a link that cannot be read) is the
    sandbox being unavailable, never an exception the sweep does not catch."""
    def broken(path):
        raise PermissionError(13, "denied by the test")

    link = tmp_path / "lib"
    link.symlink_to(tmp_path)
    monkeypatch.setattr(os_sandbox, "_LINUX_SYSTEM", (str(link),))
    monkeypatch.setattr(os_sandbox.os, "readlink", broken)
    with pytest.raises(os_sandbox.OsSandboxUnavailable, match="cannot be read"):
        linux_wrap(tmp_path, monkeypatch)


@pytest.mark.parametrize("platform", ["win32", "freebsd14", "cygwin"])
def test_wrap_elsewhere_is_unavailable(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(os_sandbox.OsSandboxUnavailable, match="no OS sandbox"):
        os_sandbox.wrap(["true"], clone=tmp_path, run_dir=tmp_path)


@pytest.mark.parametrize("platform, tool", [("darwin", "sandbox-exec"), ("linux", "bwrap")])
def test_wrap_without_the_tool_is_unavailable(tmp_path, monkeypatch, platform, tool):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: False)
    with pytest.raises(os_sandbox.OsSandboxUnavailable, match=tool):
        os_sandbox.wrap(["true"], clone=tmp_path, run_dir=tmp_path)


@pytest.mark.parametrize("platform, tool", [("darwin", "/usr/bin/sandbox-exec"),
                                            ("linux", "/usr/bin/bwrap")])
def test_the_tool_comes_from_a_system_directory_never_path(tmp_path, monkeypatch, platform,
                                                          tool):
    """A `sandbox-exec` or `bwrap` first on PATH (the sweep's environment)
    is never the one run: only the fixed system locations are looked at."""
    planted = tmp_path / "bin"
    planted.mkdir()
    for name in ("sandbox-exec", "bwrap"):
        (planted / name).write_text("#!/bin/sh\nexec \"$@\"\n")
        (planted / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(tmp_path / "h")])
    looked = []
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: looked.append(path) or True)
    argv = os_sandbox.wrap(["true"], clone=tmp_path, run_dir=tmp_path)
    assert argv[0] == tool
    assert looked and all(not path.startswith(str(planted)) for path in looked)
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: False)
    with pytest.raises(os_sandbox.OsSandboxUnavailable):
        os_sandbox.wrap(["true"], clone=tmp_path, run_dir=tmp_path)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_no_home_is_unavailable_never_a_silent_pass(tmp_path, monkeypatch, platform):
    """With no home, _granted has none to refuse (nor its credential trees):
    refused on Linux as on macOS."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: True)
    monkeypatch.setattr(os_sandbox, "homes", lambda: [])
    with pytest.raises(os_sandbox.OsSandboxUnavailable, match="home"):
        os_sandbox.wrap(["true"], clone=tmp_path, run_dir=tmp_path)


def test_wrap_takes_argv_never_a_string(tmp_path):
    with pytest.raises(ValueError, match="never a command string"):
        os_sandbox.wrap("rm -rf ~", clone=tmp_path, run_dir=tmp_path)


# --- 2. fail closed ---------------------------------------------------------------------

@pytest.mark.parametrize("unavailable", ["no tool", "no platform"])
def test_without_an_os_sandbox_the_gate_refuses_and_runs_nothing(
        tmp_path, monkeypatch, no_project_command, unavailable):
    if unavailable == "no tool":
        monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: False)
    else:
        monkeypatch.setattr(sys, "platform", "sunos5")
    ran = tmp_path / "ran"
    with pytest.raises(GateError, match="the test command was not run: no OS sandbox"):
        gate(tmp_path, f"touch {ran}")
    assert no_project_command == [] and not ran.exists()


def test_without_an_os_sandbox_the_sweep_refuses_test_gate(tmp_path, no_real_gh, monkeypatch,
                                                           no_project_command):
    monkeypatch.setattr(os_sandbox, "_is_executable", lambda path: False)
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.TEST_GATE.value
    assert "the test command was not run: no OS sandbox" in line["message"]
    assert no_project_command == []
    assert nothing_pushed(tmp_path, no_real_gh)


# --- 3. the self-test -------------------------------------------------------------------

def unconfined_wrap(argv, **_kwargs):
    """A wrap() that confines nothing: what self_test must catch."""
    return [*PREFIX, *argv]


def test_a_sandbox_that_does_not_confine_fails_its_self_test(tmp_path, monkeypatch):
    monkeypatch.setattr(os_sandbox, "wrap", unconfined_wrap)
    (tmp_path / "c").mkdir()
    (tmp_path / "r").mkdir()
    with pytest.raises(os_sandbox.OsSandboxUnavailable,
                       match="a write outside the clone and the run's directory was not denied"):
        os_sandbox.self_test(tmp_path / "c", tmp_path / "r")
    assert list((tmp_path / "c").iterdir()) == []


def recording_wrap(seen):
    def wrap(argv, **_kwargs):
        seen.append(list(argv))
        return [*PREFIX, *argv]
    return wrap


def probe_dirs():
    """The self-test's outside directories now in the account's home."""
    home = os_sandbox.homes()[0]
    return {name for name in os.listdir(home) if name.startswith(".curamoir-osbx-")}


def test_the_outside_probe_is_in_the_home_where_nothing_masks_it(tmp_path, monkeypatch):
    """Not under /tmp, which bwrap replaces with an empty tmpfs (a write or
    connect there fails for want of the path, proving nothing): in the
    account's home, which both sandboxes really deny. Removed after."""
    seen = []
    monkeypatch.setattr(os_sandbox, "wrap", recording_wrap(seen))
    (tmp_path / "c").mkdir()
    (tmp_path / "r").mkdir()
    before = probe_dirs()
    with pytest.raises(os_sandbox.OsSandboxUnavailable):
        os_sandbox.self_test(tmp_path / "c", tmp_path / "r")
    [argv] = seen
    home = os_sandbox.homes()[0]
    in_outside, listening = argv[5], argv[7]
    for path in (in_outside, listening):
        assert os.path.dirname(os.path.dirname(path)) == home, path
        assert not path.startswith(("/tmp", "/private/tmp"))
    assert probe_dirs() == before


@pytest.mark.parametrize("failing", ["mkdtemp", "_home_target"])
def test_a_self_test_that_cannot_be_set_up_is_unavailable_and_leaves_nothing(
        tmp_path, monkeypatch, failing):
    def broken(*_args, **_kwargs):
        raise PermissionError(13, "denied by the test")

    if failing == "mkdtemp":
        monkeypatch.setattr(os_sandbox.tempfile, "mkdtemp", broken)
    else:
        monkeypatch.setattr(os_sandbox, "_home_target", broken)
    monkeypatch.setattr(os_sandbox, "wrap", unconfined_wrap)
    (tmp_path / "c").mkdir()
    (tmp_path / "r").mkdir()
    before = probe_dirs()
    with pytest.raises(os_sandbox.OsSandboxUnavailable, match="could not be set up"):
        os_sandbox.self_test(tmp_path / "c", tmp_path / "r")
    assert probe_dirs() == before
    assert list((tmp_path / "c").iterdir()) == [] and list((tmp_path / "r").iterdir()) == []


def test_a_canary_that_cannot_be_written_is_not_left_in_the_home(tmp_path, monkeypatch):
    """The probe's file in the home is removed when writing it fails, and
    the home's listing is asked instead."""
    home = tmp_path / "account"  # not tmp_path/home: isolated_home's HOME
    home.mkdir()
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(home)])
    real_mkstemp = os_sandbox.tempfile.mkstemp

    def read_only_mkstemp(**kwargs):
        handle, path = real_mkstemp(**kwargs)
        os.close(handle)
        return os.open(path, os.O_RDONLY), path

    monkeypatch.setattr(os_sandbox.tempfile, "mkstemp", read_only_mkstemp)
    assert os_sandbox._home_target() == (os.path.realpath(home), None)
    assert list(home.iterdir()) == []


def a_python(tmp_path, monkeypatch, prefix):
    """sys.executable and sys.base_prefix a Python installed at `prefix`
    under a fake home (homes() is it alone); the fake home."""
    home = tmp_path / "account"  # not tmp_path/home: isolated_home's HOME
    (home / prefix / "bin").mkdir(parents=True)
    (home / prefix / "bin" / "python3").write_text("")
    monkeypatch.setattr(os_sandbox, "homes", lambda: [os.path.realpath(home)])
    monkeypatch.setattr(sys, "executable", str(home / prefix / "bin" / "python3"))
    monkeypatch.setattr(sys, "base_prefix", str(home / prefix))
    (tmp_path / "c").mkdir()
    (tmp_path / "r").mkdir()
    return home


def test_an_interpreter_the_sandbox_will_not_grant_is_said_plainly(tmp_path, monkeypatch):
    """Not "the net check did not run": the sweep's own Python is refused
    (here a directory of the home's that is not a toolchain's), so the
    probe cannot run, and nothing is made or run."""
    seen = []
    monkeypatch.setattr(os_sandbox, "wrap", recording_wrap(seen))
    home = a_python(tmp_path, monkeypatch, "tools/py")
    with pytest.raises(os_sandbox.OsSandboxUnavailable,
                       match=r"the sweep's Python .* cannot be granted to the sandbox"):
        os_sandbox.self_test(tmp_path / "c", tmp_path / "r")
    assert seen == []
    assert sorted(p.name for p in home.iterdir()) == ["tools"]


@pytest.mark.parametrize("root", ["miniconda3", "anaconda3", "miniforge3", "mambaforge"])
def test_a_conda_interpreter_is_granted_to_the_probe(tmp_path, monkeypatch, root):
    """conda's base Python has no pyvenv.cfg: its install root is in the
    allowlist, so the self-test runs its probe with it."""
    seen = []
    monkeypatch.setattr(os_sandbox, "wrap", recording_wrap(seen))
    a_python(tmp_path, monkeypatch, root)
    with pytest.raises(os_sandbox.OsSandboxUnavailable, match="self-test failed"):
        os_sandbox.self_test(tmp_path / "c", tmp_path / "r")
    assert len(seen) == 1


GOOD = {"clone": "ok", "outside": "denied", "net": "blocked", "unix_outside": "blocked",
        "unix_inside": "ok", "dns": "failed", "home_read": "denied", "home_link": "denied",
        "home_alias": "denied", "launchctl": "denied", "launchservices": "denied",
        "loopback": "denied"}


@pytest.mark.parametrize("change, why", [
    (None, "printed nothing"),
    ({"clone": "denied"}, "inside the clone did not succeed"),
    ({"outside": "ok"}, "outside the clone and the run's directory was not denied"),
    ({"net": "connected"}, "1.1.1.1:53 was not denied"),
    ({"unix_outside": "connected"}, "Unix socket outside"),
    ({"unix_inside": "denied"}, "inside the run's directory did not work"),
    ({"dns": "resolved"}, "a host name was resolved"),
    ({"home_read": "read"}, "under the user's home directory was readable"),
    ({"home_link": "linked"}, "a hard link to a file under the user's home"),
    ({"home_alias": "read"}, "/System/Volumes/Data"),
    ({"home_link": None}, "the home_link check did not run"),
    ({"launchctl": "ran"}, "/bin/launchctl ran"),
    ({"launchservices": "reached"}, "LaunchServices"),
    ({"loopback": "ok"}, "loopback was reachable though sweep.test_loopback is false"),
    ({"dns": None}, "the dns check did not run"),
])
def test_each_failed_check_is_named(change, why):
    said = {} if change is None else {k: v for k, v in {**GOOD, **change}.items()
                                      if v is not None}
    assert os_sandbox._failed_check(GOOD, platform="darwin", link=True, alias=True) == ""
    assert why in os_sandbox._failed_check(said, platform="darwin", link=True, alias=True)


def test_the_link_and_alias_checks_are_asked_only_when_the_probe_can_make_them():
    """No file of the home's to link (an unwritable home: the probe lists the
    directory instead), no alias (not macOS, or no Data volume): not asked."""
    said = {k: v for k, v in GOOD.items() if k not in ("home_link", "home_alias")}
    assert os_sandbox._failed_check(said, platform="darwin") == ""
    assert "home_link" in os_sandbox._failed_check(said, platform="darwin", link=True)
    assert "home_alias" in os_sandbox._failed_check(said, platform="darwin", alias=True)


def test_with_loopback_allowed_the_self_test_wants_it_working():
    assert "did not work" in os_sandbox._failed_check(GOOD, loopback=True, platform="darwin")
    assert os_sandbox._failed_check({**GOOD, "loopback": "ok"}, loopback=True,
                                    platform="darwin") == ""


def test_linux_asks_neither_launchd_nor_loopback():
    linux = {k: v for k, v in GOOD.items()
             if k not in ("launchctl", "launchservices", "loopback")}
    assert os_sandbox._failed_check(linux, platform="linux") == ""


def test_a_failed_self_test_refuses_the_gate_and_runs_nothing(tmp_path, monkeypatch,
                                                               no_project_command):
    monkeypatch.setattr(os_sandbox, "wrap", unconfined_wrap)
    ran = tmp_path / "ran"
    with pytest.raises(GateError, match="no OS sandbox confines it here: the sandbox "
                                        "self-test failed"):
        gate(tmp_path, f"touch {ran}")
    assert no_project_command == [] and not ran.exists()


def test_a_failed_self_test_is_a_test_gate_refusal(tmp_path, no_real_gh, monkeypatch,
                                                   no_project_command):
    monkeypatch.setattr(os_sandbox, "wrap", unconfined_wrap)
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.TEST_GATE.value
    assert "self-test failed" in line["message"]
    assert no_project_command == []
    assert nothing_pushed(tmp_path, no_real_gh)


def test_every_run_goes_through_wrap(tmp_path, monkeypatch, no_project_command):
    """The canary and each test run, base and bisection included, are
    wrapped; the self-test runs once per gate, first."""
    stand_in = Unconfined()
    monkeypatch.setattr(green_gate, "OS_SANDBOX", stand_in)
    repo = repo_of(tmp_path, THREE, "! grep -rqs BROKEN src")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n",
                                                      "src/b.py": "BROKEN\n"}))
    assert result.strategy == "reduced"
    assert len(stand_in.self_tests) == 1
    assert len(no_project_command) == len(result.runs) + 1  # + the canary
    assert all(argv[:len(PREFIX)] == PREFIX for argv in no_project_command)
    run_dirs = [run_dir for _, _, run_dir in stand_in.wrapped]
    assert len(set(run_dirs)) == len(run_dirs), "a run reused another's directory"


@pytest.mark.skipif(sys.platform != "darwin", reason="the profile's home rules are macOS's; "
                                                    f"this is {sys.platform}")
@real_sandbox
def test_a_profile_that_leaves_the_home_readable_fails_its_self_test(tmp_path, monkeypatch):
    monkeypatch.setattr(os_sandbox, "_darwin_rules", lambda *_args: ("\n", []))
    (tmp_path / "c").mkdir()
    (tmp_path / "r").mkdir()
    with pytest.raises(os_sandbox.OsSandboxUnavailable, match="under the user's home"):
        os_sandbox.self_test(tmp_path / "c", tmp_path / "r")
