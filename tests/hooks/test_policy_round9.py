"""Round 9: the R5 edge-case review's fifth round, and a security review.

**Options that set configuration or name a program, refused outright, whatever
the value** (C1), for the allowlisted tools not installed on the development
machine, from their documentation (versions assumed: tox 4.x, nox 2024.x,
mypy 1.x, black 24.x, flake8 7.x, eslint 8.x/9.x, prettier 3.x, tsc 5.x,
cargo 1.8x, pip 24.x, pnpm 9.x, yarn 1.x and 4.x, podman 5.x, yq 4.x
(mikefarah)):

- tox: `-x`/`--override` (sets any key, `testenv.commands` included),
  `--discover` (interpreters to run), `--runner` (a runner plugin).
- nox: `-P`/`--force-python` and `--extra-python` (interpreters),
  `-db`/`--default-venv-backend` and `-fb`/`--force-venv-backend` (the
  program that builds environments).
- mypy: `--python-executable` (an interpreter mypy runs). Not
  `--custom-typeshed-dir`: stub files are data, and its path is judged as a
  write target already.
- eslint: `--plugin`, `--rulesdir`, `--resolve-plugins-relative-to` and
  `--parser` (modules it loads), and `-f`/`--format` other than a built-in
  formatter name (a module path or package otherwise).
- prettier: `--plugin`, `--plugin-search-dir` (modules it loads).
- pip: `--python` (the interpreter pip runs as), `-C`/`--config-settings`,
  `--global-option`, `--build-option`, `--install-option` (build-backend and
  setup.py settings at call time).
- cargo: `+toolchain` (another rustc and cargo) and `-Z` (unstable flags,
  some of which load configuration).
- podman: `--runtime`, `--conmon`, `--hooks-dir`, `--network-cmd-path`,
  `--module` (programs and their configuration).
- Nothing in: black (no plugins; its config is a repository file), flake8
  (plugins must be installed; config files are repository files), tsc (no
  program options; `--plugins` is config-only), yarn (`config` is not an
  allowlisted subcommand; `--use-yarnrc` is a path, judged as one), yq (its
  expressions read and write data; nothing runs a program). tox's
  `--installpkg`/`--force-dep` and pip's package names are package installs,
  the pending decision cm-0ik, left as they are.

Also: **a value given to an option is a definite write** for the
whole-command rule (I2); **pnpm's `--config.<key>`** spellings (M); **URLs are
exempt only where a URL is documented** (S1); **`sort --co`** (S2).
"""

import pytest

from curamoir.hooks import judge_command


def denied(command, cwd=None):
    return judge_command(command, in_job=True, cwd=cwd).deny


# --- C1: configuration and program options, refused outright ----------------------

@pytest.mark.parametrize("command", [
    "tox -x testenv.commands=id", "tox --override testenv.commands=id",
    "tox --overr=testenv.commands=id", "tox -rx testenv.commands=id",
    "tox --discover /usr/bin/python3", "tox --runner virtualenv",
    "nox -P 3.12", "nox --force-python=3.12", "nox --extra-python 3.11",
    "nox -db uv", "nox --default-venv-backend=conda", "nox -fb venv",
    "mypy --python-executable python3 src",
    "eslint --plugin x src", "eslint --rulesdir rules src",
    "eslint --resolve-plugins-relative-to . src", "eslint --parser x src",
    "eslint -f ./fmt.js src", "eslint --format=eslint-formatter-x src",
    "prettier --plugin=x --check .", "prettier --plugin-search-dir . --check .",
    "pip --python python3 install -r requirements.txt",
    "pip install -C x=y .", "pip install --config-settings=x=y .",
    "pip install --global-option=x .", "pip install --build-option x .",
    "cargo +nightly build", "cargo -Zunstable-options build", "cargo build -Z x",
    "podman build --runtime x .", "podman build --hooks-dir hooks .",
])
def test_a_configuration_or_program_option_is_refused(command):
    assert denied(command), command


@pytest.mark.parametrize("command", [
    "tox -e py312", "tox -r", "nox -s tests", "mypy src", "eslint src",
    "eslint -f json src", "eslint --format=stylish src", "prettier --check .",
    "pip install -r requirements.txt", "cargo build --release", "cargo test",
    "podman build -t x .",
])
def test_ordinary_tool_options_pass(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- I2: an option's value is a definite write ----------------------------------

@pytest.mark.parametrize("command", [
    "git apply p && go build -o newlink/pre-commit .",
    "git apply p && go build -o=n/pre-commit .",
    "git apply p && go build -on/pre-commit .",
    "git apply p && pytest --junitxml n/x",
    "git apply p && pytest --junitxml=n/x",
    "git apply p && make -C newlink",
])
def test_an_option_value_beside_a_non_reader_is_a_definite_write(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and "on its own" in decision.reason, decision


@pytest.mark.parametrize("command", [
    "make build && make test", "npm ci && npm test", "uv sync && pytest tests/",
])
def test_bare_positionals_beside_a_non_reader_pass(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- M: pnpm --config.<key> ---------------------------------------------------------

@pytest.mark.parametrize("command", [
    "pnpm install --config.script-shell=./x",
    "pnpm run build --config.node-options=--require=x",
    "npm run build --config.scriptshell=x",
])
def test_a_config_dot_program_naming_option_is_refused(command):
    assert denied(command), command


# --- S1: URLs exempt only where a URL is documented ---------------------------------

def test_a_url_shaped_output_path_is_judged_as_a_path(tmp_path):
    root = tmp_path / "repo"
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / "a:").symlink_to(root / ".git" / "hooks")
    assert denied("go build -o a://x/pre-commit .", cwd=str(root))
    assert denied("go build -o a://x/pre-commit .")


@pytest.mark.parametrize("command", [
    "pip install git+https://h/r",
    "npm install git+https://x/y.git",
    "uv add git+https://h/r",
    "uv pip install git+https://h/r",
    "pip install --index-url https://pypi.example/simple x",
    "pip install --extra-index-url=https://pypi.example/simple x",
])
def test_a_url_where_one_is_documented_passes(command):
    assert not denied(command), judge_command(command, in_job=True).reason


@pytest.mark.parametrize("command", [
    "gh repo clone https://github.com/a/b",
    "cargo build --target-dir=https://x/y",
])
def test_a_url_elsewhere_is_judged_as_a_path(command):
    assert denied(command), command


# --- S2: sort --co ------------------------------------------------------------------

@pytest.mark.parametrize("command", ["sort --co=sh x", "sort --com sh x"])
def test_sort_compress_program_from_two_letters(command):
    assert denied(command)


def test_sort_check_passes():
    assert not denied("sort -c x")


# --- Addendum: bd's subcommand by arity, pytest's configuration ------------------

@pytest.mark.parametrize("command", [
    "bd --db x hooks install", "bd --json setup", "bd --db=x setup claude",
    "bd --frobnicate ready",
])
def test_bd_hooks_and_setup_are_found_past_leading_options(command):
    assert denied(command), command


@pytest.mark.parametrize("command", ["bd ready --json", "bd --json ready", "bd show x-1"])
def test_ordinary_bd_passes(command):
    assert not denied(command), judge_command(command, in_job=True).reason


@pytest.mark.parametrize("command", [
    "pytest -o addopts=-pevil",
    "pytest --override-ini=addopts=-p evil",
    "pytest --override-ini addopts=-pevil",
    "pytest -o required_plugins=x",
    "pytest -oaddopts=-pevil",
    "pytest -c other.ini",
    "pytest --config-file=other.ini",
    "python3 -m pytest -o addopts=-pevil",
])
def test_pytest_configuration_at_call_time_is_refused(command):
    assert denied(command), command


@pytest.mark.parametrize("command", [
    "pytest -o cache_dir=.cache", "pytest -o junit_family=xunit2 -q",
    "pytest -p no:cacheprovider -q",
])
def test_pytest_safe_ini_overrides_pass(command):
    assert not denied(command), judge_command(command, in_job=True).reason
