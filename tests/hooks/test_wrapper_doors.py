"""cm-hve: inline code and command execution one step removed.

Found by the security review of the first push (2026-10-09). Each MUST_DENY
row below was allowed during a job: the inline-code refusal looked only at the
outermost program, so a one-word wrapper (`uv run`, `npm exec`, `docker run`)
defeated it; `make --eval` is inline make; `pnpm dlx` and `uv tool run` fetch
and run a package nobody reviewed; and a git setting whose value is a command
(`core.pager`, `core.sshCommand`, ...) runs that command on the next ordinary
git call, so setting one is inline code with a delay.

Not closed, by decision (2026-10-28): code the agent writes to a file and then
runs through an allowlisted runner (`pytest`, `make`, `go run`, `cargo run`, a
script). That is the same door `pytest` already is, and what bounds it is the
sweep's scope review, not this guard. MUST_ALLOW pins that the guard did not
quietly start pretending otherwise, and that ordinary work still runs.
"""

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks.verdict import Category

MUST_DENY = [
    # inline code through a wrapper runner
    pytest.param("uv run python -c 'import os'", id="uv_run_python_c"),
    pytest.param("uv run --with requests python -c 'import os'", id="uv_run_with_python_c"),
    pytest.param("uv run python3 -c 'import os'", id="uv_run_python3_c"),
    pytest.param("npm exec -- node -e 'process.exit()'", id="npm_exec_node_e"),
    pytest.param("pnpm exec node -e 'process.exit()'", id="pnpm_exec_node_e"),
    pytest.param("yarn exec node -e 'process.exit()'", id="yarn_exec_node_e"),
    pytest.param("uv run bash -c 'echo hi'", id="uv_run_bash_c"),
    pytest.param("docker run --rm alpine sh -c 'echo hi'", id="docker_run_sh_c"),
    pytest.param("podman run --rm alpine sh -c 'echo hi'", id="podman_run_sh_c"),
    pytest.param("uv run perl -e 'print 1'", id="uv_run_perl"),
    # inline make
    pytest.param("make --eval='x:;@echo hi' x", id="make_eval_equals"),
    pytest.param("make --eval 'x:;@echo hi' x", id="make_eval_space"),
    pytest.param("make -E 'x:;@echo hi' x", id="make_E"),
    # fetch and run a package
    pytest.param("pnpm dlx cowsay hi", id="pnpm_dlx"),
    pytest.param("yarn dlx cowsay hi", id="yarn_dlx"),
    pytest.param("uv tool run cowsay hi", id="uv_tool_run"),
    # a git setting whose value git runs
    pytest.param("git config core.pager 'sh -c id'", id="git_config_pager"),
    pytest.param("git config --local core.sshCommand 'sh -c id'", id="git_config_ssh_command"),
    pytest.param("git config core.hooksPath /tmp/hooks", id="git_config_hooks_path"),
    pytest.param("git config core.fsmonitor 'sh -c id'", id="git_config_fsmonitor"),
    pytest.param("git config --global core.editor 'sh -c id'", id="git_config_editor"),
    pytest.param("git config set core.pager 'sh -c id'", id="git_config_set_subcommand"),
    pytest.param("git config diff.external /tmp/x", id="git_config_diff_external"),
    pytest.param("git config filter.x.smudge 'sh -c id'", id="git_config_filter_smudge"),
    pytest.param("git config include.path /tmp/evil.cfg", id="git_config_include_path"),
    pytest.param("git -c core.pager='sh -c id' log", id="git_c_pager"),
    pytest.param("git -c core.sshCommand=x fetch", id="git_c_ssh_command"),
    pytest.param("git -ccore.pager=x log", id="git_c_attached"),
    pytest.param("git --config-env=core.pager=X log", id="git_config_env"),
    # Every `git -c` is refused, not only a command-valued key: a job has no
    # need of one, and a list of the dangerous keys is a denylist.
    pytest.param("git -c color.ui=never log --oneline", id="git_c_harmless_key"),
    # the same settings through the environment
    pytest.param("GIT_PAGER='sh -c id' git log", id="env_git_pager"),
    pytest.param("GIT_SSH_COMMAND=x git fetch", id="env_git_ssh_command"),
    pytest.param("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=x git log",
                 id="env_git_config_count"),
    pytest.param("PAGER=x git log", id="env_pager"),
    pytest.param("GIT_EXTERNAL_DIFF=x git diff", id="env_git_external_diff"),
    pytest.param("NODE_OPTIONS='--require /tmp/x.js' npm test", id="env_node_options"),
    pytest.param("BASH_ENV=/tmp/x bash script.sh", id="env_bash_env"),
    pytest.param("LD_PRELOAD=/tmp/x.so make", id="env_ld_preload"),
]

MUST_ALLOW = [
    "uv run pytest -q",
    "uv run python -m pytest -q",
    "uv run --with pytest-xdist pytest -n auto",
    "uv run ruff check .",
    "uv sync",
    "npm test",
    "npm run lint",
    "pnpm exec eslint .",
    "yarn exec tsc --noEmit",
    "docker run --rm alpine echo hi",
    "make test",
    "make -C sub test",
    "git config user.name 'A Person'",
    "git config --get core.pager",
    "git config core.autocrlf false",
    "PYTHONPATH=src python3 -m pytest -q",
    "CI=1 npm test",
    # Agent-written code run through an allowlisted runner: allowed, by
    # decision (module docstring). Scope review bounds it, not this guard.
    "go run ./cmd/tool",
    "cargo run --bin tool",
    "bash scripts/check.sh",
]


@pytest.mark.parametrize("command", MUST_DENY)
def test_inline_code_one_step_removed_is_refused(command):
    decision = judge_command(command, in_job=True)

    assert decision.deny, f"allowed: {command}"
    assert decision.category in (Category.UNVERIFIABLE, Category.NOT_ALLOWLISTED)
    assert decision.alternative, f"{command!r} refused with nothing to do instead"


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_ordinary_work_through_the_same_programs_is_allowed(command):
    decision = judge_command(command, in_job=True)

    assert not decision.deny, f"refused: {command}\n  {decision.reason}"


def test_a_wrapped_interpreter_is_refused_as_unverifiable_and_says_why():
    decision = judge_command("uv run python -c 'import os'", in_job=True)

    assert decision.category is Category.UNVERIFIABLE
    assert "python" in decision.reason


def test_a_command_valued_git_setting_names_the_key():
    decision = judge_command("git config core.sshCommand 'sh -c id'", in_job=True)

    assert decision.category is Category.UNVERIFIABLE
    assert "core.sshcommand" in decision.reason.lower()
