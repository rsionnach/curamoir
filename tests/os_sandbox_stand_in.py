"""A stand-in for curamoir.os_sandbox, for tests whose fake test commands
must write outside the clone (a run log in tmp_path) and so cannot run
under the real OS sandbox, which denies exactly that.

Install it with `monkeypatch.setattr(green_gate, "OS_SANDBOX", Unconfined())`.
It confines NOTHING: its wrap() prefixes argv with a recognisable
pass-through (`/bin/sh -c 'exec "$@"' curamoir-stand-in ...`), so a spy can
still check that every run went through wrap(), and its self_test() only
records the call. The real sandbox is proved in tests/test_os_sandbox.py.
Absent is the other stand-in: no OS sandbox at all (cm-2qp.2).
"""

from curamoir.os_sandbox import OsSandboxUnavailable

#: What Unconfined.wrap puts before every argv.
PREFIX = ["/bin/sh", "-c", 'exec "$@"', "curamoir-stand-in"]


class Unconfined:
    """os_sandbox's interface, confining nothing; records its calls."""

    def __init__(self):
        self.wrapped = []
        self.self_tests = []

    # The real signatures (os_sandbox.wrap, os_sandbox.self_test), keyword
    # for keyword, so a misspelt keyword fails here as it would there.
    def wrap(self, argv, *, clone, run_dir, read_only=(), loopback=False, links=()):
        self.wrapped.append((list(argv), str(clone), str(run_dir)))
        return [*PREFIX, *argv]

    def self_test(self, clone, run_dir, env=None, read_only=(), loopback=False, links=()):
        self.self_tests.append((str(clone), str(run_dir)))


class Absent:
    """os_sandbox's interface where there is no OS sandbox (cm-2qp.2): wrap()
    and self_test() record the call and raise OsSandboxUnavailable, as the
    real ones do without sandbox-exec or bwrap. On the local runner the gate
    must then refuse (no unsandboxed fallback); on the hosted runner it must
    never call either (the VM is the boundary), so its lists stay empty."""

    def __init__(self):
        self.wrapped = []
        self.self_tests = []

    def wrap(self, argv, *, clone, run_dir, read_only=(), loopback=False, links=()):
        self.wrapped.append((list(argv), str(clone), str(run_dir)))
        raise OsSandboxUnavailable("no OS sandbox here (tests/os_sandbox_stand_in.Absent)")

    def self_test(self, clone, run_dir, env=None, read_only=(), loopback=False, links=()):
        self.self_tests.append((str(clone), str(run_dir)))
        raise OsSandboxUnavailable("no OS sandbox here (tests/os_sandbox_stand_in.Absent)")
