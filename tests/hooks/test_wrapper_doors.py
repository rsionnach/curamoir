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
    # cm-hhr: npm exec fetches and runs a package that is not installed,
    # unless told `--no` (user decision 2026-10-28)
    pytest.param("npm exec tsc", id="hhr_npm_exec_without_no"),
    pytest.param("npm x tsc", id="hhr_npm_x_without_no"),
    pytest.param("npm exec --yes cowsay", id="hhr_npm_exec_yes"),
    pytest.param("npm exec --no --yes cowsay", id="hhr_npm_exec_no_and_yes"),
    pytest.param("npm exec tsc --no", id="hhr_npm_exec_no_after_program"),
    pytest.param("npm exec --no cowsay", id="hhr_npm_exec_no_unlisted_program"),
    # cm-z0k: the security review's probes of the first version, each of
    # which got through it
    pytest.param("git config core.pager 'sh -c id' --get", id="z_git_config_get_after"),
    pytest.param("git config --rename-section foo core", id="z_git_config_rename_section"),
    pytest.param("git config foo.pager x", id="z_git_config_unlisted_key"),
    pytest.param("git config --add core.pager x", id="z_git_config_add"),
    pytest.param("git config --edit", id="z_git_config_edit"),
    pytest.param("make --ev='x:;@id' x", id="z_make_eval_prefix"),
    pytest.param("make -sE 'x:;@id' x", id="z_make_cluster_E"),
    pytest.param("make SHELL='sh -c id'", id="z_make_variable"),
    pytest.param("pnpm --dir . dlx cowsay", id="z_pnpm_option_before_dlx"),
    pytest.param("npm init vite", id="z_npm_init_package"),
    pytest.param("npm create vite", id="z_npm_create_package"),
    pytest.param("pnpm create vite", id="z_pnpm_create"),
    pytest.param("yarn create vite", id="z_yarn_create"),
    pytest.param("uv --directory . tool run cowsay", id="z_uv_option_before_tool_run"),
    pytest.param("uv run --with cowsay cowsay", id="z_uv_run_with"),
    pytest.param("uv run --with=cowsay cowsay", id="z_uv_run_with_equals"),
    pytest.param("uv run cowsay", id="z_uv_run_unlisted_program"),
    pytest.param("pnpm exec cowsay", id="z_pnpm_exec_unlisted_program"),
    pytest.param("docker run --entrypoint=sh alpine -c id", id="z_docker_entrypoint"),
    pytest.param("docker run --rm alpine echo hi", id="z_docker_run"),
    pytest.param("podman run --rm alpine echo hi", id="z_podman_run"),
    pytest.param("docker exec box sh", id="z_docker_exec"),
    pytest.param("GIT_TRACE2_EVENT=/tmp/x git log", id="z_env_unlisted"),
    pytest.param("HOME=/tmp/x git log", id="z_env_home"),
    pytest.param("GOFLAGS=-toolexec=x go build", id="z_env_goflags"),
    pytest.param("go build -toolexec='sh -c id' ./...", id="z_go_toolexec_equals"),
    pytest.param("go build -toolexec 'sh -c id' ./...", id="z_go_toolexec_space"),
    pytest.param("go test --toolexec=x ./...", id="z_go_toolexec_double_dash"),
    pytest.param("go test -exec 'sh -c id' ./...", id="z_go_test_exec"),
    pytest.param("cargo --config 'target.x.runner=\"sh\"' run", id="z_cargo_config"),
    pytest.param("cargo run --config=x", id="z_cargo_config_equals"),

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
    "uv run ruff check .",
    "uv sync",
    "npm test",
    "npm run lint",
    "pnpm exec eslint .",
    "yarn exec tsc --noEmit",
    "docker build -t app .",
    "docker ps",
    "git config --get core.pager",
    "git config --list",
    "git config user.email a@b.c",
    "git config set user.name x",
    "git config --unset user.name",
    "git config color.ui never",
    "make -j4 test",
    "make -s test",
    "go test ./...",
    "go vet ./...",
    "cargo test --all",
    "pnpm --dir web exec eslint .",
    "NO_COLOR=1 CI=true pytest -q",
    "npm init -y",
    "npm exec --no tsc",
    "npm exec --no -- eslint .",
    "npm x --no prettier --check .",
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


def test_npm_exec_without_no_names_the_flag_to_add():
    decision = judge_command("npm exec tsc", in_job=True)

    assert "--no" in decision.reason and "--no" in decision.alternative
