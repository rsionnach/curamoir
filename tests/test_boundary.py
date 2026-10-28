"""cm-3b2 (P0): prove the boundary. Nothing planted in the sweep's clone
runs, and only the patch leaves it.

The stand-in fix agent here does the real in-scope fix (the two planted
prints, tests/test_sweep_publish.PRINT_FIXES) AND plants, inside the
clone's work tree, a mechanism by which git or the runner could run code
or read config. Every plant points at a CANARY: a script outside the clone
that, if anything ever runs it, creates `<tmp>/canaries/<name>`. For each
mechanism the sweep runs end to end (real lint, real review, real gate,
real publish to a bare origin through the fake gh), and the test holds:

1. no canary exists afterwards: the runner's git never ran a plant;
2. the outcome is the one recorded here for it: the run is refused
   (`.git`, `.gitattributes`, a nested repository: git_guard's
   verify_git_state, before any git command), or the planted files are
   dropped as out of scope (cm-9ab) and the PR carries the fix (a
   case-variant `.GIT` or `.GitAttributes` is refused like its lower-case
   spelling: see `mechanisms`);
3. when a PR opens, the commit on origin changes ONLY the in-scope fix
   paths: none of the plants left the clone;
4. the user's checkout gained no file, changed none, and its `.git/config`
   and hooks are byte for byte as they were.

A positive control (test_the_canaries_are_armed) shows each hostile config
does fire its canary under a plain, unhardened git, so a canary that stays
absent in the sweep means the runner's git refused it, not that the trap
was broken.

Out of scope here, by design: the test command is the repository's own
code, run in the clone (green_gate). A planted conftest.py is dropped
before the gate (asserted below); code inside an in-scope edit is run by
the gate and DOES reach outside the clone until cm-p2f sandboxes the test
gate (the strict xfail below, which flips when it lands).
"""

import os
import subprocess
import sys

import pytest

from curamoir import green_gate
from curamoir.fix_scope import FILE_ADDED, GIT_INTERNALS_CHANGED, HIDDEN_CHANGE, WHOLE_RUN_KINDS
from curamoir.guards.reasons import Reason
from curamoir.job_run import REFUSED, SUCCESS
from tests.test_lint import NO_BARE_PRINT
from tests.test_sweep_job import (  # noqa: F401  (isolated_home is an autouse fixture)
    checkout,
    git,
    isolated_home,
)
from tests.test_sweep_publish import (  # noqa: F401  (quick_lookups is an autouse fixture)
    GATE,
    PRINT_FIXES,
    changed_on,
    creates,
    fixer,
    nothing_pushed,
    quick_lookups,
    sweep,
)
from tests.test_tier1 import BILLING, HANDLERS

#: The paths the in-scope fix changes: all a PR may carry.
FIX_PATHS = sorted([BILLING, HANDLERS])

#: Every client-side hook name git knows (githooks(5)).
HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
    "pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase",
    "post-checkout", "post-merge", "pre-push", "pre-receive", "update",
    "proc-receive", "post-receive", "post-update", "reference-transaction",
    "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate",
    "fsmonitor-watchman", "p4-changelist", "p4-prepare-changelist", "p4-post-changelist",
    "p4-pre-submit", "post-index-change")


# --- the canaries -------------------------------------------------------------------

class Canaries:
    """Scripts outside the clone, each creating `dir/<name>` when run."""

    def __init__(self, tmp_path):
        self.bin = tmp_path / "canary-bin"
        self.dir = tmp_path / "canaries"
        self.bin.mkdir()
        self.dir.mkdir()

    def script(self, name):
        """The absolute path of an executable that fires canary `name`."""
        path = self.bin / name
        if not path.exists():
            path.write_text(f"#!/bin/sh\ntouch '{self.dir / name}'\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        return str(path)

    def fired(self):
        return sorted(entry.name for entry in self.dir.iterdir())


def hostile_config(canaries, tag, include=None):
    """A git config naming a canary in every key that makes git run a
    program; `include` a path that `include.path` names."""
    c = lambda key: canaries.script(f"{tag}-{key}")  # noqa: E731
    text = (
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tbare = false\n"
        f"\tfsmonitor = {c('fsmonitor')}\n"
        f"\thooksPath = {canaries.bin / (tag + '-hooks')}\n"
        f"\tpager = {c('pager')}\n"
        f"\tsshCommand = {c('sshCommand')}\n"
        f"\teditor = {c('editor')}\n"
        f"\taskPass = {c('askPass')}\n"
        "[filter \"canary\"]\n"
        f"\tclean = {c('filter-clean')}\n"
        f"\tsmudge = {c('filter-smudge')}\n"
        f"\tprocess = {c('filter-process')}\n"
        "\trequired = true\n"
        "[diff \"canary\"]\n"
        f"\ttextconv = {c('diff-textconv')}\n"
        f"\tcommand = {c('diff-command')}\n"
        "[diff]\n"
        f"\texternal = {c('diff-external')}\n"
        "[merge \"canary\"]\n"
        f"\tdriver = {c('merge-driver')}\n"
        "[alias]\n"
        f"\tdiff = !{c('alias-diff')}\n"
        f"\tstatus = !{c('alias-status')}\n"
        f"\tls-files = !{c('alias-ls-files')}\n"
        f"\tcat-file = !{c('alias-cat-file')}\n"
        "[gpg]\n"
        f"\tprogram = {c('gpg')}\n"
        "[credential]\n"
        f"\thelper = !{c('credential')}\n")
    hooks = canaries.bin / f"{tag}-hooks"
    hooks.mkdir(exist_ok=True)
    for hook in HOOK_NAMES:
        target = hooks / hook
        target.write_text(f"#!/bin/sh\ntouch '{canaries.dir / (tag + '-hook-' + hook)}'\n",
                          encoding="utf-8")
        target.chmod(0o755)
    if include:
        text += f"[include]\n\tpath = {include}\n"
    return text


def attributes():
    return ("* filter=canary diff=canary merge=canary\n"
            "*.py filter=canary diff=canary merge=canary\n")


def repository_skeleton(canaries, tag, root):
    """{relative path: text} of a minimal non-bare git dir at `root`, whose
    config (and the file it includes) is hostile."""
    return {
        f"{root}/HEAD": "ref: refs/heads/main\n",
        f"{root}/config": hostile_config(canaries, tag, include="included.cfg"),
        f"{root}/included.cfg": hostile_config(canaries, f"{tag}-inc"),
        f"{root}/objects/": None,
        f"{root}/refs/heads/": None,
        f"{root}/hooks/post-checkout": "#!/bin/sh\ntouch "
                                       f"'{canaries.dir / (tag + '-own-hook')}'\n",
    }


# --- the mechanisms -----------------------------------------------------------------

#: Outcomes, (what happened, the breach kind it happened by): the whole run
#: refused before anything is pushed, or the plants dropped (cm-9ab) and a
#: PR opened with the fix alone.
REFUSED_RUN, DROPPED = "refused", "dropped"
REFUSED_INTERNALS = (REFUSED_RUN, GIT_INTERNALS_CHANGED)
DROPPED_ADDED = (DROPPED, FILE_ADDED)


def mechanisms(canaries, case_insensitive):
    """{name: (plants, git_commands, expected outcome)}. A plant's value is
    file text, or None for a directory. `git_commands` are run by the stand-in
    agent itself, in the clone, after planting (the agent's own act, which
    the review must then refuse)."""
    found = {
        # Attributes naming drivers: without the config that defines them,
        # and with no config the clone can read naming them, inert; held to
        # the snapshot anyway, so the run is refused before git reads them.
        "gitattributes-drivers": ({".gitattributes": attributes()}, [], REFUSED_INTERNALS),
        "gitattributes-nested": ({"src/sample/.gitattributes": attributes()}, [],
                                 REFUSED_INTERNALS),
        # The harmless control: still a changed `.gitattributes`.
        "gitattributes-eol-ident": ({".gitattributes": "* text eol=crlf ident\n"}, [],
                                    REFUSED_INTERNALS),
        # A nested repository (a `.git` directory) with every code-running
        # key, an include.path, and its own hooks, plus attributes that
        # name its drivers.
        "nested-git-dir": ({**repository_skeleton(canaries, "nested", "sub/.git"),
                            "sub/module.py": "x = 1\n",
                            "sub/.gitattributes": attributes()}, [], REFUSED_INTERNALS),
        # `sub/.git` a FILE pointing at a planted git dir elsewhere in the tree.
        "gitfile": ({"sub/.git": "gitdir: ../planted-gitdir\n",
                     "sub/module.py": "x = 1\n",
                     **repository_skeleton(canaries, "gitfile", "planted-gitdir")}, [],
                    REFUSED_INTERNALS),
        # `.gitmodules` alone: a plain file git reads only for submodule
        # commands, none of which the runner runs.
        "gitmodules": ({".gitmodules": (
            "[submodule \"x\"]\n\tpath = sub\n\turl = ./sub\n"
            f"\tupdate = !{canaries.script('gitmodules-update')}\n")}, [], DROPPED_ADDED),
        # ...with a gitlink in the index (the agent's own git call): the
        # index is git's own file, so the run is refused.
        "gitmodules-gitlink": ({".gitmodules": (
            "[submodule \"x\"]\n\tpath = sub\n\turl = ./sub\n"
            f"\tupdate = !{canaries.script('gitlink-update')}\n")},
            [["update-index", "--add", "--cacheinfo",
              "160000,1111111111111111111111111111111111111111,sub"]], REFUSED_INTERNALS),
        # A `.githooks/` directory with every hook: nothing names it.
        "githooks-dir": ({f".githooks/{hook}": (
            f"#!/bin/sh\ntouch '{canaries.dir / ('githooks-' + hook)}'\n")
            for hook in HOOK_NAMES}, [], DROPPED_ADDED),
        # The clone's own `.git`: hooks and config the agent may not touch.
        "clone-git-hooks": ({f".git/hooks/{hook}": (
            f"#!/bin/sh\ntouch '{canaries.dir / ('clone-hook-' + hook)}'\n")
            for hook in HOOK_NAMES}, [], REFUSED_INTERNALS),
        "clone-git-config": ({".git/config": None}, [], REFUSED_INTERNALS),
        # Files other tools read (direnv, the editor): inert to git.
        "envrc": ({".envrc": f"{canaries.script('envrc')}\n",
                   ".mailmap": "A <a@example.invalid> <b@example.invalid>\n"}, [], DROPPED_ADDED),
    }
    if case_insensitive:
        # On a case-insensitive file system `sub/.GIT` IS `sub/.git` to
        # git, and `.GitAttributes` is read as `.gitattributes`.
        # verify_git_state compares those names case-insensitively (found by
        # this module, cm-3b2), so both are refused before any git command
        # runs, as their lower-case spellings are.
        found["nested-git-dir-case"] = (
            {**repository_skeleton(canaries, "nestedcase", "sub/.GIT"),
             "sub/module.py": "x = 1\n"}, [], REFUSED_INTERNALS)
        found["gitattributes-case"] = ({"src/.GitAttributes": attributes()}, [],
                                       REFUSED_INTERNALS)
    return found


def case_insensitive(directory):
    probe = directory / "CaseProbe"
    probe.write_text("", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def planting_fixer(tmp_path, canaries, plants, git_commands=()):
    """A stand-in `claude` that plants `plants` (and appends the clone's own
    hostile config to `.git/config` for a `.git/config: None` plant), runs
    `git_commands` in the clone, then makes the real fix."""
    fix, _ = fixer(tmp_path)
    clone_config = hostile_config(canaries, "clone-config")
    script = tmp_path / "bin" / "plant"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        f"for path, text in {plants!r}.items():\n"
        "    if text is None and path == '.git/config':\n"
        f"        open(path, 'a', encoding='utf-8').write({clone_config!r})\n"
        "        continue\n"
        "    if text is None:\n"
        "        os.makedirs(path, exist_ok=True)\n"
        "        continue\n"
        "    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)\n"
        "    open(path, 'w', encoding='utf-8').write(text)\n"
        "    if text.startswith('#!'):\n"
        "        os.chmod(path, 0o755)\n"
        "env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}\n"
        f"for command in {list(git_commands)!r}:\n"
        "    subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *command],\n"
        "                   check=True, env=env)\n"
        f"os.execv({fix!r}, [{fix!r}, *sys.argv[1:]])\n",
        encoding="utf-8")
    script.chmod(0o755)
    return str(script)


# --- the user's checkout ------------------------------------------------------------

def checkout_state(repo):
    """What the user's checkout looks like: its status (ignored files too,
    the runner's own state aside), its config's bytes, its hooks."""
    git_dir = repo / ".git"
    return {
        "status": git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all",
                      "--", ":!.curamoir/state"),
        "config": (git_dir / "config").read_bytes(),
        "hooks": sorted(p.name for p in (git_dir / "hooks").iterdir()),
        "info": sorted(str(p.relative_to(git_dir)) for p in (git_dir / "info").rglob("*"))
        if (git_dir / "info").exists() else [],
        "head": git(repo, "rev-parse", "HEAD"),
    }


# --- the tests ----------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "gitattributes-drivers", "gitattributes-nested", "gitattributes-eol-ident",
    "nested-git-dir", "gitfile", "gitmodules", "gitmodules-gitlink", "githooks-dir",
    "clone-git-hooks", "clone-git-config", "envrc", "nested-git-dir-case",
    "gitattributes-case"])
def test_nothing_planted_in_the_clone_runs_and_only_the_patch_leaves(tmp_path, no_real_gh,
                                                                     name):
    canaries = Canaries(tmp_path)
    found = mechanisms(canaries, case_insensitive(tmp_path))
    if name not in found:
        pytest.skip("the file system here is case-sensitive: no case variant applies")
    plants, git_commands, expected = found[name]
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    before = checkout_state(repo)
    claude = planting_fixer(tmp_path, canaries, plants, git_commands)

    line = sweep(repo, claude=claude)

    # (1) The runner's git ran no plant.
    assert canaries.fired() == [], (name, line)
    # (2) The outcome recorded for this mechanism, and the breach it came by.
    outcome, kind = expected
    if outcome == REFUSED_RUN:
        assert line["outcome"] == REFUSED, line["message"]
        assert line["reason"] == Reason.SCOPE.value, line
        assert kind in {breach["kind"] for breach in line["scope"]["breaches"]}, line["scope"]
        if kind in WHOLE_RUN_KINDS and kind != HIDDEN_CHANGE:
            # The whole run: nothing was dropped, so nothing was read.
            assert "scope_dropped" not in line
        assert "pr" not in line and "fixes" not in line
        assert nothing_pushed(tmp_path, no_real_gh)
    else:
        assert line["outcome"] == SUCCESS, line["message"]
        planted = {path.rstrip("/") for path in plants}
        dropped = {breach["path"] for breach in line["scope_dropped"]}
        assert planted <= dropped, (planted, dropped)
        assert {breach["kind"] for breach in line["scope_dropped"]} == {kind}
        # (3) Only the patch left the clone: the commit on origin changes
        # the fix's paths and nothing planted.
        [create] = creates(no_real_gh)
        assert create["argv"][create["argv"].index("--head") + 1] == line["pr"]["branch"]
        assert line["pr"]["paths"] == FIX_PATHS
        assert sorted(changed_on(tmp_path, line["pr"]["branch"], line["pr"]["base"])) == \
            FIX_PATHS
        tree = git(tmp_path / "origin.git", "ls-tree", "-r", "--name-only",
                   line["pr"]["branch"]).split()
        assert not planted & set(tree)
    # (4) The user's checkout: no new or changed file, config or hook.
    assert checkout_state(repo) == before
    assert canaries.fired() == []


def test_the_canaries_are_armed(tmp_path):
    """The positive control: a plain git, run in a planted nested repository
    (as a git that descended into it would be), fires the canaries. So a
    canary absent after a sweep was refused, not disarmed."""
    canaries = Canaries(tmp_path)
    sub = tmp_path / "sub"
    for path, text in repository_skeleton(canaries, "armed", ".git").items():
        target = sub / path
        if text is None:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (sub / ".gitattributes").write_text(attributes(), encoding="utf-8")
    (sub / "module.py").write_text("x = 1\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for command in (["add", "module.py"], ["status"]):
        subprocess.run(["git", *command], cwd=sub, env=env, capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    fired = canaries.fired()
    assert "armed-fsmonitor" in fired or "armed-inc-fsmonitor" in fired, fired
    assert "armed-filter-clean" in fired or "armed-filter-process" in fired or \
        "armed-inc-filter-process" in fired, fired


def test_a_planted_conftest_is_dropped_before_the_test_gate_runs_it(tmp_path, no_real_gh):
    """A conftest.py the agent adds is out of scope (file_added), so it is
    put back (deleted) before the gate runs the repository's tests: it never
    runs, and the PR carries the fix alone."""
    canaries = Canaries(tmp_path)
    gate = GATE.replace("\"! grep -rq BREAKS src\"",
                        "\"PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests\"")
    repo = checkout(tmp_path, NO_BARE_PRINT, config=gate)
    before = checkout_state(repo)
    plants = {"conftest.py": f"import os\nos.system({canaries.script('conftest')!r})\n",
              "tests/conftest.py":
                  f"import os\nos.system({canaries.script('tests-conftest')!r})\n"}
    claude = planting_fixer(tmp_path, canaries, plants)

    line = sweep(repo, claude=claude)

    assert canaries.fired() == []
    assert line["outcome"] == SUCCESS, line["message"]
    assert {b["path"] for b in line["scope_dropped"]} == set(plants)
    assert line["pr"]["paths"] == FIX_PATHS
    assert sorted(changed_on(tmp_path, line["pr"]["branch"], line["pr"]["base"])) == FIX_PATHS
    assert checkout_state(repo) == before


@pytest.mark.xfail(not hasattr(green_gate, "OS_SANDBOX"),
                   reason="cm-p2f: the test gate is not OS-sandboxed yet", strict=True,
                   raises=AssertionError)
def test_code_the_test_gate_runs_cannot_reach_outside_the_clone(tmp_path, no_real_gh):
    """The test command is the repository's code, run in the clone, and an
    in-scope edit is code it runs: today it can write outside the clone (a
    canary fires). Strict: once cm-p2f's OS sandbox is in green_gate
    (`green_gate.OS_SANDBOX`) the xfail no longer applies and this must
    pass; before it, a pass would fail the suite as XPASS."""
    canaries = Canaries(tmp_path)
    gate = GATE.replace("\"! grep -rq BREAKS src\"",
                        "\"PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests\"")
    repo = checkout(tmp_path, NO_BARE_PRINT, config=gate)
    old, new = PRINT_FIXES[0][1:]
    reach = f"{new}; open({str(canaries.dir / 'gate')!r}, 'w').close()"
    claude, _ = fixer(tmp_path, edits=[(BILLING, old, reach), PRINT_FIXES[1]])

    line = sweep(repo, claude=claude)

    # The edit reached the gate (not a RuntimeError the xfail would hide:
    # it expects only the AssertionError below).
    if "test_gate" not in line:
        raise RuntimeError(f"the run stopped before the test gate: {line['message']}")
    assert canaries.fired() == []
