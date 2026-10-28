"""Round 8: the R5 edge-case review's fourth round, and the security review of
458695f.

- **`//x` is absolute** (C1). The `//` exemption meant for URLs let
  `go build -o //tmp/x` through; now only the part straight after a
  `scheme:` is exempt, and leading slashes are collapsed before judging.
- **Options that run a program** (I2), found by reading each allowlisted
  program's `--help` and man page on the development machine: `rg --pre`,
  `rg --hostname-bin`, `sort --compress-program`, `pytest -p`,
  `uv run --env-file`, `bd hooks`/`bd setup`.
- **Writes in a command line that also runs a non-reader** (I3): any
  program that is not a pure reader may change the tree, before or while a
  write in the same line runs (`&`, `|` run concurrently), so every definite
  write target in such a line is refused.
"""

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks.verdict import Category


def denied(command, cwd=None):
    return judge_command(command, in_job=True, cwd=cwd).deny


# --- C1: `//x` is absolute ------------------------------------------------------

@pytest.mark.parametrize("command", [
    "go build -o //tmp/x .",
    "go build -o=//tmp/x .",
    "go build -o//tmp/x .",
    "pip install --target=//tmp/x y",
    "pip install --target //tmp/x y",
    "cargo build --target-dir //tmp/t",
    "cargo build --target-dir=//tmp/t",
    "uv venv //tmp/v",
    "pip install --target=a://../../x y",
    "echo x > //tmp/x",
    "tee //tmp/x",
])
def test_a_double_slash_path_is_absolute(command):
    assert denied(command), command


def test_a_double_slash_path_into_the_repository_git_dir_is_refused(tmp_path):
    root = tmp_path / "repo"
    (root / ".git" / "hooks").mkdir(parents=True)
    assert denied(f"go build -o /{root}/.git/hooks/pre-commit .", cwd=str(root))


@pytest.mark.parametrize("command", [
    "npm install git+https://x/y.git",
    "pip install git+https://h/r",
    "pip install --index-url=https://pypi.example/simple x",
    "pip install x@git+ssh://git@h/r.git",
])
def test_a_url_after_its_scheme_passes(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- I2: options that run a program ----------------------------------------------

@pytest.mark.parametrize("command", [
    "rg --pre=cat x",
    "rg --pre cat x",
    "rg --hostname-bin=/bin/hostname x",
    "rg --hostname-b id x",
    "sort --compress-program=gzip a",
    "sort --compress-program gzip a",
    "sort --compress=gzip a",
    "pytest -p evil",
    "pytest -pevil",
    "python3 -m pytest -p evil",
    "uv run --env-file .env pytest",
    "bd hooks install",
    "bd setup claude",
])
def test_an_option_that_runs_a_program_is_refused(command):
    assert denied(command), command


@pytest.mark.parametrize("command", [
    "rg --pretty x", "rg --pre-glob '*.pdf' x", "sort -k1 a",
    "pytest -p no:cacheprovider", "python3 -m pytest -q -p no:cacheprovider",
    "bd ready", "rg -z x",
])
def test_reading_options_pass(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- I3: writes in a line that also runs a non-reader ----------------------------

@pytest.mark.parametrize("command", [
    "mv a b && echo x > b/hk/pre-commit",
    "cp -R a b; echo x > b/x",
    "uv run git apply p && echo x > n/x",
    "npm install && echo x > node_modules/x/y",
    "git status && echo x > out.txt",
    "echo x > n/x | git apply p",
    "echo x > n/x & npm install",
    "echo x > out.txt; git status",
    "pytest -q 2>&1 | tee out.log",
    "mkdir -p build && touch build/x",
])
def test_a_write_beside_a_non_reader_is_refused(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE, decision
    assert "on its own" in decision.reason


@pytest.mark.parametrize("command", [
    "echo x > out.txt",
    "cat a | grep b > out.txt",
    "cat a && echo x > out.txt",
    "pytest -q > out.log 2>&1",
    "git add -A && git commit -m x",
    "npm ci && npm test",
    "uv sync && pytest tests/",
    "make build && make test",
])
def test_no_write_beside_a_non_reader_passes(command):
    assert not denied(command), judge_command(command, in_job=True).reason
