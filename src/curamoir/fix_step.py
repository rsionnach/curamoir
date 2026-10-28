"""E3-R2's first half, and the sweep's one model call: the fix step.

The sweep is driven by Python (the decision recorded on cm-80h): the runner
lints in-process in the sandbox (fix_scope.lint_sandbox), hands the work
list to a headless `claude -p` on `models.cheap` whose only job is to edit
files, and then does every deterministic step itself — the scope check
(fix_scope.verify_fix), the tests (E3-R4), the PR (E3-R3), the ledger
(E3-R5). This module is the hand-off: the limits the prompt states, the
prompt, and the command line.

**Where it runs.** In a sandbox (git_guard.prepare_sandbox): a disposable
clone of the base commit, never the user's checkout. The agent's working
directory and every permission rule below name the sandbox's path, so "the
repo" in what follows is the sandbox. fix_scope's module docstring lists
what the runner (cm-fkd.4) does around this step.

**The limits are one text.** E3-R2 says "the skill prompt MUST state these
limits". There are two prompts — this one, for the scheduled sweep, and
skills/sweep/SKILL.md, for `/curamoir:sweep` — so SCOPE_LIMITS is the single
copy and tests/test_fix_step.py fails if the skill's copy differs from it by
anything but line wrapping. Its numbers (WINDOW) come from fix_scope, so
what the prompt promises and what the review step checks cannot drift
either. The prompt is still not the enforcement: fix_scope.verify_fix checks
the result as if the prompt had said nothing.

**The command line, and the documentation it rests on** (checked 2026-10-11):

- `--tools "Read,Grep,Glob,Edit"`: "Restrict which built-in tools Claude can
  use", as opposed to `--allowedTools`, which only pre-approves ("To restrict
  which tools are available, use `--tools` instead").
  https://code.claude.com/docs/en/cli-reference — so there is no Bash, no
  Write (which creates files) and no web access to misuse. The same page
  notes that on macOS, Linux and WSL the *default* set leaves out Glob and
  Grep; naming them here asks for them, and if they are absent Read and Edit
  are all a fix needs.
- `--disallowedTools "mcp__*"`: `--tools` "doesn't affect MCP tools; to deny
  those too, use `--disallowedTools "mcp__*"`" (same page). Paired with
  `--strict-mcp-config` and no `--mcp-config`, so no MCP server is loaded.
- `--permission-mode dontAsk`: "Claude Code denies every call that would
  otherwise prompt … Actions that need no approval in Manual mode still run
  … and so do actions your `--allowedTools` entries or `permissions.allow`
  rules cover" (https://code.claude.com/docs/en/headless, "Auto-approve
  tools"), so anything not approved is refused rather than waited on. Named
  explicitly because a `-p` run otherwise "takes the built-in starting
  permission mode, which can be `auto`" (same section).
- `--allowedTools` with sandbox-scoped rules only (repo_rules), never a bare
  tool name. WHY: a bare name "matches that rule at the tool level
  everywhere" (https://code.claude.com/docs/en/permissions, "Read and
  Edit"), so `--allowedTools Edit` under dontAsk approved an edit to
  ~/.bashrc or ~/.claude/settings.json — which fix_scope, reading only this
  repo's `git diff`, can never see — and `Read` approved reading ~/.ssh into
  the agent's context. The rules are `Read(//<repo>/**)` and
  `Edit(//<repo>/**)`: "Read and Edit rules both use gitignore pattern
  syntax", and `//path` is "Absolute path from filesystem root" (same page).
  Grep and Glob get no allow rule of their own: Claude Code "checks file
  permissions against `Edit(path)` and `Read(path)` rules only" and "makes a
  best-effort attempt to apply `Read` rules to all built-in tools that read
  files like Grep and Glob"; and read-only tools need no approval "within
  the working directory and additional directories" (same page), which here
  is the repo, since no `--add-dir` is passed. Outside it they would prompt,
  and dontAsk turns a prompt into a refusal — so no deny list of sensitive
  paths is needed for the default case: the fence is the allow list, and
  everything outside it is denied without being named.
- `--disallowedTools` also carries repo_deny_rules: Read and Edit of the
  repo's `.git`. The allow rule above covers `.git/`, where an edit is code
  the next git command runs and `git diff` never shows it; "Rules are
  evaluated in order: deny, then ask, then allow" (same page), so the deny
  holds inside the allowed tree. run_fix_step also snapshots `.git` and the
  work tree (git_guard.snapshot_git_state) for the review step to hold
  them to.
- `--restricted`: it "confines the built-in file tools to the working
  directories, loads only managed settings and `--settings`"
  (https://code.claude.com/docs/en/cli-reference). WHY: without it the
  user's and the project's `permissions.allow` rules and
  `additionalDirectories` still load and could re-widen the fence above —
  and a project's settings are text in the very repository being swept.
  It also drops the user's hooks and plugins from this run, so cúramóir's
  own Stop hook does not write into the sandbox; its PreToolUse denylist
  comes back through `--settings`, next. It "Requires
  Claude Code v2.1.248 or later"; an older CLI rejects the flag and the
  step fails loudly rather than running unfenced.
- `--settings` with pretooluse_settings (cm-60c): `--restricted` still
  loads "managed settings and `--settings`" (`claude --help`, 2.1.286), so
  the E0-R5 denylist is registered there: hooks/hooks.json's PreToolUse
  entries exactly, with the plugin root written into the command, and no
  other event. WHY not `--plugin-dir`: it loads the whole plugin, Stop
  hook included, which `--restricted` keeps out (fix_scope's Limits). WHY
  now, with no Bash in the toolset: a later change that adds a shell tool
  gets the denylist without having to remember it. The hook only prints
  its decision; it writes nothing into the sandbox. CLAUDE_PLUGIN_ROOT is
  set to the same root in the run's environment, since the hook's
  launcher prefers it to its own location. Held by a stand-in `claude`
  that loads hooks only from this flag (tests/test_job_env.py). That the
  real CLI runs them was checked once by hand (2026-10-27, Claude Code
  2.1.286, `-p --restricted --tools Bash --permission-mode dontAsk`,
  CURAMOIR_JOB=sweep): `rm -rf build` ran without these settings and was
  refused with the denylist's own message with them. No test calls a
  model, so re-check by hand when Claude Code changes `--restricted`.
- `--disable-slash-commands` ("Disable all skills and commands for this
  session"): the fix agent is handed its instructions here, so no skill —
  including /curamoir:sweep, which would run the lint itself — can be
  invoked from inside it.
- `--no-session-persistence`, `--max-turns`, `--output-format json` (whose
  payload "includes `total_cost_usd`", for E11-R1's ledger line), and
  `--model` with `models.cheap`, as documented on the CLI reference.
- NOT `--bare`, though the docs recommend it for scripts: "In bare mode,
  Claude Code never reads OAuth credentials or the system keychain", and the
  local scheduled mode runs on the developer's subscription (spec §6.2).
  `--restricted` is not documented as touching credentials (it limits
  settings, tools and file access); that is the docs' word, not a run's.
- NOT a throwaway HOME around the run, though it would put ~/.ssh and
  ~/.bashrc out of reach without trusting Claude Code at all: "On Linux,
  credentials are stored in `~/.claude/.credentials.json`", and on macOS the
  Keychain entry is keyed to the config directory, with the same file as
  its fallback (https://code.claude.com/docs/en/authentication), so a fresh
  HOME is a logged-out `claude` on the subscription this mode exists for.
- NOT `--permission-prompts none`: it "requires Claude Code v2.1.259 or
  later. Earlier versions reject it", and "in a `-p` run with no host, these
  requests are denied either way".

The process runs in a session of its own with the inherited `GIT_*`
variables dropped and the rest kept, CURAMOIR_JOB included (set by
job_guard.run_guarded for the run: the E0-R5 denylist's signal in a clone
that has no lock, cm-a2s), which the denylist loaded by `--settings`
(above) reads, and CLAUDE_PLUGIN_ROOT set to PLUGIN_ROOT. At the timeout its whole process group is killed
(git_guard.run_in_group), so nothing it started goes on editing the sandbox
while the review reads it.

**The residual risk, stated plainly.** The sandbox is a directory, not an
operating-system sandbox: the fence around the fix agent's reads and edits
outside it is Claude Code's own permission check, with no separate user.
The docs scope that check the same way — rules "don't apply to … arbitrary
subprocesses", which this toolset has none of, and Grep/Glob honour Read
rules on a "best-effort" basis. fix_scope reads only the sandbox, so an
edit outside it that slipped this fence (~/.bashrc) would go unseen by the
review step, and a read is never visible to it. What the sandbox does
remove is the user's checkout from reach: its config, hooks, ignored files
and uncommitted work are not in the agent's tree. An OS sandbox is the
stronger fix for the rest, if one is wanted.

What these flags have not been shown to do is hold against a real model in
this repository: nothing here calls one (tests use a stand-in `claude`), so
the first scheduled sweep is the first end-to-end evidence.

**Not run through the agent.** The lint is not one of the agent's tasks: a
Bash tool call is bounded at ten minutes and the lint's own bound is 600s
plus sweep's 30s grace, so the runner runs it (fix_scope.lint_sandbox(sandbox),
which also records it as sandbox.work_list, without which verify_fix
refuses), applies the file-level remedies (remedies.apply_file_remedies),
and passes in the work list the agent is left with (RemedyResult.for_agent).

Syntax that imports on 3.9, like sweep.py.
"""

import json
import math
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from curamoir.fix_scope import SIGNATURE_PERMISSION, WINDOW
from curamoir.git_guard import (
    GitState,
    require_sandbox,
    run_in_group,
    scrubbed_environment,
    snapshot_git_state,
)
from curamoir.repo_config import config_value
from curamoir.sweep import work_list_document

#: E3-R2's limits, as both prompts state them. The first paragraph is the
#: spec's sentence verbatim; the list says what fix_scope checks, in the
#: terms of this repository. skills/sweep/SKILL.md carries the same text.
SCOPE_LIMITS = f"""\
E3-R2, verbatim: "Fixes MUST be scoped: only files with violations; no
refactors beyond the violation; no new dependencies; no signature changes
without an explicit principle permitting it."

After you finish, cúramóir checks every change against these limits in code
and opens no PR from a fix that breaks any of them:

- Edit only the files in the work list's `paths`, and touch no other file.
  Do not create, rename or delete a file. A violation that is the file
  itself (reported at line 1, column 1 to line 1, column 1 with an empty
  `match`, which is how a rule about a file's name is reported) is not
  yours to fix: where its rule says `remedy: delete_file`, cúramóir handles
  it before you start and leaves it out of your work list; any other
  such violation, leave as it is and say so.
- Change only a violation's own lines and at most {WINDOW} lines either side
  of it. The one exception is adding, at the top of the module before its
  first function or class, an import, a logger (`logger =
  logging.getLogger(__name__)`) or a new constant set to a plain literal;
  any other code there is held to the rule above.
- Do not change a dependency manifest or lockfile (pyproject.toml,
  requirements files, package.json, uv.lock and the like), and do not import
  a package the repository does not already use.
- Do not add, remove or change the signature of any function, method or
  class (its parameters, defaults, annotations, return type, decorators or
  bases) unless the principle of a violation in that function or class
  says `permits: {SIGNATURE_PERMISSION}`.
- If a violation cannot be fixed within these limits, leave it as it is and
  say so. An unfixed violation is reported; an out-of-scope fix is discarded.
"""

#: Backstops, not the budget: E3-R6's per-run ceiling (cm-fkd.7) is the
#: real bound. Enough turns to read and edit twenty files (max_files_per_pr)
#: a few times over; half an hour of wall clock for a weekly job.
DEFAULT_MAX_TURNS = 60
DEFAULT_TIMEOUT = 1800

#: The fix agent's whole toolset (the module docstring cites the docs).
FIX_TOOLS = ("Read", "Grep", "Glob", "Edit")

#: The only tools a job's agent may be given (cm-xq9, user decision
#: 2026-10-28: no release with known security gaps). None runs a command or
#: code. The command guard (hooks/policy.py) is defence in depth, not a
#: security boundary, so a shell (Bash, PowerShell, a REPL) or any tool not
#: known to be unable to run one is refused by fix_step_argv, allowlist-first:
#: a tool added to a job later is refused until it is put here on purpose,
#: and a shell-capable one only once its job runs under a verified OS sandbox
#: (cm-4w5). Write is left off too: no job needs it, and it could create a
#: file the review step never expected.
SHELL_FREE_TOOLS = frozenset({"Read", "Grep", "Glob", "Edit"})

#: What a FixStepRun's `stderr` starts with when its process could not be
#: started (FixStepRun.started is False); the OSError follows it.
NOT_STARTED = "the fix step could not start: "

#: The plugin's root: hooks/ and scripts/ sit beside src/.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

#: How hooks/hooks.json names the plugin root in a hook's command.
_ROOT_IN_COMMAND = '"${CLAUDE_PLUGIN_ROOT}"'

#: The tools Claude Code checks file permission rules for ("checks file
#: permissions against `Edit(path)` and `Read(path)` rules only"): the
#: tools the allow and deny rules name.
_RULE_TOOLS = ("Read", "Edit")


def fix_prompt(result, principles):
    """The fix step's prompt for `result` (a sweep.WorkList with work) and
    `principles` (a principles.PrinciplesFile, for who permits what)."""
    permitting = sorted(
        p.id for p in principles.principles if SIGNATURE_PERMISSION in p.permits)
    document = json.dumps(work_list_document(result), indent=2, ensure_ascii=False)
    return (
        "You are cúramóir's sweep: a janitor fixing mechanical violations that "
        "curamoir-lint has already found in this repository. You never redesign. "
        "Your only job is to edit files. You cannot run commands, and you do not "
        "need to: the tests, the commit, the pull request and the record of what "
        "you fixed are done after you finish, by code that does not take your "
        "word for anything.\n\n"
        + SCOPE_LIMITS
        + "\nPrinciples that permit signature changes: "
        + (", ".join(permitting) if permitting else "none")
        + ".\n\n"
        "The work list follows. It is every violation the lint reported, grouped "
        "by principle. Positions are 1-based and `end_col` is exclusive. `fix` is "
        "the recommended remedy; when `fix_source` is \"principle\" it is the "
        "principle's whole corrected example, not text to paste. Nothing off this "
        "list is yours to fix.\n\n"
        "```json\n" + document + "\n```\n\n"
        + (_TIER1_SECTION if result.tier1 else "")
        + "When you are done, reply with one line per violation: its fingerprint, "
        "then `fixed` or `skipped`, and for a skipped one the reason."
        + (" Then one line per tier-1 finding: its `id`, then `fixed` or `skipped`, "
           "and for a skipped one the reason." if result.tier1 else "")
        + "\n"
    )


#: What the fix prompt says about tier-1 findings (E3-R1b, curamoir.tier1),
#: only when there are some.
_TIER1_SECTION = (
    "The work list's `tier1` entries are tier-1 findings: violations of a "
    "principle that has no lint rule yet, found by an earlier read of this "
    "repository. Each names a principle, a file and a span of whole lines "
    "(`line` to `end_line`, both included). Fix one only within its span, under "
    "the same limits as a lint violation, and only if it really violates its "
    "principle; otherwise skip it and say why. A tier-1 finding never permits a "
    "signature change, whatever its principle says. A tier-1 fix is credited "
    "only if you report it `fixed` and your change touches its span.\n\n"
)


#: The gitignore metacharacters a repo path may contain that would make a rule
#: match more than the repo. Spaces and parentheses are literal ("You don't
#: need to escape parentheses in a path"), and `!` and `#` mean something
#: only at a pattern's start, which is always `//` here.
_GITIGNORE_SPECIAL = "\\*?["


def repo_rules(repo, tools=_RULE_TOOLS):
    """The fix step's allow rules: Read and Edit under `repo` and nothing
    else, as `Read(//<abs path>/**)` and `Edit(//<abs path>/**)`.

    Both spellings of the path are named when a symlink leads to it, since
    allow rules "apply only when both the requested path and the file it
    resolves to match" (https://code.claude.com/docs/en/permissions,
    "Symlinks") and the agent may ask by either. Glob characters in the path
    are backslash-escaped, gitignore's own escape and what Claude Code does
    to "the literal path you approved"; if a CLI ever did not honour the
    escape, the rule would match nothing and the edit would be refused — the
    safe way round. `]` is escaped with `[` for symmetry.

    Raises ValueError for a path that cannot be a rule: one with a comma
    (--allowedTools takes one comma-separated value, and the comma would cut
    the rule in two), a control character, or no absolute POSIX form.
    `tools` narrows them: the tier-1 scan (curamoir.tier1) is given Read's
    alone, so nothing approves an edit at all."""
    return [f"{tool}(/{root}/**)" for tool in tools for root in _rule_roots(repo)]


def repo_deny_rules(repo):
    """The fix step's deny rules: Read and Edit of the repo's `.git`, as both
    `.git` and `.git/**`, under both spellings of a symlinked path.

    WHY: `Edit(//<repo>/**)` covers `.git/`, and an edit there — a
    `core.fsmonitor` or `core.hooksPath` in `.git/config`, a file in
    `.git/hooks/` — is invisible to `git diff` and is code the next git
    command runs. Deny wins over the allow rule: "Rules are evaluated in
    order: deny, then ask, then allow. The first match in that order
    determines the outcome, and rule specificity doesn't change the order"
    and "An allow rule can't carve an exception out of a deny rule"
    (https://code.claude.com/docs/en/permissions, "Manage permissions"). Deny
    rules "apply when either the requested path or the file it resolves to
    matches" (same page, "Symlinks"), so naming both spellings is belt and
    braces. `.git` alone is named as well as `.git/**` because the latter is
    the directory's contents, and a `.git` file (as a linked worktree has;
    the sandbox's is a directory) is covered too, belt and braces. Claude
    Code also lists `.git` among its protected paths (same page,
    "bypassPermissions"); these rules do not lean on that. git_guard's
    snapshot (snapshot_git_state) is the check in code behind them."""
    return [f"{tool}(/{root}/{tail})" for tool in _RULE_TOOLS
            for root in _rule_roots(repo) for tail in (".git", ".git/**")]


def _rule_roots(repo):
    """The repo's absolute path, and its real path if a symlink leads to it,
    each escaped for a gitignore-syntax rule (repo_rules has the reasons)."""
    given = os.path.abspath(str(repo))
    roots = [given]
    real = os.path.realpath(given)
    if real != given:
        roots.append(real)
    for root in roots:
        if not root.startswith("/") or "," in root or any(ord(c) < 32 for c in root):
            raise ValueError(f"cannot scope the fix step's permissions to {root!r}: "
                             "it needs an absolute POSIX path with no comma or "
                             "control character")
    return ["".join("\\" + c if c in _GITIGNORE_SPECIAL + "]" else c for c in root)
            for root in roots]


def pretooluse_settings(root=PLUGIN_ROOT):
    """The `--settings` that load the E0-R5 denylist into the fix step
    (module docstring, `--settings`): hooks/hooks.json's PreToolUse
    entries as registered (matcher, timeout and all), each command's
    `"${CLAUDE_PLUGIN_ROOT}"` replaced by `root`, shell-quoted, and no
    other event. The entries are read from this plugin's own hooks.json, so
    the fix step denies what an interactive session denies. OSError or
    ValueError when they cannot be read or a command does not name the
    plugin root: a fix step whose denylist could not be loaded never
    runs."""
    registered = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = registered["hooks"]["PreToolUse"]
    loaded = []
    for entry in entries:
        hooks = []
        for hook in entry["hooks"]:
            command = hook["command"]
            if not command.startswith(_ROOT_IN_COMMAND):
                raise ValueError(f"hooks/hooks.json's PreToolUse command {command!r} does not "
                                 f"start with {_ROOT_IN_COMMAND}, so it cannot be loaded "
                                 "without the plugin")
            hooks.append(dict(hook, command=shlex.quote(str(root))
                              + command[len(_ROOT_IN_COMMAND):]))
        loaded.append(dict(entry, hooks=hooks))
    return {"hooks": {"PreToolUse": loaded}}


def fix_step_argv(prompt, model, repo, max_turns=DEFAULT_MAX_TURNS, claude="claude",
                  tools=FIX_TOOLS):
    """The `claude -p` command line for the fix step in `repo`. The prompt
    sits right after `-p`, as in the CLI reference's examples, and each
    list-valued flag takes one comma-separated value, so no flag can swallow
    the prompt. Raises ValueError as repo_rules does, and for a toolset with
    any tool outside SHELL_FREE_TOOLS: no job's agent gets a shell, or a tool
    that could run one, without an OS sandbox (cm-xq9).

    `tools` is the whole toolset; the allow rules follow it, so a toolset
    without Edit (the tier-1 scan's, curamoir.tier1.SCAN_TOOLS) approves no
    edit either. Every other flag is the same for both calls."""
    unsafe = sorted(set(tools) - SHELL_FREE_TOOLS)
    if unsafe:
        raise ValueError(
            f"a job's agent may not be given {', '.join(unsafe)}: only "
            f"{', '.join(sorted(SHELL_FREE_TOOLS))} run without an OS sandbox, and "
            "none is verified yet (cm-xq9, cm-4w5)")
    allowed = tuple(tool for tool in _RULE_TOOLS if tool in tools)
    return [
        claude, "-p", prompt,
        "--model", model,
        "--tools", ",".join(tools),
        "--allowedTools", ",".join(repo_rules(repo, allowed)),
        "--disallowedTools", ",".join(["mcp__*", *repo_deny_rules(repo)]),
        "--strict-mcp-config",
        "--restricted",
        "--settings", json.dumps(pretooluse_settings()),
        "--permission-mode", "dontAsk",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns", str(max_turns),
        "--output-format", "json",
    ]


@dataclass(frozen=True)
class FixStepRun:
    """What the fix step did, as far as its process says. None of it is
    evidence that the fix is in scope or correct: fix_scope and the tests
    decide that. `exit_code` is None when it was killed at the timeout.
    `git_state` is the sandbox's git files and work tree as they were before
    the agent ran (git_guard.snapshot_git_state), for fix_scope.verify_fix."""

    argv: tuple
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    git_state: Optional[GitState] = None

    @property
    def started(self):
        """False when the process could not be started at all (run_fix_step's
        OSError: no executable, no permission), so no model call was made
        and nothing was spent. A timeout is a start: `exit_code` is None
        then too, but `timed_out` says it ran."""
        return self.exit_code is not None or self.timed_out

    def payload(self):
        """The `--output-format json` result object, or None if stdout is not
        one (a crash, a timeout, an older CLI)."""
        try:
            document = json.loads(self.stdout)
        except ValueError:
            return None
        return document if isinstance(document, dict) else None


def usable_number(value):
    """True for a token count or a dollar figure from a `claude` payload
    (FixStepRun.payload: `usage`'s counts, `total_cost_usd`) that a ledger
    line may carry as a measurement: an int or float, not a bool, that
    converts to a finite float and is not negative. The one rule for both
    model calls' spend (curamoir.sweep_job's line cost and curamoir.tier1's
    scan cost), so the two cannot disagree on what was reported. Anything
    else is "not reported": a count is written as budget.UNRECORDED, a cost
    is left out.

    WHY each part: a NaN or an infinity would be written as a bare `NaN` or
    `Infinity`, which is not JSON; an int too large for a float (10**400)
    makes math.isfinite raise OverflowError, and budget.PriceTable would
    raise the same OverflowError when it priced the count; and a negative
    is no spend at all, but would subtract from the month's total (a
    count) or from the line's summed `cost_usd` (a cost)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and value >= 0
    except OverflowError:
        return False


def run_fix_step(sandbox, prompt, model=None, timeout=DEFAULT_TIMEOUT,
                 max_turns=DEFAULT_MAX_TURNS, claude="claude", tools=FIX_TOOLS,
                 before_call=None):
    """Run the fix step in `sandbox` (a git_guard.Sandbox) and return a
    FixStepRun. `model` defaults to `models.cheap` in the sandbox's config,
    which is the base commit's. `claude` is a seam for tests, which pass a
    stand-in script: no test calls a model. `tools` is fix_step_argv's
    (the tier-1 scan passes its read-only set).

    The sandbox is snapshotted (git_guard.snapshot_git_state) before the
    agent starts; fix_scope.verify_fix needs that snapshot. GitGuardError,
    and no agent started, if `sandbox` is not a Sandbox: the step never runs
    in a checkout.

    `before_call`, when given, is called with no arguments after every
    step that can fail without a call (the config read, the argv, the
    snapshot) and immediately before the `claude` process is started: the
    point after which the call may have spent money. The sweep notes the
    call's provisional cost there (fix_scope, "The line's cost")."""
    require_sandbox(sandbox)
    path = str(sandbox.path)
    if model is None:
        model = config_value(path, "models", "cheap")
    argv = fix_step_argv(prompt, model, path, max_turns=max_turns, claude=claude, tools=tools)
    git_state = snapshot_git_state(sandbox)
    environment = scrubbed_environment()
    environment["PWD"] = path
    # The hook's launcher takes its root from CLAUDE_PLUGIN_ROOT when set,
    # and one inherited from another plugin would run that plugin's code as
    # the denylist: the root the --settings name, always.
    environment["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    if before_call is not None:
        before_call()
    try:
        result = run_in_group(argv, cwd=path, env=environment, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        return FixStepRun(tuple(argv), None, _text(expired.output), _text(expired.stderr),
                          timed_out=True, git_state=git_state)
    except OSError as error:
        return FixStepRun(tuple(argv), None, "", f"{NOT_STARTED}{error}",
                          git_state=git_state)
    return FixStepRun(tuple(argv), result.returncode, _text(result.stdout),
                      _text(result.stderr), git_state=git_state)


def _text(data):
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="backslashreplace")
    return data
