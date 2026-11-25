"""`curamoir run mutations` (cm-8ap.19, E5-R23) and the bridge's helper.

The command's contract is the user's of 2026-11-24 (bead notes): JSON lines
on stdout, each with index and total, the sentence and notices on stderr,
nothing on stdout when no entry ran; exit 0 all red, 1 any green, stale or
inconclusive, 3 a refusal (`mutation_list` for a block refused on its face,
`sandbox` for no OS sandbox or a failed worktree preflight), 4 an unusable
root; on a checkout with no .curamoir/ it runs with no ledger line and
writes nothing under the root, but only at the git top level. The bridge's
helper (.claude/hooks/r5-mutations.py) reads the bead's brief from bd and
decides the mechanical half of provenance on its exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from curamoir import green_gate, runner
from curamoir.cli import main
from tests.os_sandbox_stand_in import Absent
from tests.repo_root import REPO_ROOT
from tests.test_mutations import CORE, KILLED, SURVIVES, TESTS, _git
from tests.test_r5_supervisor import env_for, payload

HELPER = REPO_ROOT / ".claude" / "hooks" / "r5-mutations.py"
STATE = REPO_ROOT / ".claude" / "hooks" / "r5-state.sh"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "core.py").write_text(CORE, encoding="utf-8")
    (root / "tests" / "test_core.py").write_text(TESTS, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def brief(tmp_path, *entries, fence="```"):
    path = tmp_path / "brief.md"
    body = "".join(textwrap.dedent(e) for e in entries)
    path.write_text(f"# A bead\n\nSome text.\n\n{fence}r5-mutations\n{body}{fence}\n",
                    encoding="utf-8")
    return path


def cli(repo, brief_path, capsys, *, head=None, extra=()):
    head = head or _git(repo, "rev-parse", "HEAD")
    code = main(["run", "mutations", "--repo", str(repo), "--base", head, "--head", head,
                 "--brief", str(brief_path), *extra])
    out, err = capsys.readouterr()
    return code, out, err


def files_under(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if ".git" not in p.relative_to(root).parts)


# --- the output contract --------------------------------------------------------

def test_json_lines_on_stdout_and_the_sentence_on_stderr(repo, tmp_path, capsys):
    before = files_under(repo)

    code, out, err = cli(repo, brief(tmp_path, KILLED, SURVIVES), capsys)

    lines = [json.loads(line) for line in out.splitlines()]
    assert code == runner.EXIT_FAILURE
    assert sorted((line["index"], line["total"], line["result"]) for line in lines) == \
        [(1, 2, "red"), (2, 2, "green")]
    assert all(set(line) >= {"path", "locate", "mutation", "behaviour", "reason", "diff",
                             "worktree", "output_tail", "duration_s"} for line in lines)
    assert err.startswith("cúramóir ran 'mutations' with no ledger line (no .curamoir/ here):"
                          " failure.")
    assert "1 green of 2 mutation(s)" in err
    # Uninitialised: nothing is written under the root.
    assert files_under(repo) == before


def test_all_red_exits_0(repo, tmp_path, capsys):
    code, out, err = cli(repo, brief(tmp_path, KILLED, fence="~~~"), capsys)

    assert code == runner.EXIT_SUCCESS
    assert [json.loads(line)["result"] for line in out.splitlines()] == ["red"]
    assert "all 1 mutation(s) were killed" in err


@pytest.mark.parametrize(("text", "why"), [
    ("# no block\n", "has no fenced `r5-mutations` block"),
    ("```r5-mutations\n[]\n```\n", "non-empty list"),
    ("```r5-mutations\n- path: a.py\n```\n", "missing"),
    ("```r5-mutations\n- {path: a.py, locate: {lines: [1, 1]}, mutation: flip, "
     "expect_red: x}\n```\n", "is not one of"),
    ("```r5-mutations\n- {path: a.py, locate: {lines: [1, 1]}, mutation: invert_condition, "
     "expect_red: x, behavior: y}\n```\n", "the field is `behaviour`"),
])
def test_a_block_refused_on_its_face_is_mutation_list_exit_3_and_nothing_on_stdout(
        repo, tmp_path, capsys, text, why):
    path = tmp_path / "brief.md"
    path.write_text(text, encoding="utf-8")

    code, out, err = cli(repo, path, capsys)

    assert code == runner.EXIT_REFUSED
    assert out == ""
    assert "refused (mutation_list)" in err and why in err


def test_an_unreadable_brief_is_a_mutation_list_refusal(repo, tmp_path, capsys):
    code, out, err = cli(repo, tmp_path / "missing.md", capsys)

    assert (code, out) == (runner.EXIT_REFUSED, "")
    assert "refused (mutation_list)" in err and "cannot be read" in err


def test_no_os_sandbox_is_a_sandbox_refusal(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())

    code, out, err = cli(repo, brief(tmp_path, KILLED), capsys)

    assert (code, out) == (runner.EXIT_REFUSED, "")
    assert "refused (sandbox)" in err


def test_a_head_that_is_not_a_commit_is_a_sandbox_refusal(repo, tmp_path, capsys):
    code, out, err = cli(repo, brief(tmp_path, KILLED), capsys, head="f" * 40)

    assert (code, out) == (runner.EXIT_REFUSED, "")
    assert "refused (sandbox)" in err and "--base" in err


def test_a_path_gone_from_the_tree_is_a_stale_line_and_exit_1(repo, tmp_path, capsys):
    gone = KILLED.replace("src/pkg/core.py", "src/pkg/gone.py")

    code, out, _ = cli(repo, brief(tmp_path, gone), capsys)

    (line,) = [json.loads(text) for text in out.splitlines()]
    assert code == runner.EXIT_FAILURE and line["result"] == "stale"


# --- where it may run ------------------------------------------------------------

def test_with_no_curamoir_only_the_git_top_level_runs(repo, tmp_path, capsys):
    code, out, err = cli(repo / "src", brief(tmp_path, KILLED), capsys)

    assert (code, out) == (runner.EXIT_UNUSABLE_ROOT, "")
    assert "not the top of a git work tree" in err


def test_a_curamoir_that_is_a_file_is_an_unusable_root(repo, tmp_path, capsys):
    (repo / ".curamoir").write_text("", encoding="utf-8")

    code, out, _ = cli(repo, brief(tmp_path, KILLED), capsys)

    assert (code, out) == (runner.EXIT_UNUSABLE_ROOT, "")


def test_an_undeclarable_runner_kind_is_refused(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CURAMOIR_RUNNER", "nonsense")

    code, out, err = cli(repo, brief(tmp_path, KILLED), capsys)

    assert (code, out) == (runner.EXIT_REFUSED, "")
    assert "refused (runner)" in err


def test_an_initialised_checkout_gets_a_ledger_line_and_the_record(repo, tmp_path, capsys):
    from curamoir.scaffold import init_repo

    init_repo(repo)
    path = brief(tmp_path, KILLED, SURVIVES)

    code, out, err = cli(repo, path, capsys)

    assert code == runner.EXIT_FAILURE
    assert err.startswith("cúramóir ran 'mutations': failure.")
    (record,) = (repo / ".curamoir" / "state" / "r5" / "runs").iterdir()
    block = path.read_text(encoding="utf-8").split("```r5-mutations\n")[1].split("```")[0]
    assert (record / "mutations.yaml").read_text(encoding="utf-8") == block
    assert (record / "results.jsonl").read_text(encoding="utf-8").splitlines() == \
        out.splitlines()
    ledger = (repo / ".curamoir" / "state" / "ledger.jsonl").read_text(encoding="utf-8")
    (line,) = [json.loads(text) for text in ledger.splitlines()
               if json.loads(text).get("job") == "mutations"]
    assert line["outcome"] == "failure"


def test_the_options_belong_to_the_mutations_job_alone(repo, capsys):
    code = main(["run", "brief", "--repo", str(repo), "--head", "abc"])
    out, _ = capsys.readouterr()
    assert code == runner.EXIT_NOT_A_JOB and "takes no --head" in out

    code = main(["run", "mutations", "--repo", str(repo), "--base", "a"])
    _, err = capsys.readouterr()
    assert code == runner.EXIT_NOT_A_JOB and "needs --head, --brief" in err


def test_the_jobs_say_whether_they_run_uninitialised():
    assert {name: job.runs_uninitialised for name, job in runner.JOBS.items()} == \
        {"brief": False, "sweep": False, "mutations": True}


# --- the bridge's helper (.claude/hooks/r5-mutations.py) ---------------------------

def fake_bd(tmp_path, payload=None, exit_code=0):
    """A `bd` on PATH that answers `show <id> --json` with `payload`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "bd"
    script.write_text(f"#!{sys.executable}\nimport json, sys\n"
                      f"sys.stdout.write({json.dumps(json.dumps(payload))})\n"
                      f"sys.exit({exit_code})\n", encoding="utf-8")
    script.chmod(0o755)
    return bin_dir


def helper(repo, tmp_path, bin_dir):
    """The helper, copied into `repo` beside a copy of the state helpers (it
    resolves the checkout from its own location), run with an R5 run in
    flight, as r5-supervise runs it; (CompletedProcess, mutations_run events)."""
    hooks = repo / ".claude" / "hooks"
    for source in (REPO_ROOT / ".claude" / "hooks").rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts:
            target = hooks / source.relative_to(REPO_ROOT / ".claude" / "hooks")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    (repo / ".claude-plugin").mkdir(exist_ok=True)
    (repo / ".claude-plugin" / "plugin.json").write_text('{"name": "curamoir"}')
    head = _git(repo, "rev-parse", "HEAD")
    env = helper_env(repo, bin_dir)
    for args in (("r5-lock.sh", "acquire", "r5-supervise", "cm-x.1"),
                 ("r5-state.sh", "init", "cm-x.1", head, head)):
        started = subprocess.run(["bash", str(hooks / args[0]), *args[1:]], cwd=repo, env=env,
                                 capture_output=True, text=True)
        assert started.returncode == 0, started.stderr
    done = subprocess.run([sys.executable, str(hooks / "r5-mutations.py"), "cm-x.1", head, head],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=300)
    state = json.loads((repo / ".claude" / "r5-state.json").read_text(encoding="utf-8"))
    events = [e for e in state["audit_trail"] if e["event"] == "mutations_run"]
    return done, events


def helper_env(repo, bin_dir):
    env = env_for(repo)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("PYTEST_ADDOPTS", None)
    return env


def bead(*entries):
    body = "".join(textwrap.dedent(e) for e in entries)
    return [{"id": "cm-x.1", "description": "Do the thing.",
             "notes": f"2026-11-24: the kill check.\n\n```r5-mutations\n{body}```\n"}]


def test_the_helper_passes_when_every_mutation_is_killed(repo, tmp_path):
    done, events = helper(repo, tmp_path, fake_bd(tmp_path, bead(KILLED)))

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.startswith("pass: all 1 mutation(s) killed")
    (event,) = events
    assert (event["verdict"], event["exit"], event["lines"], event["total"]) == \
        ("pass", 0, 1, 1)
    report = repo / event["report"]
    assert [json.loads(line)["result"] for line in report.read_text().splitlines()] == ["red"]


def test_the_helper_blocks_on_a_surviving_mutation(repo, tmp_path):
    done, events = helper(repo, tmp_path, fake_bd(tmp_path, bead(KILLED, SURVIVES)))

    assert done.returncode == 1
    assert [e["verdict"] for e in events] == ["block"]
    assert "entry 2 green" in events[0]["why"]


def test_a_brief_with_no_block_is_none(repo, tmp_path):
    done, events = helper(repo, tmp_path, fake_bd(tmp_path, [{"id": "cm-x.1",
                                                              "description": "No block."}]))

    assert done.returncode == 0
    assert done.stdout.startswith("none:") and "no mechanical kill check ran" in done.stdout
    assert [e["verdict"] for e in events] == ["none"]


def test_bd_failing_could_not_run(repo, tmp_path):
    done, events = helper(repo, tmp_path, fake_bd(tmp_path, {}, exit_code=1))

    assert done.returncode == 2
    assert [e["verdict"] for e in events] == ["could_not_run"]


def test_a_refused_block_could_not_run(repo, tmp_path):
    done, events = helper(repo, tmp_path, fake_bd(tmp_path, [{
        "id": "cm-x.1", "description": "```r5-mutations\n[]\n```\n"}]))

    assert done.returncode == 2
    assert [(e["verdict"], e["exit"]) for e in events] == [("could_not_run", 3)]


def test_done_provenance_follows_the_helper_not_the_reviewer(repo, tmp_path):
    """r5-state.sh refuses `done provenance` unless the last mutations_run
    since provenance was requested is pass or none, whatever the reviewer's
    findings say."""
    bin_dir = fake_bd(tmp_path, bead(KILLED, SURVIVES))
    _, events = helper(repo, tmp_path, bin_dir)
    hooks, env = repo / ".claude" / "hooks", helper_env(repo, bin_dir)

    def state(*args, stdin=""):
        return subprocess.run(["bash", str(hooks / args[0]), *args[1:]], cwd=repo, env=env,
                              input=stdin, capture_output=True, text=True)

    state_file = repo / ".claude" / "r5-state.json"
    data = json.loads(state_file.read_text())
    data["completed_passes"] = [{"name": "correctness", "fix_iterations": 0},
                                {"name": "edge-cases", "fix_iterations": 0}]
    state_file.write_text(json.dumps(data))
    clean = tmp_path / "clean.md"
    clean.write_text("All good.\n\nR5-FINDINGS: []\n", encoding="utf-8")

    def clean_round(verdict):
        assert state("r5-state.sh", "event", "mutations_run",
                     json.dumps(dict(events[0], verdict=verdict))).returncode == 0
        assert state("r5-parallel-block.sh", stdin=payload(cwd=repo)).returncode == 0
        found = state("r5-state.sh", "findings", "provenance", str(clean), "PASS")
        assert found.returncode == 0, found.stderr

    assert state("r5-state.sh", "pass", "provenance").returncode == 0
    clean_round("block")
    refused = state("r5-state.sh", "done", "provenance", "0")
    assert refused.returncode != 0
    assert "the mechanical kill check is block" in refused.stderr

    assert state("r5-state.sh", "request", "provenance", "2").returncode == 0
    clean_round("pass")
    assert state("r5-state.sh", "done", "provenance", "0").returncode == 0, "refused"




def test_the_helpers_reports_are_named_for_a_dotted_bead(repo, tmp_path):
    _, events = helper(repo, tmp_path, fake_bd(tmp_path, bead(KILLED)))

    assert events[0]["report"] == ".claude/r5-reports/cm-x.1-mutations-1.jsonl"
    assert (repo / ".claude" / "r5-reports" / "cm-x.1-mutations-1.brief.md").is_file()


# --- edge-cases review, round 1 --------------------------------------------------

def test_head_is_pinned_to_its_commit_before_any_entry(repo, tmp_path, capsys, monkeypatch):
    from curamoir.gate import mutations

    seen = {}
    real = mutations.run_mutations

    def spy(*args, **kwargs):
        seen.update(base=kwargs["base"], head=kwargs["head"])
        return real(*args, **kwargs)

    monkeypatch.setattr(mutations, "run_mutations", spy)
    code = main(["run", "mutations", "--repo", str(repo), "--base", "main", "--head", "HEAD",
                 "--brief", str(brief(tmp_path, KILLED))])
    capsys.readouterr()

    assert code == runner.EXIT_SUCCESS
    assert seen == {"base": _git(repo, "rev-parse", "main"),
                    "head": _git(repo, "rev-parse", "HEAD")}


def test_a_refused_run_on_an_initialised_checkout_leaves_no_record(repo, tmp_path, capsys,
                                                                  monkeypatch):
    from curamoir.scaffold import init_repo

    init_repo(repo)
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())

    code, out, _ = cli(repo, brief(tmp_path, KILLED), capsys)

    assert (code, out) == (runner.EXIT_REFUSED, "")
    assert not (repo / ".curamoir" / "state" / "r5" / "runs").exists()


def test_a_fence_inside_a_fence_is_no_block_to_the_helper(repo, tmp_path):
    documented = [{"id": "cm-x.1", "description":
                   "The format:\n\n````markdown\n```r5-mutations\n- path: a.py\n```\n````\n"}]

    done, events = helper(repo, tmp_path, fake_bd(tmp_path, documented))

    assert done.returncode == 0 and [e["verdict"] for e in events] == ["none"]



def test_a_record_that_cannot_be_written_is_a_notice_and_the_lines_still_print(
        repo, tmp_path, capsys):
    from curamoir.scaffold import init_repo

    init_repo(repo)
    runs = repo / ".curamoir" / "state" / "r5"
    runs.mkdir(parents=True, exist_ok=True)
    runs.chmod(0o500)
    try:
        code, out, err = cli(repo, brief(tmp_path, KILLED, SURVIVES), capsys)
    finally:
        runs.chmod(0o700)

    assert code == runner.EXIT_FAILURE
    assert sorted(json.loads(line)["result"] for line in out.splitlines()) == ["green", "red"]
    assert err.count("could not be written") == 1


# --- provenance review, round 1 ------------------------------------------------

@pytest.mark.parametrize(("yaml_text", "runner_env", "cores", "cap"), [
    ("gate:\n  worktrees:\n    max_parallel: 3\n", {}, 16, 3),
    ("", {}, 16, 2),
    ("gate:\n  worktrees:\n    max_parallel: 3\n",
     {"CURAMOIR_RUNNER": "hosted", "GITHUB_ACTIONS": "true",
      "RUNNER_ENVIRONMENT": "github-hosted"}, 16, 3),
    ("", {"CURAMOIR_RUNNER": "hosted", "GITHUB_ACTIONS": "true",
          "RUNNER_ENVIRONMENT": "github-hosted"}, 16, 8),
])
def test_the_configured_cap_reaches_the_runner(repo, tmp_path, capsys, monkeypatch, yaml_text,
                                               runner_env, cores, cap):
    from curamoir.gate import mutations
    from curamoir.scaffold import init_repo

    init_repo(repo)
    config = repo / ".curamoir" / "curamoir.yaml"
    config.write_text(config.read_text(encoding="utf-8") + yaml_text, encoding="utf-8")
    for key, value in runner_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    seen = {}
    monkeypatch.setattr(mutations, "run_mutations",
                        lambda *a, **k: seen.update(cap=k["cap"]) or [])

    cli(repo, brief(tmp_path, KILLED), capsys)

    assert seen == {"cap": cap}


def test_a_worktree_cap_of_0_is_refused_by_the_config():
    from curamoir.config import ConfigError, load_config

    with pytest.raises(ConfigError, match="max_parallel"):
        load_config({"gate": {"worktrees": {"max_parallel": 0}}})


def fake_curamoir(bin_dir, lines, exit_code):
    script = bin_dir / "curamoir"
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      f"sys.stdout.write({''.join(json.dumps(line) + chr(10) for line in lines)!r})\n"
                      f"sys.exit({exit_code})\n", encoding="utf-8")
    script.chmod(0o755)


RED_LINE = {"index": 1, "total": 2, "result": "red"}


def test_exit_1_with_every_line_red_still_blocks(repo, tmp_path):
    """A run that failed after its entries (a worktree it could not remove)
    is not a pass: only exit 0 with every line red is."""
    bin_dir = fake_bd(tmp_path, bead(KILLED))
    fake_curamoir(bin_dir, [RED_LINE, dict(RED_LINE, index=2)], 1)

    done, events = helper(repo, tmp_path, bin_dir)

    assert done.returncode == 1
    assert [e["verdict"] for e in events] == ["block"]
    assert "every entry red, but the run failed after them" in events[0]["why"]


@pytest.mark.parametrize(("lines", "exit_code"), [
    ([RED_LINE], 0),                               # fewer lines than total
    ([RED_LINE], 1),
])
def test_fewer_lines_than_total_could_not_run(repo, tmp_path, lines, exit_code):
    bin_dir = fake_bd(tmp_path, bead(KILLED))
    fake_curamoir(bin_dir, lines, exit_code)

    done, events = helper(repo, tmp_path, bin_dir)

    assert done.returncode == 2
    assert [e["verdict"] for e in events] == ["could_not_run"]


def test_a_brief_with_two_blocks_is_a_block_the_runner_refuses(repo, tmp_path):
    two = bead(KILLED)
    two[0]["description"] = "```r5-mutations\n" + textwrap.dedent(KILLED) + "```\n"

    done, events = helper(repo, tmp_path, fake_bd(tmp_path, two))

    assert done.returncode == 2
    assert [(e["verdict"], e["exit"]) for e in events] == [("could_not_run", 3)]


def test_done_provenance_needs_a_verdict_from_this_round(repo, tmp_path):
    bin_dir = fake_bd(tmp_path, bead(KILLED))
    _, events = helper(repo, tmp_path, bin_dir)
    hooks, env = repo / ".claude" / "hooks", helper_env(repo, bin_dir)

    def state(*args, stdin=""):
        return subprocess.run(["bash", str(hooks / args[0]), *args[1:]], cwd=repo, env=env,
                              input=stdin, capture_output=True, text=True)

    state_file = repo / ".claude" / "r5-state.json"
    data = json.loads(state_file.read_text())
    data["completed_passes"] = [{"name": "correctness", "fix_iterations": 0},
                                {"name": "edge-cases", "fix_iterations": 0}]
    state_file.write_text(json.dumps(data))
    clean = tmp_path / "clean.md"
    clean.write_text("All good.\n\nR5-FINDINGS: []\n", encoding="utf-8")
    assert state("r5-state.sh", "pass", "provenance").returncode == 0
    assert state("r5-state.sh", "event", "mutations_run",
                 json.dumps(dict(events[0], verdict="pass"))).returncode == 0
    assert state("r5-parallel-block.sh", stdin=payload(cwd=repo)).returncode == 0
    assert state("r5-state.sh", "findings", "provenance", str(clean), "PASS").returncode == 0

    # A new round, and no new mutations_run: the earlier pass does not count.
    assert state("r5-state.sh", "request", "provenance", "2").returncode == 0
    assert state("r5-parallel-block.sh", stdin=payload(cwd=repo)).returncode == 0
    assert state("r5-state.sh", "findings", "provenance", str(clean), "PASS").returncode == 0
    refused = state("r5-state.sh", "done", "provenance", "0")
    assert refused.returncode != 0 and "not run this round" in refused.stderr


# --- provenance review, round 2 ------------------------------------------------

def test_results_from_two_threads_are_printed_and_recorded_one_at_a_time(repo, tmp_path,
                                                                        monkeypatch):
    """The runner calls on_result from its worker threads. Two results are
    delivered at the same instant; each printing takes a while; they must
    never overlap, the record is made once, and every line is whole."""
    import threading
    import time

    from curamoir.gate import mutations
    from curamoir.scaffold import init_repo

    init_repo(repo)
    start, active, overlaps, made = threading.Barrier(2), [0], [], []
    real_mkdir = Path.mkdir

    def counting_mkdir(self, *args, **kwargs):
        if self.parent.name == "runs":
            made.append(self)
        return real_mkdir(self, *args, **kwargs)

    def emit(_line):
        active[0] += 1
        if active[0] > 1:
            overlaps.append(True)
        time.sleep(0.2)
        active[0] -= 1

    def two_at_once(*args, on_result, **kwargs):
        results = [mutations.Result(n, 2, "src/pkg/core.py", "lines 4-4", "return_constant",
                                    None, "red", "killed") for n in (1, 2)]

        def deliver(result):
            start.wait()
            on_result(result)

        threads = [threading.Thread(target=deliver, args=(r,)) for r in results]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    monkeypatch.setattr(mutations, "run_mutations", two_at_once)
    monkeypatch.setattr(Path, "mkdir", counting_mkdir)
    from curamoir.config import LOCAL_RUNNER
    from curamoir.mutations_job import run_mutations_job
    from curamoir.runner import _Unledgered

    head = _git(repo, "rev-parse", "HEAD")
    run = _Unledgered(LOCAL_RUNNER)
    run_mutations_job(repo, run, base=head, head=head, brief=str(brief(tmp_path, KILLED)),
                      emit=emit, notice=lambda text: None)

    assert run.outcome == "success" and overlaps == []
    (record,) = (repo / ".curamoir" / "state" / "r5" / "runs").iterdir()
    assert set(made) == {record} and (record / "mutations.yaml").is_file()
    lines = (record / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(line)["index"] for line in lines) == [1, 2]


def test_a_worktree_cap_above_64_is_refused_by_the_config():
    from curamoir.config import ConfigError, load_config

    with pytest.raises(ConfigError, match="max_parallel"):
        load_config({"gate": {"worktrees": {"max_parallel": 65}}})
    assert load_config({"gate": {"worktrees": {"max_parallel": 64}}})["gate"]["worktrees"] \
        == {"max_parallel": 64}
