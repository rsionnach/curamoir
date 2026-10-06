"""Read a repository's effective configuration, tolerantly.

Every consumer that wants a configured value goes through here rather than
calling `load_config_text` and handling the failure modes itself. Three reasons:

- A hook must never die because the config is malformed. Falling back to the
  documented defaults keeps the handoff being written while the developer fixes
  their YAML, and `/curamoir:doctor` is where a bad config gets reported.
- PyYAML may be absent for an installed plugin (an unpacked directory has no pip
  step), and the hooks are deliberately stdlib-only.
- It gives one place to look when asking "does anything actually read this key?",
  which is the question that caught four inert keys in spec-compliance review.
"""

from copy import deepcopy
from pathlib import Path

from curamoir.config import DEFAULTS, load_config_text


def effective_config(repo):
    """The repo's config merged over the defaults, or the defaults alone.

    Never raises: a missing file, unreadable file, malformed YAML or absent
    PyYAML all resolve to the documented defaults.
    """
    path = Path(repo) / ".curamoir" / "curamoir.yaml"
    try:
        return load_config_text(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(DEFAULTS)


def config_value(repo, *keys, default=None):
    """One nested value, e.g. config_value(repo, "handoff", "keep_history")."""
    node = effective_config(repo)
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
