"""gate/digest.py (cm-8ap.14): the verdict's inputs and input_digest.

Spec: docs/r5-gate-v2-curamoir-spec.md R5-R75 and §11 "Verdict inputs":
a whole tracked file is ``git:<blob id>``, anything else ``sha256:`` over its
exact bytes; ``input_digest`` is the sha256 of the canonical JSON (UTF-8,
keys sorted, no insignificant whitespace) of ``tree_sha`` and ``inputs``;
no digest is taken from a name, size or mtime, and none is accepted as an
argument. Each test names the rule it pins.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from curamoir.gate import digest
from curamoir.gate.digest import DigestError, VerdictInputs


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
    (root / "lens").mkdir()
    (root / "lens" / "correctness.md").write_text("# Correctness Reviewer\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


TREE = "a" * 40


# --- one input (§11: git:<blob id> or sha256 over the exact bytes) ---------------

def test_a_tracked_unmodified_file_is_its_blob_id(repo):
    assert digest.of_file(repo, "brief.md") == "git:" + _git(repo, "rev-parse", "HEAD:brief.md")
    assert digest.of_file(repo, "lens/correctness.md") == \
        "git:" + _git(repo, "rev-parse", "HEAD:lens/correctness.md")


def test_a_tracked_file_changed_on_disk_is_the_sha256_of_its_bytes(repo):
    (repo / "brief.md").write_bytes(b"edited, not committed\n")
    assert digest.of_file(repo, "brief.md") == _sha256(b"edited, not committed\n")


def test_an_untracked_file_is_the_sha256_of_its_bytes(repo):
    (repo / "plan.md").write_bytes(b"a plan\r\nwith CRLF\r\n")
    assert digest.of_file(repo, "plan.md") == _sha256(b"a plan\r\nwith CRLF\r\n")


def test_a_file_checked_out_through_a_line_ending_rule_is_digested_as_it_is_on_disk(repo):
    # The blob id is computed from the bytes on disk, not through git's
    # filters: a file stored with LF and checked out as CRLF is not the
    # committed blob byte for byte, so it is recorded by sha256 of its bytes.
    (repo / ".gitattributes").write_text("*.txt text eol=crlf\n", encoding="utf-8")
    (repo / "notes.txt").write_bytes(b"one\ntwo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attributes")
    (repo / "notes.txt").unlink()
    _git(repo, "checkout", "--", "notes.txt")
    on_disk = (repo / "notes.txt").read_bytes()
    assert on_disk == b"one\r\ntwo\r\n"
    assert digest.of_file(repo, "notes.txt") == _sha256(on_disk)


def test_a_files_name_size_and_mtime_do_not_make_its_digest(repo):
    first = digest.of_file(repo, "brief.md")
    os.utime(repo / "brief.md", (0, 0))
    assert digest.of_file(repo, "brief.md") == first
    (repo / "copy.md").write_bytes((repo / "brief.md").read_bytes())
    assert digest.of_file(repo, "copy.md") == _sha256((repo / "brief.md").read_bytes())
    (repo / "brief.md").write_text("the bead's briefX", encoding="utf-8")  # same size + 0
    assert digest.of_file(repo, "brief.md") != first


@pytest.mark.parametrize("path", ["missing.md", "lens", "../outside.md"])
def test_a_path_that_is_not_a_file_in_the_repository_is_refused(repo, path):
    (repo.parent / "outside.md").write_text("outside\n", encoding="utf-8")
    with pytest.raises(DigestError):
        digest.of_file(repo, path)


def test_a_symlink_out_of_the_repository_is_refused(repo):
    (repo.parent / "secret.md").write_text("outside\n", encoding="utf-8")
    (repo / "link.md").symlink_to(repo.parent / "secret.md")
    with pytest.raises(DigestError, match="outside the repository"):
        digest.of_file(repo, "link.md")


def test_of_bytes_takes_bytes_only():
    assert digest.of_bytes(b"") == _sha256(b"")
    with pytest.raises(DigestError):
        digest.of_bytes("text")


# --- input_digest (§11: canonical JSON of tree_sha and inputs) -------------------

def test_input_digest_is_the_sha256_of_the_canonical_json(repo):
    built = digest.verdict_inputs(repo, TREE, files={"brief": "brief.md"},
                                  contents={"runner_version": "0.1.0 ✓".encode()})
    expected_inputs = {"brief": "git:" + _git(repo, "rev-parse", "HEAD:brief.md"),
                       "runner_version": _sha256("0.1.0 ✓".encode())}
    assert dict(built.inputs) == expected_inputs
    canonical = json.dumps({"inputs": expected_inputs, "tree_sha": TREE}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert built.input_digest == _sha256(canonical)
    assert built.tree_sha == TREE


def test_the_canonical_form_is_compact_sorted_utf8():
    assert digest.canonical({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_the_same_tree_and_inputs_give_the_same_digest_whatever_the_order(repo):
    one = digest.verdict_inputs(repo, TREE, files={"brief": "brief.md", "lens:correctness": "lens/correctness.md"},
                                contents={"gate_config": b"max_rounds: 3\n"})
    two = digest.verdict_inputs(repo, TREE, contents={"gate_config": b"max_rounds: 3\n"},
                                files={"lens:correctness": "lens/correctness.md", "brief": "brief.md"})
    assert one.input_digest == two.input_digest
    assert list(one.inputs) == sorted(one.inputs)


def test_a_different_tree_or_input_changes_the_digest(repo):
    base = digest.verdict_inputs(repo, TREE, contents={"gate_config": b"a"})
    assert digest.verdict_inputs(repo, "b" * 40, contents={"gate_config": b"a"}).input_digest \
        != base.input_digest
    assert digest.verdict_inputs(repo, TREE, contents={"gate_config": b"b"}).input_digest \
        != base.input_digest


@pytest.mark.parametrize("files, contents", [
    ({"notes": "brief.md"}, {}),
    ({}, {"lens:": b"x"}),
    ({}, {"lens:Correctness": b"x"}),
    ({"brief": "brief.md"}, {"brief": b"x"}),
    ({}, {1: b"x"}),
])
def test_only_the_input_names_the_contract_lists_are_taken(repo, files, contents):
    with pytest.raises(DigestError):
        digest.verdict_inputs(repo, TREE, files=files, contents=contents)


@pytest.mark.parametrize("tree", ["", "abc", "A" * 40, None, "a" * 41])
def test_tree_sha_must_be_a_full_object_id(repo, tree):
    with pytest.raises(DigestError):
        digest.verdict_inputs(repo, tree)


def test_no_digest_is_accepted_from_the_caller(repo):
    # R5-R75: no command accepts a digest as an argument. A digest string
    # given where content is expected is digested as content, never stored.
    planted = "sha256:" + "d" * 64
    built = digest.verdict_inputs(repo, TREE, contents={"brief": planted.encode()})
    assert built.inputs["brief"] == _sha256(planted.encode()) != planted
    with pytest.raises(DigestError):
        digest.verdict_inputs(repo, TREE, contents={"brief": planted})


def test_verdict_inputs_cannot_be_built_outside_the_module():
    with pytest.raises(TypeError, match="verdict_inputs only"):
        VerdictInputs(tree_sha=TREE, inputs={}, input_digest="sha256:" + "0" * 64)


def test_verdict_inputs_are_read_only(repo):
    built = digest.verdict_inputs(repo, TREE, contents={"brief": b"x"})
    with pytest.raises(TypeError):
        built.inputs["brief"] = "sha256:" + "0" * 64


@pytest.mark.parametrize("path", [None, 3, b"brief.md"])
def test_an_input_path_that_is_not_a_path_is_a_digest_error(repo, path):
    with pytest.raises(DigestError, match="not a path"):
        digest.verdict_inputs(repo, TREE, files={"brief": path})


def test_a_repository_argument_below_the_work_tree_top_is_refused(repo):
    # The tracked-blob lookup reads HEAD's tree from the root, so a
    # sub-directory would make every tracked file read as untracked.
    with pytest.raises(DigestError, match="not the top of its git work tree"):
        digest.of_file(repo / "lens", "correctness.md")
