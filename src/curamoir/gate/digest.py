"""R5-R75: the verdict's inputs and their digest, computed in one place.

A gate's verdict depends on more than the tree it reviewed: the brief, the
plan and the mutation block (the subject), and the lens templates, the gate
configuration and the runner's version (the apparatus). ``verdict.inputs``
names a digest of each, and ``verdict.input_digest`` digests them together
with ``tree_sha`` (§11, "Verdict inputs"):

- a whole tracked file is ``git:<blob id>``; anything else is
  ``sha256:<hex>`` over its exact bytes. A tracked path is recorded by its
  blob only when the bytes on disk are that blob, so a modified tracked file
  is never recorded as the committed one;
- ``input_digest`` is ``sha256:`` over the canonical JSON (UTF-8, keys
  sorted, no insignificant whitespace) of ``{"inputs": ..., "tree_sha": ...}``.

Only the runner computes a digest, and only from content: nothing here takes
a digest, a name, a size or an mtime as input, so :func:`verdict_inputs` is
given the files and bytes themselves. :class:`VerdictInputs` is the
one form ``gate/record.py`` accepts for a verdict's inputs, and only
:func:`verdict_inputs` builds one, so a digest that appears in a reviewer's
output (a finding's text, a stray key) has no way into the record.

The fields are informational: the verdict check reads ``tree_sha`` and
nothing else, and nothing refuses on them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from curamoir.git_guard import git

#: The input names §11 lists, ``lens:<name>`` aside.
FIXED_NAMES = ("brief", "plan", "mutations", "gate_config", "runner_version")

_LENS_NAME = re.compile(r"lens:[a-z][a-z_-]*")
_TREE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Held only by this module: VerdictInputs refuses construction without it.
_BUILT_HERE = object()


class DigestError(ValueError):
    """An input name, digest or path the contract does not allow."""


def of_bytes(data: bytes) -> str:
    """``sha256:<hex>`` over ``data``, exactly as given."""
    if not isinstance(data, (bytes, bytearray)):
        raise DigestError("a digest is taken over bytes")
    return "sha256:" + hashlib.sha256(bytes(data)).hexdigest()


def of_file(repo, path) -> str:
    """The digest of the file at ``path`` in ``repo``: ``git:<blob id>`` when
    the path is tracked at HEAD and its bytes on disk are that blob, else
    ``sha256:`` over the bytes. ``path`` must be a regular file inside the
    repository (a symlink is not followed out of it)."""
    repo = Path(repo).resolve()
    target = (repo / path)
    try:
        resolved = target.resolve(strict=True)
    except OSError as error:
        raise DigestError(f"{path}: cannot be read ({error.strerror or error})") from error
    if not resolved.is_relative_to(repo):
        raise DigestError(f"{path}: is outside the repository")
    if not resolved.is_file():
        raise DigestError(f"{path}: is not a regular file")
    data = resolved.read_bytes()
    relative = resolved.relative_to(repo).as_posix()
    blob = _tracked_blob(repo, relative)
    if blob is not None and blob == _blob_id(repo, data):
        return "git:" + blob
    return of_bytes(data)


def _tracked_blob(repo: Path, relative: str) -> str | None:
    """The blob id ``relative`` has in HEAD's tree, or None (untracked, not a
    blob, or no HEAD yet)."""
    out = git(repo, "ls-tree", "-z", "--full-tree", "HEAD", "--", relative, ok=(0, 128))
    entries = [e for e in out.split(b"\0") if e]
    if len(entries) != 1:
        return None
    meta, _, name = entries[0].partition(b"\t")
    parts = meta.split()
    if len(parts) != 3 or parts[1] != b"blob" or name.decode("utf-8", "surrogateescape") != relative:
        return None
    return parts[2].decode("ascii")


def _blob_id(repo: Path, data: bytes) -> str:
    """The id git gives a blob holding exactly ``data`` in this repository's
    object format (SHA-1 or SHA-256), computed here rather than by
    ``hash-object``, so no clean filter or line-ending rule changes it."""
    fmt = git(repo, "rev-parse", "--show-object-format").decode("ascii").strip()
    hasher = hashlib.sha256() if fmt == "sha256" else hashlib.sha1()
    hasher.update(b"blob %d\0" % len(data))
    hasher.update(data)
    return hasher.hexdigest()


def canonical(value) -> bytes:
    """The canonical JSON §11 digests: UTF-8, keys sorted, no insignificant
    whitespace, non-ASCII kept as UTF-8 rather than escaped."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class VerdictInputs:
    """A verdict's ``inputs`` and ``input_digest`` for one ``tree_sha``.

    Built only by :func:`verdict_inputs`; ``gate/record.py`` takes a
    verdict's inputs in no other form."""

    tree_sha: str
    inputs: Mapping[str, str]
    input_digest: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._token is not _BUILT_HERE:
            raise TypeError("VerdictInputs is built by gate.digest.verdict_inputs only")


def verdict_inputs(repo, tree_sha: str, *, files: Mapping[str, object] = MappingProxyType({}),
                   contents: Mapping[str, bytes] = MappingProxyType({})) -> VerdictInputs:
    """Digest each input from its content and digest them with ``tree_sha``.

    ``files`` maps an input name to a path in ``repo`` (digested by
    :func:`of_file`); ``contents`` maps a name to the exact bytes of anything
    that is not a file there (digested by :func:`of_bytes`). Names are the
    ones §11 lists, each given once. No digest is accepted from the caller:
    every one is computed here, from content."""
    if not (isinstance(tree_sha, str) and _TREE.fullmatch(tree_sha)):
        raise DigestError("tree_sha must be a full hex object id")
    if not isinstance(files, Mapping) or not isinstance(contents, Mapping):
        raise DigestError("files and contents must each map an input name to its source")
    both = set(files) & set(contents)
    if both:
        raise DigestError(f"input {sorted(both)[0]!r} is given twice")
    digests = {}
    for name in [*files, *contents]:
        if not isinstance(name, str) or not (name in FIXED_NAMES or _LENS_NAME.fullmatch(name)):
            raise DigestError(f"input name {name!r} is not one §11 allows")
    for name, path in files.items():
        digests[name] = of_file(repo, path)
    for name, data in contents.items():
        digests[name] = of_bytes(data)
    ordered = dict(sorted(digests.items()))
    digest = of_bytes(canonical({"inputs": ordered, "tree_sha": tree_sha}))
    return VerdictInputs(tree_sha=tree_sha, inputs=MappingProxyType(ordered),
                         input_digest=digest, _token=_BUILT_HERE)
