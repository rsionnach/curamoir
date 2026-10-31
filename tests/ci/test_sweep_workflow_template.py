"""cm-2qp.4 (E14.4): templates/github/curamoir-sweep.yml, the scheduled
GitHub Actions sweep (docs/direction-platform-first.md §5 R5).

What the template must say, read as YAML the way GitHub reads it (PyYAML
parses `on:` as the boolean True, a YAML 1.1 quirk, so the triggers are
looked up under both). The permissions are checked by E0-R8's own
checker (curamoir.ci.permissions), so the template cannot widen scope
past the guardrail.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from curamoir.ci.permissions import check_workflow_permissions

DOC = "https://github.com/rsionnach/curamoir/blob/HEAD/docs/direction-platform-first.md"
TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "github" / "curamoir-sweep.yml"


@pytest.fixture(scope="module")
def text():
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(text):
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def steps(workflow):
    [job] = workflow["jobs"].values()
    return job["steps"]


def step_running(steps, command):
    [step] = [step for step in steps if command in step.get("run", "")]
    return step


def header(text):
    """The leading comment block, before the first YAML key."""
    return text.split("\nname:", 1)[0]


def test_the_template_parses_with_schedule_and_manual_triggers(workflow):
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    [schedule] = triggers["schedule"]
    # Weekly: one weekday, every week.
    _minute, _hour, day, month, weekday = schedule["cron"].split()
    assert (day, month) == ("*", "*") and weekday.isdigit()


def test_the_cron_comment_points_at_the_cadence_and_the_hourly_minimum(text):
    assert "cadence.sweep" in text and ".curamoir/curamoir.yaml" in text
    assert re.search(r"hourly", text) and "one\n    # hour" in text


def test_it_runs_on_github_hosted_ubuntu_in_one_non_overlapping_job(workflow, text):
    [job] = workflow["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["concurrency"]["group"]
    assert "self-hosted runner or `act` is\n# refused with reason `runner`" in header(text)


def test_the_permissions_pass_the_e0_r8_checker_and_are_least_privilege(workflow, text):
    assert check_workflow_permissions(text) == []
    # The sweep files no issues on GitHub, so `issues` is not asked for.
    assert workflow["permissions"] == {"contents": "write", "pull-requests": "write"}


def test_checkout_has_full_history_and_persists_no_credentials(steps):
    [checkout] = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}


def test_every_action_is_pinned_to_a_full_commit_sha(steps, text):
    """GitHub's own `actions/*` included: a full 40-character SHA with its
    release in a comment (security review; excellence review)."""
    used = [step["uses"] for step in steps if "uses" in step]
    assert any(use.startswith("actions/checkout@") for use in used)
    for use in used:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use), use
        assert re.search(re.escape(use) + r" # v\d+\.\d+\.\d+", text), use


def test_the_tools_the_sweep_needs_are_installed(steps):
    assert any(step.get("uses", "").startswith("astral-sh/setup-uv@") for step in steps)
    installs = step_running(steps, "install.sh")["run"]
    # Pinned: the documented installer takes the version as its argument.
    assert 'curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"' in (
        installs)
    assert 'uv tool install "ast-grep-cli==${AST_GREP_VERSION}"' in installs


def test_git_has_the_bot_identity_and_gh_as_its_credential_helper(steps):
    setup = step_running(steps, "gh auth setup-git")
    assert setup["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert 'git config user.email "41898282+github-actions[bot]' in setup["run"]


def test_the_sweep_step_declares_hosted_and_runs_curamoir_run_sweep(steps):
    sweep = step_running(steps, "curamoir run sweep")
    assert sweep["env"]["CURAMOIR_RUNNER"] == "hosted"
    assert sweep["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert sweep["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert 'uvx --from "git+https://github.com/rsionnach/curamoir@${CURAMOIR_SHA}"' in (
        sweep["run"])
    # A refusal (EXIT_REFUSED, 3) keeps the job green but is annotated; any
    # other non-zero exit fails it.
    assert "3) echo \"::warning" in sweep["run"] and '*) exit "$status"' in sweep["run"]
    assert "if" not in sweep


def test_the_comment_says_a_runner_refusal_is_green_with_a_warning(text):
    assert "reason `runner`" in text.split("- name: curamoir run sweep")[0].rsplit(
        "# The sweep.", 1)[1]
    assert "read the warning" in text


@pytest.mark.parametrize("setting", ["CLAUDE_CODE_VERSION", "AST_GREP_VERSION"])
def test_each_tool_version_is_one_pinned_setting(workflow, setting):
    assert re.fullmatch(r"\d+\.\d+\.\d+", workflow["env"][setting])


def test_a_branch_ref_is_documented_as_accepted_but_discouraged(text):
    assert "branch name is accepted but discouraged" in header(text)
    assert "A branch is accepted but\n  # discouraged" in text


def test_the_ref_is_one_required_setting_with_no_default(workflow, text):
    """No default: a ref that does not exist (`main` on rsionnach/curamoir
    today) would fail every run before its ledger line."""
    assert workflow["env"]["CURAMOIR_REF"] == "${{ vars.CURAMOIR_REF }}"
    assert "pin a tag or commit" in text
    assert "required: a tag or commit SHA" in header(text)


def test_an_unset_ref_fails_the_job_first_with_a_clear_error(steps):
    first = steps[0]
    assert first["if"] == "env.CURAMOIR_REF == ''"
    assert "::error" in first["run"] and "set the curamoir_ref repository variable" in (
        first["run"].lower())
    assert "tag or commit" in first["run"]
    assert first["run"].rstrip().endswith("exit 1")


def test_the_state_step_always_runs_after_the_sweep(steps):
    sweep = step_running(steps, "curamoir run sweep")
    state = step_running(steps, "curamoir state push")
    assert steps.index(state) > steps.index(sweep)
    # Always, except when there is no cúramóir to run it with (the first
    # step has then failed the job already).
    assert state["if"] == "always() && env.CURAMOIR_SHA != ''"
    assert '--branch "${CURAMOIR_STATE_BRANCH}"' in state["run"]
    # Exactly the push token: no API key and no runner claim reach it.
    assert state["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_the_header_names_the_api_key_and_claims_nothing_for_the_oauth_token(text):
    top = header(text)
    assert "ANTHROPIC_API_KEY" in top and "per token" in top
    assert "open question" in top
    assert DOC in top
    assert "is NOT wired" in top
    lowered = top.lower()
    for claim in ("token is permitted", "terms permit unattended use of it.",
                  "allowed for unattended", "supported for unattended"):
        assert claim not in lowered
    # Nothing in the workflow uses it.
    assert "CLAUDE_CODE_OAUTH_TOKEN }}" not in text and "secrets.CLAUDE_CODE_OAUTH" not in text


def test_the_header_states_the_hosted_exposure(text):
    top = header(text)
    assert "GITHUB_TOKEN" in top and "readable by them" in top
    assert DOC in top


def test_the_template_cites_no_internal_ids_or_module_names(text):
    """It is copied into other repositories, where bead and requirement ids,
    cúramóir's module names and relative doc paths mean nothing."""
    cited = text.replace(DOC, "").replace(".curamoir/curamoir.yaml", "")
    assert not re.search(r"cm-[0-9a-z]+|E\d+(\.\d+|-R\d+)|curamoir\.[a-z]|§|(?<![./])docs/", cited)
    assert "beads" not in text


def test_the_exposure_comment_says_to_add_no_other_secrets(text):
    """The fix agent's process drops only the run's GitHub and Actions
    tokens, by name, so the template tells the user not to hand the sweep
    step any other secret."""
    comment = " ".join(line.lstrip("# ") for line in text.split("\n")
                       if line.startswith("#"))
    assert "Add no other secrets to the sweep step's `env:`" in comment
    assert "reaches it" in comment


def test_the_cron_comment_leads_with_cost(text):
    comment = text.split("  schedule:\n", 1)[1].split("- cron:", 1)[0]
    assert comment.index("API money") < comment.index("Routine")


def test_every_run_step_uses_bash_with_pipefail(steps):
    """`shell: bash` is `bash -eo pipefail` on GitHub; the default shell has
    no pipefail, so a failed `curl | bash` would pass its step."""
    for step in steps:
        if "run" in step:
            assert step.get("shell") == "bash", step.get("name")


# --- edge cases review ----------------------------------------------------------------

def test_the_uv_cache_is_off_so_no_run_executes_what_an_earlier_run_left(steps):
    """setup-uv's default (`auto`) saves the uv cache on GitHub-hosted
    runners and restores it next run under a key that rarely changes; the
    repository's tests and the fix agent run in the same VM and could write
    into it, and a later run would execute that cúramóir. Off, every run
    starts from a fresh VM."""
    [setup] = [step for step in steps if step.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert setup["with"] == {"enable-cache": False}


def test_the_ref_is_resolved_once_to_a_commit_and_both_steps_run_that_commit(steps):
    """A tag moved (or a branch pushed) between the sweep and the state step
    must not make them run two different cúramóirs."""
    resolve = step_running(steps, "git ls-remote https://github.com/rsionnach/curamoir")
    sweep = step_running(steps, "curamoir run sweep")
    state = step_running(steps, "curamoir state push")
    assert steps.index(resolve) < steps.index(sweep) < steps.index(state)
    script = resolve["run"]
    # A full SHA is taken as it is; anything else must resolve, or the job is red.
    assert "^[0-9a-f]{40}$" in script
    assert "::error" in script and "exit 1" in script
    assert 'CURAMOIR_SHA=${sha}" >> "$GITHUB_ENV"' in script
    # A peeled annotated tag names the commit, not the tag object.
    assert "^{}" in script
    for step in (sweep, state):
        assert "${CURAMOIR_SHA}" in step["run"] and "${CURAMOIR_REF}" not in step["run"]


def test_the_concurrency_comment_says_what_github_does_with_a_third_run(text):
    comment = text.split("\nconcurrency:", 1)[0].rsplit("\n\n", 1)[1]
    assert "a second run waits" in comment and "a third replaces the waiting one" in comment


def test_the_header_says_a_ruleset_must_let_the_bot_push_the_state_branch(text):
    top = header(text)
    assert "ruleset" in top and "github-actions[bot]" in top


def test_the_timeout_is_on_the_sweep_step_not_the_job(workflow, steps):
    """On the job, a sweep that hit it would cancel the state step too, and
    the run's ledger lines would go with the VM."""
    [job] = workflow["jobs"].values()
    assert "timeout-minutes" not in job
    sweep = step_running(steps, "curamoir run sweep")
    assert isinstance(sweep["timeout-minutes"], int) and sweep["timeout-minutes"] > 0
    assert [step for step in steps if "timeout-minutes" in step] == [sweep]


# --- the steps' bash, run (provenance review) -------------------------------------------
#
# Each `run:` body is taken from the parsed YAML and run the way GitHub
# documents `shell: bash`: `bash --noprofile --norc -eo pipefail {0}`, with
# stub `git` and `uvx` first on PATH and a temporary GITHUB_ENV file. The
# resolve step's stub git prints what a REAL `git ls-remote` printed for a
# repository built here, never hand-written output.

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not installed, so the "
                                                     "workflow's run steps cannot be run")


def _real_git(cwd, *args):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, env={**os.environ, "GIT_AUTHOR_NAME": "a",
                                           "GIT_AUTHOR_EMAIL": "a@x",
                                           "GIT_COMMITTER_NAME": "a",
                                           "GIT_COMMITTER_EMAIL": "a@x"})
    return done.stdout.strip()


@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    """A repository with a branch, a lightweight tag and an annotated tag,
    each on its own commit, and the real `git ls-remote` of it."""
    repo = tmp_path_factory.mktemp("upstream")
    _real_git(repo, "init", "--quiet", "--initial-branch=main")
    commits = {}
    for name in ("main", "release", "light", "annotated"):
        _real_git(repo, "commit", "--quiet", "--allow-empty", "-m", name)
        commits[name] = _real_git(repo, "rev-parse", "HEAD")
        if name == "release":
            _real_git(repo, "branch", "release")
        elif name == "light":
            _real_git(repo, "tag", "light")
        elif name == "annotated":
            _real_git(repo, "tag", "-a", "v1", "-m", "v1")
    _real_git(repo, "reset", "--quiet", "--hard", commits["main"])
    listing = repo / "ls-remote.out"
    listing.write_text(_real_git(repo, "ls-remote", str(repo)) + "\n", encoding="utf-8")
    tag_object = _real_git(repo, "rev-parse", "v1")
    assert tag_object != commits["annotated"]   # so the test can tell them apart
    return {"listing": listing, "commits": commits, "tag_object": tag_object}


def _stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def run_step(tmp_path, step, env):
    """Run `step`'s body as GitHub's `shell: bash` does, with stub git and
    uvx: (exit code, stdout+stderr, what it wrote to GITHUB_ENV, the uvx
    arguments it passed)."""
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    calls = tmp_path / "uvx-calls"
    _stub(stubs, "git", 'if [ "$1" = ls-remote ]; then cat "$LS_REMOTE_OUTPUT"; exit 0; fi\n'
                        'echo "unexpected git $*" >&2; exit 97\n')
    _stub(stubs, "uvx", f'printf "%s\\n" "$*" >> "{calls}"\n'
                        'if [ -n "$UVX_SAY" ]; then printf "%s\\n" "$UVX_SAY"; fi\n'
                        'exit "${UVX_EXIT:-0}"\n')
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    done = subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        capture_output=True, text=True, cwd=tmp_path,
        env={"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(tmp_path),
             "GITHUB_ENV": str(github_env), "CURAMOIR_STATE_BRANCH": "curamoir/state",
             **env})
    used = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done.returncode, done.stdout + done.stderr, github_env.read_text(
        encoding="utf-8"), used


def _resolve(steps):
    return step_running(steps, "git ls-remote https://github.com/rsionnach/curamoir")


@needs_bash
@pytest.mark.parametrize("ref, expected", [
    ("release", "release"), ("light", "light"), ("v1", "annotated")])
def test_the_resolve_step_writes_the_commit_a_branch_or_tag_names(tmp_path, steps, upstream,
                                                                  ref, expected):
    code, out, written, _ = run_step(tmp_path, _resolve(steps), {
        "CURAMOIR_REF": ref, "LS_REMOTE_OUTPUT": str(upstream["listing"])})

    assert code == 0, out
    assert written == f"CURAMOIR_SHA={upstream['commits'][expected]}\n"
    # An annotated tag resolves to its commit, never the tag object.
    assert upstream["tag_object"] not in written


@needs_bash
def test_the_resolve_step_takes_a_full_sha_as_it_is(tmp_path, steps, upstream):
    sha = upstream["commits"]["main"]

    code, out, written, _ = run_step(tmp_path, _resolve(steps), {
        "CURAMOIR_REF": sha, "LS_REMOTE_OUTPUT": "/nonexistent"})

    assert code == 0, out
    assert written == f"CURAMOIR_SHA={sha}\n"


@needs_bash
@pytest.mark.parametrize("ref", ["abbreviated", "nope"])
def test_the_resolve_step_fails_red_for_a_short_sha_or_an_unknown_ref(tmp_path, steps,
                                                                      upstream, ref):
    ref = upstream["commits"]["release"][:12] if ref == "abbreviated" else "no-such-ref"

    code, out, written, _ = run_step(tmp_path, _resolve(steps), {
        "CURAMOIR_REF": ref, "LS_REMOTE_OUTPUT": str(upstream["listing"])})

    assert code == 1
    assert "::error" in out and ref in out
    assert written == ""


SHA = "0123456789abcdef0123456789abcdef01234567"


@needs_bash
@pytest.mark.parametrize("uvx_exit, step_exit, annotation", [
    (0, 0, None), (3, 0, "::warning title=cúramóir sweep refused::"), (1, 1, None)])
def test_the_sweep_step_keeps_a_refusal_green_and_fails_a_failure(tmp_path, steps,
                                                                  uvx_exit, step_exit,
                                                                  annotation):
    code, out, _, used = run_step(tmp_path, step_running(steps, "curamoir run sweep"), {
        "CURAMOIR_SHA": SHA, "UVX_EXIT": str(uvx_exit)})

    assert code == step_exit, out
    assert (annotation in out) if annotation else ("::warning" not in out)
    assert used == [f"--from git+https://github.com/rsionnach/curamoir@{SHA} curamoir run "
                    "sweep"]


@needs_bash
@pytest.mark.parametrize("uvx_exit, step_exit, annotation", [
    (0, 0, None), (5, 0, "::notice title=cúramóir state::"), (3, 3, None), (1, 1, None)])
def test_the_state_step_passes_a_push_and_nothing_to_push_and_fails_the_rest(
        tmp_path, steps, uvx_exit, step_exit, annotation):
    code, out, _, used = run_step(tmp_path, step_running(steps, "curamoir state push"), {
        "CURAMOIR_SHA": SHA, "UVX_EXIT": str(uvx_exit), "UVX_SAY": "cúramóir said this."})

    assert code == step_exit, out
    assert "cúramóir said this." in out
    assert (annotation in out) if annotation else ("::notice" not in out)
    assert "::warning" not in out
    assert used == [f"--from git+https://github.com/rsionnach/curamoir@{SHA} curamoir state "
                    "push --branch curamoir/state"]


@needs_bash
@pytest.mark.parametrize("uvx_exit, step_exit", [(0, 0), (5, 0), (1, 1)])
def test_the_state_step_warns_when_lines_were_skipped(tmp_path, steps, uvx_exit, step_exit):
    """The sentence's "It skipped N line(s)" clause (state_branch's
    _skipped_note) is the signal, whatever the exit."""
    from curamoir.state_branch import _skipped_note

    said = "cúramóir pushed 1 ledger line to curamoir/state (abc)." + _skipped_note(2)
    code, out, _, _ = run_step(tmp_path, step_running(steps, "curamoir state push"), {
        "CURAMOIR_SHA": SHA, "UVX_EXIT": str(uvx_exit), "UVX_SAY": said})

    assert code == step_exit, out
    assert "::warning title=cúramóir state::" in out
