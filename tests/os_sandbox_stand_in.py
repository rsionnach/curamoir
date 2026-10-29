"""A stand-in for curamoir.os_sandbox, for tests whose fake test commands
must write outside the clone (a run log in tmp_path) and so cannot run
under the real OS sandbox, which denies exactly that.

Install it with `monkeypatch.setattr(green_gate, "OS_SANDBOX", Unconfined())`.
It confines NOTHING: its wrap() prefixes argv with a recognisable
pass-through (`/bin/sh -c 'exec "$@"' curamoir-stand-in ...`), so a spy can
still check that every run went through wrap(), and its self_test() only
records the call. The real sandbox is proved in tests/test_os_sandbox.py.
"""

#: What Unconfined.wrap puts before every argv.
PREFIX = ["/bin/sh", "-c", 'exec "$@"', "curamoir-stand-in"]


class Unconfined:
    """os_sandbox's interface, confining nothing; records its calls."""

    def __init__(self):
        self.wrapped = []
        self.self_tests = []

    def wrap(self, argv, *, clone, run_dir, **_options):
        self.wrapped.append((list(argv), str(clone), str(run_dir)))
        return [*PREFIX, *argv]

    def self_test(self, clone, run_dir, env=None, **_options):
        self.self_tests.append((str(clone), str(run_dir)))
