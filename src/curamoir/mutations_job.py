"""cm-8ap.19: `curamoir run mutations`, the mutation runner as a job (E5-R23).

    curamoir run mutations --repo <root> --base <sha> --head <sha> --brief <path>

runs the brief's ``r5-mutations`` list (gate/mutation_list.py) through the
runner (gate/mutations.py) with no model, as ``runner.JOBS["mutations"]``.
It is what cúramóir's bridge (.claude/hooks/r5-mutations.py) and NthLayer's
sequential gate call until the gate replaces them.

**Output** (Rob, 2026-11-24). One JSON object per entry on stdout, as each
finishes, each carrying ``index`` and ``total``; the run's sentence and any
notice (an orphan worktree from a dead run) on stderr. A run that executes
no entry prints nothing on stdout.

**Outcome.** Success when every entry is red; a failure (exit 1) when any is
green, stale or inconclusive; a refusal (exit 3) when no entry ran:
``mutation_list`` for a block the contract refuses on its face (none, more
than one, empty, an unknown key, a missing field, a bad mutation kind),
``sandbox`` for no OS sandbox or a worktree preflight that fails before the
first entry. Anything only the tree can show (a path gone, a locate that no
longer matches) is that entry's STALE line.

**Where the record goes** (d-cm-8ap.19-14). On a checkout with ``.curamoir/``
the run writes ``.curamoir/state/r5/runs/<run id>/``: ``mutations.yaml``,
the block's content byte for byte, and ``results.jsonl``, the lines it
printed. The gate itself writes under its bead (R5-R30); this command has
no ``--bead``. On a checkout without ``.curamoir/`` nothing is written
under the root (runner.Job.runs_uninitialised).
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import threading
from pathlib import Path

from curamoir.config import ConfigError, safe_load_unique
from curamoir.gate import mutation_list, mutations, worktrees
from curamoir.git_guard import GitGuardError, resolve_base
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Refusal
from curamoir.repo_config import CONFIG_PATH, strict_config

RECORD_DIR = (".curamoir", "state", "r5", "runs")
OPTIONS = ("base", "head", "brief")


def run_mutations_job(repo, run, *, base, head, brief, emit, notice):
    """The job's body: `run` reports the outcome (a JobRun, or the runner's
    unledgered stand-in); `emit(text)` writes one stdout line, `notice(text)`
    one stderr line."""
    repo = Path(repo)
    initialised = (repo / ".curamoir").is_dir()
    try:
        text = Path(brief).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        run.refused(Refusal(Reason.MUTATION_LIST, f"the brief {brief} cannot be read: {error}"))
        return
    try:
        block = mutation_list.extract_block(text)
        mutation_list.parse_entries(block)
    except mutation_list.MutationListError as error:
        run.refused(Refusal(Reason.MUTATION_LIST, str(error)))
        return
    pinned = {}
    for name, value in (("base", base), ("head", head)):
        try:
            # Pinned to the commit now: each entry's worktree is made when a
            # slot frees, and a ref (HEAD, a branch) could have moved by then.
            pinned[name] = resolve_base(repo, value)
        except GitGuardError as error:
            run.refused(Refusal(Reason.SANDBOX, f"no mutation ran: --{name} {value!r} does "
                                                f"not name a commit here ({error})"))
            return
    try:
        config = strict_config(repo)
    except ConfigError as error:
        run.refused(Refusal(Reason.GUARD_ERROR, f"curamoir.yaml cannot be loaded: {error}"))
        return
    configured = config["gate"]["worktrees"]["max_parallel"]
    cap = worktrees.max_parallel(run.runner, configured, _set_explicitly(repo))
    # The record is made with the first result, so a run refused before any
    # entry leaves none; a record that cannot be written is a notice, and
    # the run's own lines still stand.
    record = {"dir": _record_dir(repo) if initialised else None, "made": False}
    # Results arrive from the runner's worker threads: one at a time, so a
    # line is printed and recorded whole and the record is made once.
    writing = threading.Lock()

    def printed(result):
        with writing:
            _printed(result)

    def _printed(result):
        line = json.dumps(result.line(), sort_keys=True)
        emit(line)
        if record["dir"] is None:
            return
        try:
            if not record["made"]:
                record["dir"].mkdir(parents=True, exist_ok=True)
                (record["dir"] / "mutations.yaml").write_text(block, encoding="utf-8")
                record["made"] = True
            with open(record["dir"] / "results.jsonl", "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as error:
            notice(f"the run's record {record['dir']} could not be written: {error}")
            record["dir"] = None

    try:
        results = mutations.run_mutations(
            repo, block, base=pinned["base"], head=pinned["head"], runner=run.runner, cap=cap,
            timeout=config["sweep"]["test_timeout_seconds"], on_result=printed, notices=notice)
    except mutations.SandboxMissing as error:
        run.refused(Refusal(Reason.SANDBOX, f"no mutation ran: {error}"))
        return
    except mutations.CleanupFailed as error:
        # Every entry ran and printed its line; what failed is the clean-up
        # after them, which is a failure, never "no mutation ran".
        run.failed(f"{mutations.summary(error.results)}; then {error}")
        return
    except worktrees.WorktreeError as error:
        run.refused(Refusal(Reason.SANDBOX, f"no mutation ran: {error}"))
        return
    sentence = mutations.summary(results)
    if any(r.result in mutations.BLOCKING for r in results):
        run.failed(sentence)
    else:
        run.succeeded(sentence)


def _set_explicitly(repo: Path) -> bool:
    """Whether curamoir.yaml itself sets gate.worktrees.max_parallel (the
    hosted cap applies it only then, d-cm-8ap.19-15)."""
    try:
        data = safe_load_unique(repo.joinpath(*CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    try:
        return "max_parallel" in data["gate"]["worktrees"]
    except (KeyError, TypeError):
        return False


def _record_dir(repo: Path) -> Path:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return repo.joinpath(*RECORD_DIR) / f"{now}-{os.getpid()}-{secrets.token_hex(3)}"
