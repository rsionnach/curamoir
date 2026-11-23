"""The repository root, resolved in one place.

Test modules import it rather than climbing from their own ``__file__``:
the `no-layout-relative-path` guard rule (examples/python/) flags that shape
in a test file, and the R5 gate's round 0 runs that rule over every test
file a change touches.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
