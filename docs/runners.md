# Runners: the two seams

This page is for whoever wants cúramóir's scheduled sweep to run somewhere
new: another scheduler, or another agent CLI in place of `claude -p`. There
are two seams, and nothing else in the sweep is provider-specific:

- **Seam A, the scheduler**: what runs `curamoir run <job>`, and where.
- **Seam B, the agent runner**: the one model call a job makes, the
  contract `fix_step.run_fix_step` keeps today.

Which section is for you:

- Setting up the GitHub Actions workflow: [the worked example](#the-worked-example-github-actions), starting with its setup items.
- Setting up a Routine: [Claude Code Routine](#claude-code-routine).
- Writing a new scheduler: [Seam A](#seam-a-the-scheduler).
- Writing an agent runner for another CLI: [Seam B](#seam-b-the-agent-runner).

Cursor and Codex are described as what a contributor would write; neither
is built, and this page carries no adapter code. Why the hosted runner is
the default, and what each runner guards, is in
[Platform-first runners](direction-platform-first.md) §3 and §7; the
boundary as a user sees it is the README's
[Guardrails](../README.md#guardrails).

## Seam A: the scheduler

A scheduler is supported when it can run `curamoir run <job>` with
`CURAMOIR_RUNNER=hosted` on a disposable VM, and then always
`curamoir state push --branch <state branch>`, from the root of a clone of
the repository being swept. For the sweep:

```sh
CURAMOIR_RUNNER=hosted curamoir run sweep
curamoir state push --branch <state branch>
```

It also needs, in the VM:

- a git identity (the sweep refuses to commit without one);
- push credentials for origin, and `gh` authenticated (the sweep's branch,
  its PR and the state branch);
- the clone's full history;
- uv, the claude CLI and ast-grep, pinned;
- a Claude credential for `claude -p`;
- a repository `/curamoir:init` has initialised (otherwise nothing runs,
  exit 4).

**The run's exit codes** (`runner.py`, one sentence printed per run):

| Exit | Means | A scheduler should |
|---|---|---|
| `0` | success | pass |
| exit 3 | refused: a guard or the sweep declined, reason in the sentence and the ledger line | show it, not fail (an expected outcome) |
| anything else | failure | fail |

**The runner kind is declared, never inferred.** `config.resolve_runner` is
the one reader of the declaration. Its rule is exact and case-sensitive:

| `CURAMOIR_RUNNER` | Hosted marker | The run |
|---|---|---|
| unset, empty, or `local` | any | local |
| `hosted` | `CLAUDE_CODE_REMOTE=true` (every Claude Code cloud session) | hosted |
| `hosted` | `GITHUB_ACTIONS=true` with `RUNNER_ENVIRONMENT=github-hosted` | hosted |
| `hosted` | `GITHUB_ACTIONS=true`, `RUNNER_ENVIRONMENT` anything else or unset (self-hosted, `act`, GitHub Enterprise Server, an older runner) | refused |
| `hosted` | neither | refused |
| anything else (`Hosted`, `ci`, a value with a space) | any | refused |

A refusal is reason `runner`, exit 3. Its sentence gives the value's length
at most: the value itself is never echoed, since the sentence lands in the
committed ledger, and a secret pasted into the variable by mistake would
land with it. It is checked after the kill switch and the lock, so a paused
repository reads paused even when misdeclared.

A marker without the declaration runs local. On a GitHub runner with
`CURAMOIR_RUNNER` unset, the sweep wants the OS sandbox around the tests,
and where it finds none it refuses with "no OS sandbox confines it here":
the cue to declare `hosted`.

**The state push** commits the run's new ledger lines to a state branch
(`state_branch.py`), never the default branch, because the VM is discarded
with them. Run it always: after a success, a refusal, a failure or a
timeout of the run.

| Exit | Means | A scheduler should |
|---|---|---|
| `0` | pushed | pass |
| `5` | nothing new to push | pass |
| `3` | refused (the default branch, a branch that is not a state branch, among others) | fail: the lines were not kept |
| `1` | failed (git or a ruleset rejected the push, or the branch kept moving) | fail: the lines were not kept |

If its sentence says "It skipped N line(s)", some lines were not ledger
lines and were not committed: show a warning, whatever the exit. Use one
state branch per scheduler (`curamoir/state` from Actions,
`claude/curamoir-state` from a Routine), and one scheduler per repository:
two share no lock and no ledger.

**A new hosted scheduler must be a disposable VM**: fresh for the run,
holding no developer's home, discarded afterwards. That is what `hosted`
means, and why its tests run unsandboxed and its fix agent gets a shell. A
scheduler on a machine someone keeps (a laptop, a self-hosted runner) is
the local adapter: leave `CURAMOIR_RUNNER` unset, and the OS sandbox holds
the run.

**Adding a hosted marker is a code change, not a documentation change.** A
VM that sets neither marker above is refused `hosted` until
`config.resolve_runner` accepts a variable that only that platform's
disposable VMs set, matched exactly as the two above are: a constant beside
`CLAUDE_MARKER` and `GITHUB_MARKER` in `config.py`, cases in
`tests/test_runner_kind.py`, and a row in Platform-first runners §3.
Nothing else reads the markers; every other hosted branch reads the
resolved `runner.hosted`.

## The worked example: GitHub Actions

`templates/github/curamoir-sweep.yml` is seam A on GitHub-hosted Actions,
and `/curamoir:init` offers to write it. The template is the single source:
its header lists what to set up, and its comments say why each step is
there.

**The four setup items:**

1. The `ANTHROPIC_API_KEY` repository secret: the model calls are billed per
   token to that key. The subscription OAuth token is not wired; whether its
   terms permit unattended scheduled use is open (Platform-first runners
   §7, billing note).
2. The `CURAMOIR_REF` repository variable: a cúramóir tag or commit.
3. Settings > Actions > General > "Allow GitHub Actions to create and
   approve pull requests", or the sweep's PR cannot open.
4. If a ruleset or branch protection covers the state branch, it must let
   `github-actions[bot]` push to it, or every run's ledger lines are lost
   and the job fails at its last step.

**Add no other secrets to the sweep step's `env:`.** The fix step drops
`GITHUB_TOKEN`, `GH_TOKEN`, the gh enterprise tokens and the `ACTIONS_*`
runtime and OIDC variables from its process, by name. Any other secret you
put in the sweep step's `env:` reaches the agent, so add none. Keep the
workflow's `permissions:` minimal. Even so, the agent runs as the runner's
user on the same VM and can still reach what the sweep holds: the boundary
is the VM and the token's scope (the README's
[Hosted runner limits](../README.md#hosted-runner-limits)).

**The agent's test command.** On hosted the fix agent may run the base's
test command, through a Claude Code permission rule that names it. A
command no rule can name exactly gets no shell at all, so the agent cannot
run tests (the green gate still does). It can be named when each part
between `&&`s has no `VAR=` prefix, no quotes, pipes or `;`, none of
`` $ < > ( ) * , # \ ` ``, and does not start with a wrapper Claude Code
strips (`timeout`, `nice`, `xargs` and the like), a shell or another
general command runner (`eval`, `source`, `exec`, `env`, `sudo`, `su`,
`watch`, `setsid` and the like), or git.

The steps, in order:

**Triggers, permissions, one run at a time.** A weekly `schedule:` and
`workflow_dispatch`; `permissions:` of `contents: write` and
`pull-requests: write` only; a concurrency group, so a second sweep waits
rather than racing the first for the same PR and state branch.

**Pinned tools, GitHub-hosted only.** The claude CLI (and ast-grep, as
`AST_GREP_VERSION`) are pinned in the workflow's `env:`, so a run never
installs whatever was released that morning. `runs-on` must stay a
GitHub-hosted image, because `RUNNER_ENVIRONMENT=github-hosted` is half of
the marker:

```yaml
CLAUDE_CODE_VERSION: "2.1.308"
```

```yaml
runs-on: ubuntu-latest
```

**The cúramóir ref, resolved once.** With `CURAMOIR_REF` unset the first
step fails, red, before anything is installed or spent. The next resolves
it to a full commit SHA for every later step, so the sweep and the state
push run the same cúramóir even if the tag is moved between them:

```yaml
echo "Running cúramóir at ${sha} (${ref})."
echo "CURAMOIR_SHA=${sha}" >> "$GITHUB_ENV"
```

**Actions pinned by commit SHA.** Every action, GitHub's own included, is
pinned to a full SHA (a tag can be moved). The checkout keeps full history
and writes no token into `.git/config`:

```yaml
- uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
  with:
    fetch-depth: 0
    persist-credentials: false
```

**uv, with its cache off.** setup-uv's default saves the uv cache and
restores it on later runs, so a run's tests or fix agent could plant the
cúramóir a later run executes:

```yaml
- uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
  with:
    enable-cache: false
```

**Identity and credentials.** The run commits as `github-actions[bot]`;
pushes go through gh's credential helper, which reads `GH_TOKEN` from the
pushing step's environment, so the token is never stored on disk:

```yaml
gh auth setup-git --hostname github.com
```

**The sweep.** The declaration is on this step alone:

```yaml
env:
  CURAMOIR_RUNNER: hosted
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  GH_TOKEN: ${{ github.token }}
```

```yaml
case "$status" in
  0) ;;
  3) echo "::warning title=cúramóir sweep refused::The sweep was refused; the reason is in the step log and in the ledger line." ;;
  *) exit "$status" ;;
esac
```

A runner refusal (not GitHub-hosted, or no marker) is exit 3 too, so it
also shows as a green job with a warning: read the warning. The 60-minute
timeout is on this step, not the job, so the state push still runs after
it.

**The state push, always.** Of this step's outcomes, only a refusal (3) or
a failure fails the job, because then the ledger line was not kept; "It
skipped" becomes a warning and exit 5 a notice:

```yaml
- name: curamoir state push
  if: always() && env.CURAMOIR_SHA != ''
```

The step is skipped only when `CURAMOIR_REF` was unset or did not resolve;
an earlier step has then already failed the job.

## Claude Code Routine

A Routine runs the prompt in `templates/routine/sweep-prompt.md`, which
`/curamoir:routine` shows with the `/schedule` steps. It pins one cúramóir
commit, runs `CURAMOIR_RUNNER=hosted curamoir run sweep`, then always
`curamoir state push --branch claude/curamoir-state`. It runs as you (your
GitHub identity, your Claude usage). Remove its connectors: the sweep needs
none, and a run can use any connector it is given.

Nothing in cúramóir keeps two Routine runs apart: each run's lock is on its
own VM. Whether a scheduled run can start while a **Run now** run is going
has not been verified; the state push's lease keeps either from
overwriting the other's lines, but do not press Run now while a run is
going. "Succeeded" means only that the session ended; read the run's
report for what the sweep did.

### Routine environment

<!-- cm-2qp.10 fills this section: the Routine's cloud environment recipe
(setup script, environment variables, network access level, and the
default branch's protection rule), word for word the same text as
commands/routine.md section 3. Until then it says only what follows. -->

Not written yet. Until it is, a Routine made from `/curamoir:routine` is
not expected to complete a sweep; use the GitHub Actions workflow.

## Seam B: the agent runner

`fix_step.run_fix_step` is the only model-specific call in the sweep: the
fix step makes it with an editing toolset, and the tier-1 scan with a
read-only one. A `codex exec` or Cursor CLI runner would implement the same
contract in its place. The source is `fix_step`'s module docstring, "The
run_fix_step contract"; in short:

- **In.** The clone (a `git_guard.Sandbox`, never a checkout); the prompt,
  which carries the work list; the model; the tools; the timeout;
  `max_turns`; `before_call`; the runner kind; and `test_command`, the
  base's.
- **Must.** Snapshot `git_state` (`git_guard.snapshot_git_state`) before the
  agent starts; call `before_call` right before the process that may spend
  starts (the ledger notes the call as unrecorded there, so a run killed
  mid-call never reads as free); kill the whole process group at the
  timeout; leave the edits in the clone's work tree, never committed.
- **Out.** A `FixStepRun`: `started`, `exit_code` and `timed_out` say whether
  and how the agent ran; `stderr` starts with `fix_step.NOT_STARTED` when
  the process could not start; `git_state` is the snapshot, returned for
  `fix_scope.verify_fix` and the scan to hold the clone to; `argv` carries
  `--model <model>` (the scan reads the model from there, and fails after
  spending without it); `stdout` is the payload below.
- **Not its job.** The scope check (`fix_scope.verify_fix`, which reads the
  diff from the clone against `git_state`), the green gate and publish stay
  the caller's.

Besides the four bullets above, `fix_step` enforces more today, and another
CLI's runner must meet the same requirements by its own means:

- **Only the allowed tools, on both runner kinds (`cm-xq9`).** Today: no
  tool outside `SHELL_FREE_TOOLS` (Read, Grep, Glob, Edit), plus
  `HOSTED_SHELL_TOOLS` (Bash) for the fix step on the hosted runner only.
  On local a shell-capable toolset is refused before the process starts. No
  tool that creates files, no web access, no MCP server. Reads and edits
  are scoped to the clone with its `.git` denied; on hosted the shell is
  approved for the base's test command and Claude Code's built-in
  read-only commands, with git denied. A CLI that cannot be held to that
  set cannot be a local runner.
- **The clone's own configuration never loads.** Today: `--restricted`,
  `--strict-mcp-config` with no `--mcp-config`, and a `mcp__*` deny, on both
  kinds. The clone's project settings, hooks, plugins and MCP servers are
  text a hostile commit can write.
- **The command denylist runs before any shell command.** Today:
  cúramóir's PreToolUse hook (`hooks/hooks.json`'s entries, the policy in
  `src/curamoir/hooks/`) comes in through `--settings`, so it runs with the
  clone's settings shut out and refuses `rm -rf build` and its kind. It
  catches mistakes; the VM is still the boundary.
- **A fixed environment.** Today: inherited `GIT_*` variables dropped, the
  rest kept, `CURAMOIR_JOB` included (the denylist's signal in a clone with
  no lock), and `CLAUDE_PLUGIN_ROOT` set (where the hook is found). On
  hosted the fix step's process loses `HOSTED_RUN_TOKEN_VARIABLES` (the
  run's GitHub and Actions tokens; the scan's keeps them), and with a shell
  it gets `HOSTED_TEST_ENVIRONMENT` (no bytecode, no pytest cache,
  `CI=true`) and uv's project environment outside the clone with
  `uv.lock` frozen.
- **The prompt passes as it is.** Today: `fix_prompt` builds it for the
  runner kind. It is not the fence: `fix_scope` checks the result as if
  the prompt had said nothing.

**How "tools" and `max_turns` map.** The tools are a set of cúramóir's names
(Read, Grep, Glob, Edit, Bash); a runner translates them into its CLI's own
tool and approval settings, and refuses a set it cannot express exactly
rather than run wider. `max_turns` bounds the agent's steps; if the CLI's
non-interactive mode has no turn limit, the timeout and the process-group
kill are the bound, and the runner says so.

**The payload another CLI must emit, or translate into.** `stdout` is one
JSON object in the shape of `claude -p --output-format json`'s result:

| Field | Required | Who reads it |
|---|---|---|
| `usage.input_tokens`, `usage.output_tokens` | yes, numbers | the ledger (spend) |
| `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens` | when the provider caches | the ledger |
| `usage.cache_creation.ephemeral_5m_input_tokens`, `ephemeral_1h_input_tokens` | optional | the ledger; unsplit writes are priced at the 1-hour rate |
| `total_cost_usd` | expected | the ledger, for the record (not the ceiling) |
| `is_error` | on an error, a real boolean `true` | the sweep and the scan (the scan ignores `subtype`) |
| `subtype` | on an error, starting `error` (`error_max_turns`) | the sweep |
| `result` | yes, for the scan | the scan (its findings), the fix step (its replies) |

A call that started and printed no usable `usage` is recorded as
unrecorded, never as zero. The tokens are priced by model from
`budget.prices` (defaults in `config.py`, overridable in
`.curamoir/curamoir.yaml`): for a model with no price there, the budget
guard is degraded and the monthly ceiling is NOT enforced until one is
added.

## Other providers

Neither is built. Each is what a contributor would write, through the two
seams above.

### Cursor

Seam A: a Cursor Automation runs in a Cursor cloud VM, so it is a shell step
that runs the two commands from [Seam A](#seam-a-the-scheduler) in the
clone, with the cúramóir commit pinned, everything that section's "it also
needs" list names, and exit 3 shown as a refusal rather than a failure. It
cannot declare `hosted` yet: no marker `config.resolve_runner` accepts is
set there, so the contribution starts with a marker only Cursor's
disposable VMs set, added to `config.py` with its tests.

Seam B, optionally: a runner that puts Cursor's CLI, in its non-interactive
mode, behind the `run_fix_step` contract, with every requirement under
[Seam B](#seam-b-the-agent-runner) met by Cursor's own settings and hooks.
Without it the agent stays `claude -p`.

### Codex

A Codex Automation runs locally, through the Codex app with the laptop
awake, so it is not a hosted runner: it is the local adapter, seam A with
`CURAMOIR_RUNNER` unset, the OS sandbox around the tests, no shell for the
agent, and the supervisor lock as on any local run. Its step is
`curamoir run sweep` with no declaration; if it runs in your checkout, the
ledger stays there and no state push is needed. A seam-B runner would wrap
`codex exec`, Codex's non-interactive mode, behind the `run_fix_step`
contract: the clone as its working directory, the toolset translated into
Codex's own settings (read and edit in the clone only, no shell on the
local runner), the clone's own Codex configuration kept from loading, and
its output translated into the payload
[Seam B](#seam-b-the-agent-runner) describes.
