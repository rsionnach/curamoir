"""Read a repository's configuration: tolerantly, or strictly.

`effective_config` is the tolerant reader and `strict_config` the strict
one; `CONFIG_PATH` is where both look.

Every consumer that wants a configured value goes through here rather than
calling `load_config_text` and handling the failure modes itself. Three
reasons. The first two are why the tolerant reader falls back;
`strict_config` is for readers whose fallback would loosen a guard (its
docstring names them). The third is for both:

- A hook must never die because the config is malformed. Falling back to the
  documented defaults keeps the handoff being written while the developer fixes
  their YAML, and `/curamoir:doctor` is where a bad config gets reported.
- PyYAML may be absent for an installed plugin (an unpacked directory has no pip
  step), and the hooks are deliberately stdlib-only.
- It gives one place to look when asking "does anything actually read this key?",
  which is the question that caught four inert keys in spec-compliance review.
"""

import os
import stat
from copy import deepcopy
from pathlib import Path

from curamoir.config import DEFAULTS, ConfigError, load_config_text

CONFIG_PATH = (".curamoir", "curamoir.yaml")


def effective_config(repo):
    """The repo's config merged over the defaults, or the defaults alone.

    Never raises: a missing file, unreadable file, malformed YAML or absent
    PyYAML all resolve to the documented defaults.
    """
    path = Path(repo).joinpath(*CONFIG_PATH)
    try:
        return load_config_text(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(DEFAULTS)


def strict_config(repo):
    """The strict half of the pair: `repo`'s config merged over the
    defaults, or the defaults when there is no config file. ConfigError
    when there is one that cannot be loaded, every problem naming the path.

    For a reader whose fallback would loosen a guard (job_guard's budget and
    open-PR cap, lint's threshold): a broken file there is a refusal, not
    the defaults.
    """
    path = Path(repo).joinpath(*CONFIG_PATH)
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        # NotADirectoryError: `.curamoir` is a file, not a directory, so
        # there is no config inside it to have been meant.
        return deepcopy(DEFAULTS)
    except OSError as unreachable:
        # A .curamoir/ that cannot be searched: the file may well be there,
        # and treating it as absent would put the defaults in its place.
        raise ConfigError([f"{path} cannot be read: {unreachable}"]) from None
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        # A dangling symlink, or one into a directory that cannot be
        # searched (pathlib's is_file re-raises EACCES on 3.12, so it is
        # not used here).
        regular = False
    if not regular:
        # A directory, a dangling symlink, a FIFO: something is there, and
        # whatever limits it was meant to set cannot be read. Checked before
        # the read because opening a FIFO blocks until something writes to
        # it, which for an unattended job is never.
        raise ConfigError([f"{path} is not a regular file, so it cannot be read"])
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as unreadable:
        # Permissions, bytes that are not UTF-8.
        raise ConfigError([f"{path} cannot be read: {unreadable}"]) from None
    try:
        return load_config_text(text)
    except ConfigError as invalid:
        raise ConfigError([f"{path}: invalid: " + "; ".join(invalid.problems)]) from None
    except Exception as error:  # a YAML syntax error is PyYAML's own type
        # Caught here so every failure reaches the caller as a ConfigError:
        # job_guard refuses it after the kill switch and the lock like any
        # other invalid config, rather than letting it escape run_guarded
        # as a failure with no reason.
        raise ConfigError([f"{path}: not valid YAML: {error}"]) from None


def config_value(repo, *keys, default=None):
    """One nested value, e.g. config_value(repo, "handoff", "keep_history")."""
    node = effective_config(repo)
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
