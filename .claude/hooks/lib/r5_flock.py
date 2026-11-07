#!/usr/bin/env python3
"""Take an exclusive flock(2) on an inherited file descriptor (cm-99l, cm-70j).

    r5_flock.py <fd> [<timeout-seconds>]

lib/flock.sh opens the lock file on its own fd and runs this, which locks
that fd and exits. flock(2) locks belong to the open file description, so
the lock outlives this process: the calling shell still holds the fd, and
the kernel drops the lock when the shell (and any child it gave the fd to)
exits. macOS ships no flock(1), which is why the shell needs a helper.

Exit 0 with the lock taken; 4 if it was not taken within the timeout
(default 10s); 3 if the fd cannot be locked at all; 1 on a usage error.
Standard library only, Python 3.9 compatible.
"""

from __future__ import annotations

import fcntl
import signal
import sys


def _timed_out(signum, frame):
    sys.exit(4)


def main(argv: list) -> int:
    if len(argv) not in (1, 2):
        print("usage: r5_flock.py <fd> [<timeout-seconds>]", file=sys.stderr)
        return 1
    try:
        fd = int(argv[0])
        timeout = int(argv[1]) if len(argv) == 2 else 10
    except ValueError:
        print("r5_flock: <fd> and <timeout-seconds> must be integers", file=sys.stderr)
        return 1
    signal.signal(signal.SIGALRM, _timed_out)
    signal.alarm(timeout)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        print(f"r5_flock: cannot lock fd {fd}: {exc}", file=sys.stderr)
        return 3
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
