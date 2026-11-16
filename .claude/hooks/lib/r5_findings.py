#!/usr/bin/env python3
"""The bridge's entry to the shared R5 Finding contract (cm-70j).

The parser itself is ``src/curamoir/gate/findings.py`` (lifted from this file
by cm-8ap.13); this file puts that ``src/`` on the path, takes the module's
names as its own, and runs its command line. r5-state.sh calls it for
`findings` and `advisory`:

    r5_findings.py parse <lens> <report-file> [--floor <level>] [--prior <file>]
                         [--blocking <lens,lens,...>]

r5-state.sh passes ``--blocking`` with the run's own blocking passes, so a
run keeps the roster it started with: since cm-ig5 a new run blocks on
correctness, edge cases and provenance, and clarity is advisory (never
``unproven``); a run started with clarity among its blocking passes still
labels clarity's findings. Without the option the module's default applies.

Exit codes are the module's: 0; 2 if the report does not meet the contract
(the caller records the reviewer UNAVAILABLE); 1 on a usage error, including
a checkout with no ``src/curamoir/gate/findings.py`` beside it.

Standard library only, and Python 3.9 compatible, like the module: the hooks
run under whatever python3 the session finds (macOS ships 3.9).
"""

from __future__ import annotations

import sys
from pathlib import Path

# .claude/hooks/lib/r5_findings.py -> the repository root, then src/. Ahead of
# anything else on the path, so an installed copy of curamoir never stands in
# for the checkout's own.
_SRC = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(_SRC))
try:
    from curamoir.gate import findings as _impl
except ImportError as exc:
    print(f"r5_findings: cannot import curamoir.gate.findings from {_SRC}: {exc}", file=sys.stderr)
    sys.exit(1)

# The module's names, so a caller can use this file as the module itself.
# They are the same objects: patch _impl, not this module, to change
# behaviour (fingerprint looks normalise_claim up in _impl).
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

if __name__ == "__main__":
    sys.exit(_impl.main(sys.argv[1:]))
