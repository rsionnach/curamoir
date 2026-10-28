"""R5 edge-case review and the security review of 986347c: where the policy
failed open, read what the shell would not run, or missed a spelling.

- **It must fail closed (C1).** A guard that raises is a guard that allows: an
  uncaught RecursionError from `uv run uv run ...` left the hook silent, and a
  silent PreToolUse hook permits the call. So judge_command refuses on any
  exception, runner nesting is bounded, and the hook entry point denies when
  judging fails inside a job.
- **package.json is untrusted input (C2).** The hook read it whole, followed
  symlinks, blocked on a FIFO and let a RecursionError out of json, which (by
  C1's route) switched the guard off for every command, `rm -rf /` included.
- **The shell rewrites words the guard reads (C3).** Brace expansion and
  `$'...'` quoting turn `--{force,quiet}` and `-$'-hard'` into words the guard
  never saw.
- **Writes outside the reviewed diff (I1).** `.git/` and paths outside the
  repository are never in the diff the sweep reviews, and a file there can run
  code later: `.git/hooks/pre-commit` runs on the next `git commit`.
- **Spellings (I2, S3, S4, S5, M7).** Abbreviated `--hard`/`--mirror`,
  `--node_options`, `yarn run -B`, linker flags naming a program, and a
  `scripts` argument of the wrong type.
"""

import io
import json
import os
import time

import pytest

from curamoir.hooks import judge_command, policy, pretooluse
from curamoir.hooks.verdict import Category


def denied(command, scripts=None):
    return judge_command(command, in_job=True, scripts=scripts).deny


# --- C1: fail closed --------------------------------------------------------

NESTING_PREFIXES = ["uv run ", "pnpm exec ", "yarn exec ", "yarn run ", "uv run -m uv run "]


@pytest.mark.parametrize("prefix", NESTING_PREFIXES)
def test_deeply_nested_runners_are_refused_not_crashed(prefix):
    decision = judge_command(prefix * 600 + "rm -rf /", in_job=True)
    assert decision.deny
    assert decision.category == Category.UNVERIFIABLE


def test_nesting_is_bounded_at_eight_runners():
    assert not denied("uv run " * 8 + "pytest")
    decision = judge_command("uv run " * 9 + "pytest", in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE
    assert "nested more than 8 deep" in decision.reason


def test_a_check_that_raises_is_a_refusal(monkeypatch):
    def broken(program, arguments, judge):
        raise ValueError("boom")

    monkeypatch.setitem(policy._PROGRAM_CHECKS, "make", (broken,))
    decision = judge_command("make test", in_job=True)
    assert decision.deny
    assert decision.category == Category.UNVERIFIABLE
    assert "ValueError" in decision.reason
    assert decision.alternative == "simplify the command"


def _main(command, cwd):
    stdout = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    assert pretooluse.main(stdin=io.StringIO(json.dumps(payload)), stdout=stdout) == 0
    out = stdout.getvalue().strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.fixture
def in_job(monkeypatch):
    monkeypatch.setenv("CURAMOIR_JOB", "sweep")
    monkeypatch.setenv("CURAMOIR_USE_DCG", "0")


def test_the_hook_denies_when_judging_fails_inside_a_job(in_job, monkeypatch, tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pretooluse, "judge_command", broken)
    decision = _main("git status", tmp_path)
    assert decision is not None and decision["permissionDecision"] == "deny"
    assert "RuntimeError" in decision["permissionDecisionReason"]


def test_an_error_before_the_job_decision_stays_silent(monkeypatch, tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pretooluse, "in_curamoir_job", broken)
    assert _main("rm -rf /", tmp_path) is None


# --- C2: package.json is untrusted input ------------------------------------

def test_a_deeply_nested_package_json_leaves_the_guard_strict(in_job, tmp_path):
    (tmp_path / "package.json").write_text("[" * 200_000, encoding="utf-8")
    yarn = _main("yarn run build", tmp_path)
    rm = _main("rm -rf /", tmp_path)
    assert yarn is not None and yarn["permissionDecision"] == "deny"
    assert rm is not None and rm["permissionDecision"] == "deny"


def _scripts_file(tmp_path, content=b'{"scripts": {"build": "tsc"}}'):
    path = tmp_path / "package.json"
    path.write_bytes(content)
    return path


def test_package_json_is_read_for_yarn(tmp_path):
    _scripts_file(tmp_path)
    assert pretooluse._package_scripts(tmp_path, "yarn run build") == frozenset({"build"})


def test_package_json_is_not_read_without_yarn(tmp_path):
    _scripts_file(tmp_path)
    assert pretooluse._package_scripts(tmp_path, "npm run build") is None


def test_a_symlinked_package_json_is_not_followed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    _scripts_file(real)
    (tmp_path / "package.json").symlink_to(real / "package.json")
    assert pretooluse._package_scripts(tmp_path, "yarn run build") is None


def test_a_fifo_package_json_returns_promptly(tmp_path):
    os.mkfifo(tmp_path / "package.json")
    started = time.monotonic()
    assert pretooluse._package_scripts(tmp_path, "yarn run build") is None
    assert time.monotonic() - started < 1


def test_an_oversize_package_json_is_not_read(tmp_path):
    padding = "x" * (pretooluse.MAX_PACKAGE_JSON_BYTES + 1)
    _scripts_file(tmp_path, json.dumps({"scripts": {"build": "tsc"}, "p": padding}).encode())
    assert pretooluse._package_scripts(tmp_path, "yarn run build") is None


@pytest.mark.parametrize("content", [
    b'\xef\xbb\xbf{"scripts": {"build": "tsc"}}',
    b'{"scripts": {"build": "\xff"}}',
    b'{"scripts": ["build"]}',
    b'["build"]',
], ids=["bom", "invalid_utf8", "scripts_not_a_dict", "not_an_object"])
def test_a_malformed_package_json_means_unknown_scripts(tmp_path, content):
    _scripts_file(tmp_path, content)
    assert pretooluse._package_scripts(tmp_path, "yarn run build") is None


# --- C3: shell expansion the guard cannot see -------------------------------

@pytest.mark.parametrize("command", [
    "git push origin main --{force,quiet}",
    "git rebase {-x,id} main",
    "make {--eval=x,all}",
    "git reset {--hard,}",
    "git status --{a..c}",
    "git reset -$'-hard'",
    'echo $"x"',
    "echo \"a\"$'b'",
])
def test_expansion_the_guard_cannot_read_is_refused(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE


@pytest.mark.parametrize("command", [
    "find . -name x -print {} ;",
    "git log --format='{a,b}'",
    'git commit -m "refactor {a, b}"',
    "echo '$'x",
    "echo \"it costs $\"",
    "jq '{a,b}' x.json",
])
def test_braces_and_dollars_that_do_not_expand_are_allowed(command):
    assert not denied(command)


def test_find_exec_braces_are_not_brace_expansion():
    # find -exec is the destructive pass's refusal, not the expansion rule's.
    decision = judge_command("find . -name x -exec cat {} ;", in_job=True)
    assert decision.category == Category.DESTRUCTIVE


# --- I1: writes outside the reviewed diff -----------------------------------

@pytest.mark.parametrize("command", [
    "echo x > .git/hooks/pre-commit",
    "echo x >> .git/config",
    "echo x &> /tmp/out",
    "echo x >| ~/out",
    "echo x 1> ../out",
    "echo x > src/../../out",
    "echo x > .GIT/hooks/pre-commit",
    "echo x > .gi?/hooks/pre-commit",
    "tee .git/hooks/pre-commit",
    "tee -a /etc/hosts",
    "cp a .git/hooks/pre-commit",
    "cp -t /tmp a b",
    "cp --target-directory=.git a",
    "mv a ../b",
    "mv .git/HEAD x",
    "touch ~/x",
    "mkdir -p .git/hooks",
    "mkdir /tmp/x",
])
def test_a_write_outside_the_reviewed_diff_is_refused(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny, command
    assert "diff" in decision.reason


@pytest.mark.parametrize("command", [
    "echo x > out.txt",
    "pytest 2>/dev/null",
    "pytest > /dev/null 2>&1",
    "cp a b",
    "mkdir -p build/x",
    "cp /usr/share/dict/words words.txt",
    "touch .gitignore",
    "tee out.log",
])
def test_a_write_inside_the_repository_is_allowed(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- I2: abbreviated destructive git options --------------------------------

@pytest.mark.parametrize("command", [
    "git reset --ha", "git reset --har HEAD~1", "git reset --hard",
    "git push --mirror", "git push --mi origin", "git push --mirr",
    "git push --de origin branch",
])
def test_abbreviated_destructive_git_options_are_denied(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.DESTRUCTIVE
    assert decision.alternative


@pytest.mark.parametrize("command", ["git reset --soft HEAD~1", "git push --dry-run"])
def test_other_reset_and_push_options_still_pass(command):
    assert not denied(command)


# --- S3: program-naming options however spelled -----------------------------

@pytest.mark.parametrize("command", [
    "npm run build --nodeoptions=--require=x",
    "npm run build --script_shell=x",
    "npm test --Script-Shell=x",
    "pnpm run build --node_OPTIONS x",
    "yarn run build --Node_Opt=x",
])
def test_program_naming_options_are_refused_in_any_spelling(command):
    assert denied(command)


# --- S4: yarn run -B runs a binary ------------------------------------------

@pytest.mark.parametrize("flag", ["-B", "--binaries-only"])
def test_yarn_run_binaries_only_judges_the_binary(flag):
    assert denied(f"yarn run {flag} build", scripts={"build"})
    assert not denied("yarn run build", scripts={"build"})


# --- S5: go linker flags that name a program --------------------------------

@pytest.mark.parametrize("command", [
    "go build -ldflags=-extld=/tmp/x",
    "go build --ldflags -extld=/tmp/x",
    "go build -ldflags '-s -extldflags=-fuse-ld=/tmp/x'",
    "go build -ldflags=all=-extld=/tmp/x",
    "go test -gccgoflags=-B/tmp",
])
def test_go_linker_flags_naming_a_program_are_refused(command):
    assert denied(command)


@pytest.mark.parametrize("command", [
    'go build -ldflags="-s -w"',
    "go build -ldflags '-X main.version=1.2 -s'",
    "go build -ldflags=-X=main.v=1",
    "go build -ldflags=all=-s",
])
def test_go_linker_flags_that_strip_or_stamp_pass(command):
    assert not denied(command)


# --- M7: `scripts` must be a collection of names -----------------------------

@pytest.mark.parametrize("scripts", ["b", ["b", 3], [["b"]], {"b": "tsc"}],
                         ids=["str", "non_str_item", "unhashable_item", "dict"])
def test_a_malformed_scripts_argument_is_treated_as_unknown(scripts):
    decision = judge_command("yarn run b", in_job=True, scripts=scripts)
    assert decision.deny
    assert "could not judge" not in decision.reason


@pytest.mark.parametrize("scripts", [{"b"}, frozenset({"b"}), ("b",), ["b"]])
def test_a_collection_of_names_is_accepted(scripts):
    assert not denied("yarn run b", scripts=scripts)
