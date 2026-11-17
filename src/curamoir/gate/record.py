"""R5-R48 and R5-R49: the gate's round record and the tree-keyed verdict.

The record is the §11 round record, written to
``.curamoir/state/r5/<bead>/record.json``: the bead, the repository, the
branch and its ``base``/``head``, one entry in ``rounds`` per round, and at
the end a ``verdict`` keyed to the tree it reviewed.

**Crash safety.** The file is rewritten whole after each round: into a
temporary file beside it, fsynced, then renamed over it. A reader therefore
sees the record as it stood after some completed round, never a partial one,
and a crash mid-round costs that round alone (R5-R48).

**What reaches the record.** A round's lens entries keep the keys §11 gives
them and no others, and each finding in them keeps the Finding contract's
keys; a verdict's ``inputs`` and ``input_digest`` come only from a
:class:`curamoir.gate.digest.VerdictInputs`, which only the digest module
builds. So nothing a reviewer writes (a stray ``input_digest`` key, a digest
quoted in its output) becomes the record's digest (R5-R75).

**The verdict check** (§11, R5-R49). A close, push or merge passes when the
verdict's state is ``CLEAN`` and ``HEAD^{tree}`` equals ``verdict.tree_sha``;
anything else refuses, naming the state or the mismatch. A missing or
unreadable record, or one with no verdict, refuses too. The gate guard's
three surfaces call :func:`check_verdict`; wiring them is a separate bead.

The ledger's one job line per gate is written by ``job_guard``, as for every
job, once the runner registers the gate job; this module does not write it.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from curamoir.gate.digest import DIGEST_FORM, VerdictInputs, input_digest_of
from curamoir.git_guard import GitGuardError, git

#: Where a bead's record lives, under the repository's root.
RECORD_DIR = (".curamoir", "state", "r5")
RECORD_NAME = "record.json"

KINDS = ("preflight", "review", "fix", "verify")
STATES = ("CLEAN", "ESCALATED", "PREFLIGHT_FAILED", "BUDGET")

#: The keys of one lens entry in a round (§11).
LENS_KEYS = ("model", "tier", "mode", "tokens_in", "tokens_out", "duration_s", "status",
             "skip_rule", "findings", "closures", "closure_rejected", "unaccounted",
             "downgrades_refused", "prose_unfiled")
#: The list in a lens entry that holds finding objects (``closures`` and
#: ``unaccounted`` hold fingerprints, as gate/findings.py returns them).
_FINDING_LISTS = ("findings",)
#: The keys of one finding (§11, the Finding contract).
FINDING_KEYS = ("lens", "severity", "path", "start_line", "end_line", "claim", "evidence",
                "fix", "fingerprint", "verification_level", "resolves", "regression", "status")
#: The keys of a round other than its lenses (§11).
ROUND_KEYS = ("n", "kind", "head", "started", "ended", "lenses", "merge", "fix", "mutations")
_ROUND_REQUIRED = ("n", "kind", "head", "started", "ended", "lenses")

_BEAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class RecordError(ValueError):
    """A record that cannot be read, or a write the contract does not allow."""


def record_path(repo, bead: str) -> Path:
    """``<repo>/.curamoir/state/r5/<bead>/record.json``. ``bead`` is one path
    component: letters, digits, ``.``, ``_`` and ``-``, and not ``.`` or
    ``..``."""
    if not (isinstance(bead, str) and _BEAD.fullmatch(bead) and bead not in (".", "..")):
        raise RecordError(f"bead id {bead!r} cannot name a record directory")
    return Path(repo).joinpath(*RECORD_DIR, bead, RECORD_NAME)


def load(repo, bead: str) -> dict:
    """The bead's record as it stood after its last completed write."""
    path = record_path(repo, bead)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise RecordError(f"no gate record for {bead} at {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise RecordError(f"the gate record for {bead} cannot be read: {error}") from error
    try:
        record = json.loads(text)
    except ValueError as error:
        raise RecordError(f"the gate record for {bead} is not JSON: {error}") from error
    if not isinstance(record, dict) or not isinstance(record.get("rounds"), list):
        raise RecordError(f"the gate record for {bead} is not a round record")
    return record


def start(repo, bead: str, *, repo_name: str, branch: str, base: str, head: str,
          replace: bool = False) -> dict:
    """Write a new record with no rounds. An existing record is kept, and
    this refuses, unless ``replace`` is true: an earlier gate's record is
    never lost by accident."""
    path = record_path(repo, bead)
    if path.exists() and not replace:
        raise RecordError(f"a gate record for {bead} already exists at {path}")
    for name, value in (("repo_name", repo_name), ("branch", branch)):
        if not (isinstance(value, str) and value.strip()):
            raise RecordError(f"{name} must be non-empty text")
    for name, value in (("base", base), ("head", head)):
        _object_id(name, value)
    record = {"bead": bead, "repo": repo_name, "branch": branch, "base": base, "head": head,
              "rounds": []}
    _write(path, record)
    return record


def append_round(repo, bead: str, round_: dict) -> dict:
    """Add one completed round and write the record. The round's ``n`` is
    the next number; its lens entries and their findings keep only the keys
    §11 gives them. No round is added after the verdict."""
    record = load(repo, bead)
    if "verdict" in record:
        raise RecordError(f"the gate record for {bead} already has its verdict")
    record["rounds"].append(_round(round_, expected_n=len(record["rounds"]) + 1))
    _write(record_path(repo, bead), record)
    return record


def set_verdict(repo, bead: str, state: str, *, tree_sha: str, head_sha: str, at: str,
                inputs: VerdictInputs | None = None) -> dict:
    """Write the verdict. ``inputs``, when given, is the digest module's
    :class:`VerdictInputs` for this same ``tree_sha``; nothing else may
    supply a verdict's inputs or ``input_digest``."""
    record = load(repo, bead)
    if "verdict" in record:
        raise RecordError(f"the gate record for {bead} already has its verdict")
    if state not in STATES:
        raise RecordError(f"verdict state {state!r} is not one of {', '.join(STATES)}")
    _object_id("tree_sha", tree_sha)
    _object_id("head_sha", head_sha)
    _text("at", at)
    verdict = {"state": state, "tree_sha": tree_sha, "head_sha": head_sha, "at": at}
    if inputs is not None:
        if not isinstance(inputs, VerdictInputs):
            raise RecordError("a verdict's inputs come from gate.digest.verdict_inputs only")
        if inputs.tree_sha != tree_sha:
            raise RecordError("the verdict inputs were digested for a different tree_sha")
        recorded = dict(inputs.inputs)
        if not all(isinstance(k, str) and isinstance(v, str) and DIGEST_FORM.fullmatch(v)
                   for k, v in recorded.items()):
            raise RecordError("a verdict input is not a git: or sha256: digest")
        # Recomputed, not trusted: the digest written is the one these inputs
        # and this tree give.
        if inputs.input_digest != input_digest_of(tree_sha, recorded):
            raise RecordError("the verdict's input_digest does not match its inputs and tree_sha")
        verdict["inputs"] = recorded
        verdict["input_digest"] = inputs.input_digest
    record["verdict"] = verdict
    _write(record_path(repo, bead), record)
    return record


@dataclass(frozen=True)
class Refusal:
    """Why the verdict check refused: the message names the state or the
    mismatch."""

    reason: str

    def __str__(self) -> str:
        return self.reason


def check_verdict(repo, bead: str) -> Refusal | None:
    """None when the bead's verdict is ``CLEAN`` and ``HEAD^{tree}`` equals
    its ``tree_sha``; otherwise a :class:`Refusal` saying why."""
    try:
        record = load(repo, bead)
    except RecordError as error:
        return Refusal(str(error))
    verdict = record.get("verdict")
    if not isinstance(verdict, dict):
        return Refusal(f"the gate record for {bead} has no verdict yet")
    state = verdict.get("state")
    if state != "CLEAN":
        return Refusal(f"the gate's verdict for {bead} is {state!r}, not CLEAN")
    try:
        tree = git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{tree}",
                   ok=(0, 1, 128)).decode("ascii", "replace").strip()
    except GitGuardError as error:
        return Refusal(f"the current tree cannot be read: {error}")
    if not _OBJECT_ID.fullmatch(tree):
        return Refusal("the current tree cannot be read: HEAD names no commit")
    recorded = verdict.get("tree_sha")
    if tree != recorded:
        return Refusal(f"the tree has changed since the gate ran for {bead}: "
                       f"HEAD^{{tree}} is {tree}, the verdict is for {recorded}")
    return None


# --- validation and writing -----------------------------------------------------

def _object_id(name: str, value) -> None:
    if not (isinstance(value, str) and _OBJECT_ID.fullmatch(value)):
        raise RecordError(f"{name} must be a full hex object id")


def _text(name: str, value) -> None:
    if not (isinstance(value, str) and value.strip()):
        raise RecordError(f"{name} must be non-empty text")


def _round(raw, *, expected_n: int) -> dict:
    if not isinstance(raw, dict):
        raise RecordError("a round must be an object")
    missing = [k for k in _ROUND_REQUIRED if k not in raw]
    if missing:
        raise RecordError(f"round is missing {', '.join(missing)}")
    if type(raw["n"]) is not int or raw["n"] != expected_n:
        raise RecordError(f"round n must be {expected_n}, the next round")
    if raw["kind"] not in KINDS:
        raise RecordError(f"round kind {raw['kind']!r} is not one of {', '.join(KINDS)}")
    _object_id("round head", raw["head"])
    _text("round started", raw["started"])
    _text("round ended", raw["ended"])
    if not isinstance(raw["lenses"], dict):
        raise RecordError("round lenses must map a lens to its entry")
    kept = {k: copy.deepcopy(raw[k]) for k in ROUND_KEYS if k in raw}
    kept["lenses"] = {name: _lens_entry(name, entry) for name, entry in raw["lenses"].items()}
    return kept


def _lens_entry(name, entry) -> dict:
    if not isinstance(name, str) or not isinstance(entry, dict):
        raise RecordError("a lens entry must be an object under its lens name")
    kept = {k: copy.deepcopy(entry[k]) for k in LENS_KEYS if k in entry}
    for key in _FINDING_LISTS:
        if key in kept:
            if not isinstance(kept[key], list) or not all(isinstance(f, dict) for f in kept[key]):
                raise RecordError(f"lens {name}: {key} must be a list of findings")
            kept[key] = [{k: f[k] for k in FINDING_KEYS if k in f} for f in kept[key]]
    return kept


def _write(path: Path, record: dict) -> None:
    """The whole record, atomically: a temporary file beside ``path`` (named
    by pid, so two writers never share one), fsynced, renamed over it."""
    try:
        text = json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False,
                          allow_nan=False) + "\n"
    except (TypeError, ValueError) as error:
        raise RecordError(f"the record cannot be written as JSON: {error}") from error
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with scratch.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, path)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
