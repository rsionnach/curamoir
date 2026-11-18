"""gate/record.py (cm-8ap.14): the round record and the tree-keyed verdict.

Spec: docs/r5-gate-v2-curamoir-spec.md R5-R48, R5-R49, R5-R75 and §11
("Round record", "Verdict check", "Verdict inputs"); bead 2 of §10 closes
when

- a crash mid-round leaves the completed rounds readable;
- the gate guard's check refuses on a stale ``tree_sha``;
- two runs at one tree with the same inputs give the same ``input_digest``
  (also in test_gate_digest.py);
- a digest planted in a reviewer's output does not reach the record.

Each test names the rule it pins. The reviewer report in the planted-digest
test is written for that test, from the §11 Finding shape; none is a real
report.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from curamoir.gate import digest, findings, record
from curamoir.gate.record import RecordError, Refusal


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "brief.md").write_text("the bead's brief\n", encoding="utf-8")
    (root / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _head(repo):
    return _git(repo, "rev-parse", "HEAD")


def _tree(repo):
    return _git(repo, "rev-parse", "HEAD^{tree}")


def _start(repo, bead="cm-x.1"):
    head = _head(repo)
    return record.start(repo, bead, repo_name="curamoir", branch="bead/cm-x.1", base=head, head=head)


def _round(repo, n, kind="review", lenses=None):
    return {"n": n, "kind": kind, "head": _head(repo), "started": "2026-11-17T10:00:00Z",
            "ended": "2026-11-17T10:05:00Z",
            "lenses": lenses if lenses is not None else {
                "correctness": {"model": "m", "tier": "deep", "mode": "review", "tokens_in": 1,
                                "tokens_out": 2, "duration_s": 3, "status": "ok", "skip_rule": None,
                                "findings": [], "closures": [], "closure_rejected": [],
                                "unaccounted": [], "downgrades_refused": [], "prose_unfiled": []}}}


# --- the record's place and shape (R5-R48, §11 "Round record") ------------------

def test_the_record_lives_under_the_beads_state_directory(repo):
    _start(repo)
    path = repo / ".curamoir" / "state" / "r5" / "cm-x.1" / "record.json"
    assert path.is_file()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"bead": "cm-x.1", "repo": "curamoir", "branch": "bead/cm-x.1",
                       "base": _head(repo), "head": _head(repo), "rounds": []}


@pytest.mark.parametrize("bead", ["", ".", "..", "a/b", "../x", "-x", "a b", "x\n", None])
def test_a_bead_id_that_is_not_one_path_component_is_refused(repo, bead):
    with pytest.raises(RecordError):
        record.record_path(repo, bead)


def test_starting_over_an_existing_record_refuses_unless_told_to_replace(repo):
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    with pytest.raises(RecordError, match="already exists"):
        _start(repo)
    assert len(record.load(repo, "cm-x.1")["rounds"]) == 1
    head = _head(repo)
    record.start(repo, "cm-x.1", repo_name="curamoir", branch="b", base=head, head=head, replace=True)
    assert record.load(repo, "cm-x.1")["rounds"] == []


@pytest.mark.parametrize("name, value", [("base", "abc"), ("head", "Z" * 40), ("base", None),
                                         ("repo_name", ""), ("branch", " "), ("branch", None)])
def test_start_needs_full_object_ids_and_names(repo, name, value):
    head = _head(repo)
    kwargs = {"repo_name": "curamoir", "branch": "b", "base": head, "head": head, name: value}
    with pytest.raises(RecordError, match=name):
        record.start(repo, "cm-x.1", **kwargs)


def test_rounds_are_numbered_from_one_in_order(repo):
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    for wrong in (1, 3, True):
        with pytest.raises(RecordError, match="must be 2"):
            record.append_round(repo, "cm-x.1", _round(repo, wrong))
    record.append_round(repo, "cm-x.1", _round(repo, 2, kind="fix"))
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1, 2]


@pytest.mark.parametrize("change, reason", [
    ({"kind": "rest"}, "kind"),
    ({"head": "nope"}, "round head"),
    ({"started": ""}, "started"),
    ({"ended": ""}, "ended"),
    ({"ended": 5}, "ended"),
    ({"lenses": []}, "lenses"),
])
def test_a_round_outside_the_contract_is_refused(repo, change, reason):
    _start(repo)
    with pytest.raises(RecordError, match=reason):
        record.append_round(repo, "cm-x.1", {**_round(repo, 1), **change})


@pytest.mark.parametrize("key", ["n", "kind", "head", "started", "ended", "lenses"])
def test_a_round_missing_a_required_key_is_refused(repo, key):
    _start(repo)
    raw = _round(repo, 1)
    del raw[key]
    with pytest.raises(RecordError, match=f"missing {key}"):
        record.append_round(repo, "cm-x.1", raw)


def test_no_round_is_added_after_the_verdict(repo):
    _start(repo)
    record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                       at="2026-11-17T11:00:00Z")
    with pytest.raises(RecordError, match="already has its verdict"):
        record.append_round(repo, "cm-x.1", _round(repo, 1))
    with pytest.raises(RecordError, match="already has its verdict"):
        record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                           at="2026-11-17T11:00:00Z")


def test_the_verdict_state_is_one_the_contract_names(repo):
    _start(repo)
    with pytest.raises(RecordError, match="verdict state"):
        record.set_verdict(repo, "cm-x.1", "PASS", tree_sha=_tree(repo), head_sha=_head(repo),
                           at="2026-11-17T11:00:00Z")


# --- a crash mid-round (R5-R48; §10 bead 2) --------------------------------------

def test_a_crash_while_writing_a_round_leaves_the_completed_rounds(repo, monkeypatch):
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    record.append_round(repo, "cm-x.1", _round(repo, 2, kind="fix"))
    before = (repo / ".curamoir/state/r5/cm-x.1/record.json").read_bytes()

    def crash(*args):
        raise OSError("the disk went away mid-round")

    monkeypatch.setattr(record.os, "replace", crash)
    with pytest.raises(RecordError, match="cannot be written"):
        record.append_round(repo, "cm-x.1", _round(repo, 3, kind="verify"))
    monkeypatch.undo()

    path = repo / ".curamoir/state/r5/cm-x.1/record.json"
    assert path.read_bytes() == before
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1, 2]
    assert list(path.parent.glob("*.tmp")) == []


def test_a_writer_killed_before_its_rename_leaves_the_record_readable(repo):
    # A process killed after writing its temporary file and before renaming
    # it: the code below runs the module's own writer in a child, which is
    # killed with SIGKILL from inside os.replace.
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    script = (
        "import os, signal, sys, json\n"
        "from curamoir.gate import record\n"
        "os.replace = lambda *a: os.kill(os.getpid(), signal.SIGKILL)\n"
        f"record.append_round({str(repo)!r}, 'cm-x.1', json.loads(sys.argv[1]))\n")
    child = subprocess.run(["python3", "-c", script, json.dumps(_round(repo, 2))],
                           env={**os.environ, "PYTHONPATH": os.pathsep.join(
                               [str(record.__file__).rsplit("/curamoir/", 1)[0],
                                os.environ.get("PYTHONPATH", "")])},
                           capture_output=True, text=True, timeout=60)
    assert child.returncode == -9, child.stderr
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1]
    stray = list((repo / ".curamoir/state/r5/cm-x.1").glob("*.tmp"))
    assert len(stray) == 1  # the killed writer's; the next write is unaffected
    record.append_round(repo, "cm-x.1", _round(repo, 2))
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1, 2]


@pytest.mark.parametrize("content, reason", [
    (None, "no gate record"),
    (b"{\"rounds\": [", "not JSON"),
    (b"[]", "not a round record"),
    (b"{\"rounds\": {}}", "not a round record"),
    (b"\xff\xfe", "cannot be read"),
])
def test_a_missing_or_broken_record_is_reported_not_trusted(repo, content, reason):
    path = record.record_path(repo, "cm-x.1")
    if content is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    with pytest.raises(RecordError, match=reason):
        record.load(repo, "cm-x.1")


# --- the verdict check (R5-R49, §11 "Verdict check") ----------------------------

def _clean(repo, bead="cm-x.1"):
    record.set_verdict(repo, bead, "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                       at="2026-11-17T11:00:00Z")


def test_a_clean_verdict_at_the_current_tree_passes(repo):
    _start(repo)
    _clean(repo)
    assert record.check_verdict(repo, "cm-x.1") is None


def test_a_stale_tree_sha_refuses_naming_the_mismatch(repo):
    _start(repo)
    _clean(repo)
    gated = _tree(repo)
    (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "a change after the gate")
    refusal = record.check_verdict(repo, "cm-x.1")
    assert isinstance(refusal, Refusal)
    assert gated in str(refusal) and _tree(repo) in str(refusal)
    assert "changed" in str(refusal)


def test_a_message_edit_that_keeps_the_tree_keeps_the_verdict(repo):
    _start(repo)
    _clean(repo)
    _git(repo, "commit", "-q", "--amend", "-m", "reworded, same tree")
    assert record.check_verdict(repo, "cm-x.1") is None


def test_an_uncommitted_change_does_not_pass_as_the_gated_tree(repo):
    # The check reads HEAD's tree: a change still in the working copy is not
    # part of what HEAD names, so the verdict still describes HEAD exactly.
    _start(repo)
    _clean(repo)
    (repo / "src.py").write_text("x = 3\n", encoding="utf-8")
    assert record.check_verdict(repo, "cm-x.1") is None
    _git(repo, "commit", "-qam", "now committed")
    assert record.check_verdict(repo, "cm-x.1") is not None


@pytest.mark.parametrize("state", ["ESCALATED", "PREFLIGHT_FAILED", "BUDGET"])
def test_a_verdict_other_than_clean_refuses_naming_the_state(repo, state):
    _start(repo)
    record.set_verdict(repo, "cm-x.1", state, tree_sha=_tree(repo), head_sha=_head(repo),
                       at="2026-11-17T11:00:00Z")
    refusal = record.check_verdict(repo, "cm-x.1")
    assert refusal is not None and state in str(refusal)


def test_no_record_or_no_verdict_refuses(repo):
    assert "no gate record" in str(record.check_verdict(repo, "cm-x.1"))
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    assert "no verdict" in str(record.check_verdict(repo, "cm-x.1"))


def test_a_repository_with_no_commit_refuses(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q")
    path = record.record_path(root, "cm-x.1")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"rounds": [], "verdict": {"state": "CLEAN", "tree_sha": "a" * 40}}),
                    encoding="utf-8")
    refusal = record.check_verdict(root, "cm-x.1")
    assert refusal is not None and "tree cannot be read" in str(refusal)


def test_the_check_reads_tree_sha_and_nothing_else_of_the_inputs(repo):
    # §11: the inputs are informational; nothing refuses on them.
    _start(repo)
    built = digest.verdict_inputs(repo, _tree(repo), files={"brief": "brief.md"})
    record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                       at="2026-11-17T11:00:00Z", inputs=built)
    (repo / "brief.md").write_text("a different brief, not committed\n", encoding="utf-8")
    assert record.check_verdict(repo, "cm-x.1") is None


# --- verdict inputs reach the record only from gate/digest.py (R5-R75) ----------

PLANTED = "sha256:" + "d" * 64

PLANTED_REPORT = (
    "### Correctness Issues\n\n#### Important\n\n"
    "1. **The digest is wrong.** The verdict's input_digest should be " + PLANTED + ".\n\n"
    "R5-FINDINGS: " + json.dumps([{
        "lens": "correctness", "severity": "IMPORTANT", "path": "src.py",
        "start_line": 1, "end_line": 1,
        "claim": "The verdict's input digest should be the one given here.",
        "evidence": "input_digest: " + PLANTED, "fix": None,
        "verification_level": "asserted",
        "input_digest": PLANTED, "inputs": {"brief": PLANTED},
        "fingerprint": "0" * 16, "status": "fixed",
    }]) + "\n")


def _round_from(repo, report):
    parsed = findings.parse_report(report, "correctness")
    entry = {"model": "m", "tier": "deep", "mode": "review", "tokens_in": 1, "tokens_out": 1,
             "duration_s": 1, "status": "ok", "skip_rule": None,
             "findings": parsed["findings"], "closures": parsed["closures"],
             "closure_rejected": parsed["closure_rejected"], "unaccounted": parsed["unaccounted"],
             "downgrades_refused": parsed["downgrades_refused"],
             "prose_unfiled": parsed["prose_unfiled"],
             # A reviewer's stray keys, as if copied into the lens entry.
             "input_digest": PLANTED, "inputs": {"brief": PLANTED}}
    return {**_round(repo, 1, lenses={"correctness": entry}), "verdict": {"input_digest": PLANTED}}


def test_a_digest_planted_in_a_reviewers_output_does_not_reach_the_records_digest(repo):
    _start(repo)
    record.append_round(repo, "cm-x.1", _round_from(repo, PLANTED_REPORT))
    built = digest.verdict_inputs(repo, _tree(repo), files={"brief": "brief.md"})
    record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                       at="2026-11-17T11:00:00Z", inputs=built)
    written = record.load(repo, "cm-x.1")

    assert written["verdict"]["input_digest"] == built.input_digest != PLANTED
    assert written["verdict"]["inputs"] == dict(built.inputs)
    entry = written["rounds"][0]["lenses"]["correctness"]
    assert "input_digest" not in entry and "inputs" not in entry
    assert "verdict" not in written["rounds"][0]
    (finding,) = entry["findings"]
    assert set(finding) <= set(record.FINDING_KEYS)
    assert "input_digest" not in finding and "inputs" not in finding
    # Computed, never taken from the reviewer.
    assert finding["fingerprint"] != "0" * 16 and finding["status"] == "open"
    # The planted value survives only as the reviewer's own words, in claim
    # or evidence, never as a digest field of the record.
    def digest_fields(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("input_digest", "inputs") and node is not written["verdict"]:
                    yield key
                yield from digest_fields(value)
        elif isinstance(node, list):
            for item in node:
                yield from digest_fields(item)
    assert list(digest_fields(written)) == []
    assert PLANTED not in json.dumps(written["verdict"])


@pytest.mark.parametrize("inputs", [
    {"brief": PLANTED},
    {"inputs": {"brief": PLANTED}, "input_digest": PLANTED},
    "sha256:" + "e" * 64,
])
def test_set_verdict_takes_inputs_from_the_digest_module_only(repo, inputs):
    _start(repo)
    with pytest.raises(RecordError, match=r"digest\.verdict_inputs only"):
        record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                           at="2026-11-17T11:00:00Z", inputs=inputs)
    assert "verdict" not in record.load(repo, "cm-x.1")


def test_inputs_digested_for_another_tree_are_refused(repo):
    _start(repo)
    other = digest.verdict_inputs(repo, "f" * 40, files={"brief": "brief.md"})
    with pytest.raises(RecordError, match="different tree_sha"):
        record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                           at="2026-11-17T11:00:00Z", inputs=other)


def test_two_runs_at_one_tree_with_the_same_inputs_record_the_same_input_digest(repo):
    # §10 bead 2: the same inputs at the same tree give the same input_digest,
    # here through two separate records as two gate runs would write them.
    for bead in ("cm-x.1", "cm-x.2"):
        _start(repo, bead)
        built = digest.verdict_inputs(repo, _tree(repo), files={"brief": "brief.md", "plan": "src.py"},
                                      contents={"gate_config": b"max_rounds: 3\n",
                                                "runner_version": b"0.1.0"})
        record.set_verdict(repo, bead, "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                           at=f"2026-11-17T11:0{bead[-1]}:00Z", inputs=built)
    first, second = (record.load(repo, b)["verdict"] for b in ("cm-x.1", "cm-x.2"))
    assert first["input_digest"] == second["input_digest"]
    assert first["inputs"] == second["inputs"]
    assert first["at"] != second["at"]


def test_a_verdict_inputs_copy_with_another_digest_cannot_be_made_or_recorded(repo):
    # A VerdictInputs is not a dataclass, so dataclasses.replace cannot copy
    # one; and one altered by other means is caught when the record
    # recomputes input_digest from its inputs and tree_sha.
    import dataclasses
    _start(repo)
    built = digest.verdict_inputs(repo, _tree(repo), files={"brief": "brief.md"})
    with pytest.raises(TypeError):
        dataclasses.replace(built, input_digest=PLANTED)
    with pytest.raises(AttributeError):
        built.input_digest = PLANTED
    forged = digest.verdict_inputs(repo, _tree(repo), files={"brief": "brief.md"})
    object.__setattr__(forged, "input_digest", PLANTED)
    with pytest.raises(RecordError, match="does not match its inputs"):
        record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                           at="2026-11-17T11:00:00Z", inputs=forged)
    reshaped = digest.verdict_inputs(repo, _tree(repo), files={"brief": "brief.md"})
    object.__setattr__(reshaped, "inputs", {"brief": "not a digest"})
    with pytest.raises(RecordError, match="not a git: or sha256: digest"):
        record.set_verdict(repo, "cm-x.1", "CLEAN", tree_sha=_tree(repo), head_sha=_head(repo),
                           at="2026-11-17T11:00:00Z", inputs=reshaped)
    assert "verdict" not in record.load(repo, "cm-x.1")


@pytest.mark.parametrize("n", [1.0, "1", None])
def test_a_rounds_n_must_be_an_integer(repo, n):
    _start(repo)
    with pytest.raises(RecordError, match="must be 1"):
        record.append_round(repo, "cm-x.1", _round(repo, n))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_a_value_json_cannot_hold_is_a_record_error_and_leaves_the_record(repo, value):
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    raw = _round(repo, 2)
    raw["lenses"]["correctness"]["duration_s"] = value
    with pytest.raises(RecordError, match="cannot be written as JSON"):
        record.append_round(repo, "cm-x.1", raw)
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1]


def test_a_gates_record_leaves_git_status_clean_and_the_verdict_survives_a_commit(repo):
    # Rob, 2026-11-18: .curamoir/.gitignore (and the one curamoir init
    # writes) ignores state/r5/, so committing everything after the gate
    # cannot put the record in the tree its verdict is keyed to.
    from curamoir.scaffold import GITIGNORE
    (repo / ".curamoir").mkdir()
    (repo / ".curamoir" / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "curamoir init")
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    _clean(repo)
    assert _git(repo, "status", "--porcelain") == ""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "commit everything after the gate")
    assert record.check_verdict(repo, "cm-x.1") is None


def test_a_writer_waits_for_the_records_lock(repo):
    # Each change holds an exclusive flock on record.lock for its whole read,
    # change and write. Holding that lock here, a child's append_round must
    # wait for it, and finish only once it is released.
    import fcntl
    import time
    _start(repo)
    # The child says when it reaches the lock, so a slow start cannot pass
    # for waiting on it.
    script = (
        "import json, sys\n"
        "from curamoir.gate import record\n"
        "real = record.fcntl.flock\n"
        "def announced(fd, op):\n"
        "    print('at-lock', flush=True)\n"
        "    return real(fd, op)\n"
        "record.fcntl.flock = announced\n"
        f"record.append_round({str(repo)!r}, 'cm-x.1', json.loads(sys.argv[1]))\n"
        "print('ok', flush=True)\n")
    src = str(record.__file__).rsplit("/curamoir/", 1)[0]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    lock = record.record_path(repo, "cm-x.1").with_name(record.LOCK_NAME)
    with lock.open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        child = subprocess.Popen(["python3", "-c", script, json.dumps(_round(repo, 1))],
                                 env=env, stdout=subprocess.PIPE, text=True)
        reached = child.stdout.readline().strip()
        time.sleep(1)
        still_waiting = child.poll() is None
        rounds_while_held = len(record.load(repo, "cm-x.1")["rounds"])
    out = child.communicate(timeout=60)[0].strip()
    assert reached == "at-lock"
    assert still_waiting, "the child wrote while the lock was held"
    assert rounds_while_held == 0
    assert out == "ok" and child.returncode == 0
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1]


def test_threads_in_one_process_never_share_a_temporary_file(repo):
    import threading
    _start(repo)
    path = record.record_path(repo, "cm-x.1")
    errors = []

    def write():
        try:
            for _ in range(20):
                record._write(path, {"rounds": []})
        except Exception as error:  # collected and asserted below
            errors.append(error)

    threads = [threading.Thread(target=write) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert list(path.parent.glob("*.tmp")) == []


def test_a_write_the_disk_refuses_is_a_record_error_and_keeps_the_record(repo):
    _start(repo)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    directory = record.record_path(repo, "cm-x.1").parent
    before = (directory / "record.json").read_bytes()
    directory.chmod(0o500)
    try:
        if os.access(directory, os.W_OK):
            pytest.skip("this user can write a read-only directory (root)")
        with pytest.raises(RecordError):
            record.append_round(repo, "cm-x.1", _round(repo, 2))
    finally:
        directory.chmod(0o700)
    assert (directory / "record.json").read_bytes() == before


def test_a_record_holding_nan_or_infinity_is_refused_as_not_json(repo):
    for constant in ("NaN", "Infinity", "-Infinity"):
        path = record.record_path(repo, "cm-x.1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"rounds": [], "x": ' + constant + "}", encoding="utf-8")
        with pytest.raises(RecordError, match="not JSON"):
            record.load(repo, "cm-x.1")


def test_a_directory_fsync_the_file_system_refuses_does_not_report_a_written_round_as_failed(
        repo, monkeypatch):
    # The rename has already replaced the record when the directory is
    # fsynced; a file system that refuses that fsync (EINVAL) must not make
    # the caller retry a round that is on disk.
    _start(repo)
    real = os.fsync

    def refuse_directories(fd):
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "Invalid argument")
        return real(fd)

    monkeypatch.setattr(record.os, "fsync", refuse_directories)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    monkeypatch.undo()
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1]


# --- the record's own filters, fed directly (provenance round 1) ----------------

def test_findings_in_a_lens_entry_keep_only_the_finding_contracts_keys(repo):
    # The record's own filter, not the parser's: a finding with stray keys
    # handed straight to append_round is written with the contract's keys only.
    _start(repo)
    stray = {"lens": "correctness", "severity": "MINOR", "path": "src.py", "start_line": 1,
             "end_line": 1, "claim": "c", "evidence": "e", "fix": None, "fingerprint": "1" * 16,
             "resolves": None, "regression": False, "status": "open",
             "input_digest": PLANTED, "inputs": {"brief": PLANTED}, "note": "unknown key"}
    raw = _round(repo, 1)
    raw["lenses"]["correctness"]["findings"] = [stray]
    record.append_round(repo, "cm-x.1", raw)
    (written,) = record.load(repo, "cm-x.1")["rounds"][0]["lenses"]["correctness"]["findings"]
    assert set(written) == set(stray) - {"input_digest", "inputs", "note"}
    assert set(written) <= set(record.FINDING_KEYS)


@pytest.mark.parametrize("findings_value", ["x", {}, [1], [{"lens": "correctness"}, "y"]])
def test_a_lens_entrys_findings_must_be_a_list_of_objects(repo, findings_value):
    _start(repo)
    raw = _round(repo, 1)
    raw["lenses"]["correctness"]["findings"] = findings_value
    with pytest.raises(RecordError, match="must be a list of findings"):
        record.append_round(repo, "cm-x.1", raw)


@pytest.mark.parametrize("field, value", [("tree_sha", "abc"), ("head_sha", None), ("at", ""),
                                          ("at", 5), ("tree_sha", "G" * 40)])
def test_set_verdict_checks_its_shas_and_time(repo, field, value):
    _start(repo)
    kwargs = {"tree_sha": _tree(repo), "head_sha": _head(repo), "at": "2026-11-17T11:00:00Z",
              field: value}
    with pytest.raises(RecordError, match=field):
        record.set_verdict(repo, "cm-x.1", "CLEAN", **kwargs)
    assert "verdict" not in record.load(repo, "cm-x.1")


def test_the_record_is_fsynced_before_its_rename_and_its_directory_after(repo, monkeypatch):
    import stat
    _start(repo)
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("dir-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
        return real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(record.os, "fsync", fsync)
    monkeypatch.setattr(record.os, "replace", replace)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    assert events == ["file-fsync", "replace", "dir-fsync"]


def test_this_checkouts_own_gitignore_keeps_gate_records_out_of_git():
    # The repository's own .curamoir/.gitignore, not only the template:
    # its gate records must never be committable.
    from pathlib import Path

    from curamoir.scaffold import GITIGNORE
    root = Path(__file__).resolve().parents[1]
    ignore = (root / ".curamoir" / ".gitignore").read_text(encoding="utf-8").splitlines()
    patterns = [line for line in GITIGNORE.splitlines() if line and not line.startswith("#")]
    assert [p for p in patterns if p not in ignore] == []
    checked = subprocess.run(["git", "-C", str(root), "check-ignore", "-q",
                              ".curamoir/state/r5/cm-x.1/record.json"])
    assert checked.returncode == 0


def test_doctors_hint_for_a_missing_gitignore_names_every_pattern_init_writes(tmp_path):
    from curamoir.doctor import _check_gitignore
    from curamoir.scaffold import GITIGNORE
    finding = _check_gitignore(tmp_path)
    patterns = [line for line in GITIGNORE.splitlines() if line and not line.startswith("#")]
    assert finding.level == "warning"
    assert [p for p in patterns if p not in finding.fix] == []


def test_a_lock_that_cannot_be_taken_is_a_record_error(repo):
    _start(repo)
    lock = record.record_path(repo, "cm-x.1").with_name(record.LOCK_NAME)
    lock.unlink(missing_ok=True)
    lock.mkdir()  # a directory cannot be opened for appending
    with pytest.raises(RecordError, match="lock cannot be taken"):
        record.append_round(repo, "cm-x.1", _round(repo, 1))
    assert record.load(repo, "cm-x.1")["rounds"] == []


def test_a_directory_that_cannot_be_opened_for_its_fsync_does_not_fail_a_written_round(
        repo, monkeypatch):
    # The other best-effort branch: opening the directory for its fsync
    # fails (a bead directory with write and search but no read permission
    # does this). The round is already on disk, so append_round returns.
    _start(repo)
    real = os.open
    directory = str(record.record_path(repo, "cm-x.1").parent)

    def refuse_directory(path, flags, *args, **kwargs):
        if str(path) == directory:
            raise PermissionError(13, "Permission denied")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(record.os, "open", refuse_directory)
    record.append_round(repo, "cm-x.1", _round(repo, 1))
    monkeypatch.undo()
    assert [r["n"] for r in record.load(repo, "cm-x.1")["rounds"]] == [1]
