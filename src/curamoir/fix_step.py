"""E3-R2's first half, and the sweep's one model call: the fix step.

The sweep is driven by Python (the decision recorded on cm-80h): the runner
lints in-process (sweep.work_list), hands the work list to a headless
`claude -p` on `models.cheap` whose only job is to edit files, and then does
every deterministic step itself — the scope check (fix_scope.verify_fix),
the tests (E3-R4), the PR (E3-R3), the ledger (E3-R5). This module is the
hand-off: the limits the prompt states, the prompt, and the command line.

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
- `--allowedTools` with repo-scoped rules only (repo_rules), never a bare
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
- `--restricted`: it "confines the built-in file tools to the working
  directories, loads only managed settings and `--settings`"
  (https://code.claude.com/docs/en/cli-reference). WHY: without it the
  user's and the project's `permissions.allow` rules and
  `additionalDirectories` still load and could re-widen the fence above —
  and a project's settings are text in the very repository being swept.
  It also drops the user's hooks and plugins from this run, so cúramóir's
  own Stop hook no longer writes into the fix step's tree. It "Requires
  Claude Code v2.1.248 or later"; an older CLI rejects the flag and the
  step fails loudly rather than running unfenced.
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

**The residual risk, stated plainly.** The fence around the fix agent's
reads and edits is Claude Code's own permission check, not the operating
system: no sandbox, no separate user. The docs scope it the same way — rules
"don't apply to … arbitrary subprocesses", which this toolset has none of,
and Grep/Glob honour Read rules on a "best-effort" basis. fix_scope checks
only the repository's diff, so an edit outside the repo that slipped this
fence would go unseen by the review step; and a read is never visible to it.
cm-fkd.4's runner should run the scheduled sweep in a disposable checkout
for its own reasons, and an OS sandbox is the stronger fix if one is
wanted.
- NOT `--permission-prompts none`: it "requires Claude Code v2.1.259 or
  later. Earlier versions reject it", and "in a `-p` run with no host, these
  requests are denied either way".

What these flags have not been shown to do is hold against a real model in
this repository: nothing here calls one (tests use a stand-in `claude`), so
the first scheduled sweep is the first end-to-end evidence.

**Not run through the agent.** The lint is not one of the agent's tasks: a
Bash tool call is bounded at ten minutes and the lint's own bound is 600s
plus sweep's 30s grace, so the runner runs it (sweep.work_list) and passes
the result in.

Syntax that imports on 3.9, like sweep.py.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from curamoir.fix_scope import SIGNATURE_PERMISSION, WINDOW
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

- Edit only the files in the work list's `paths`. Do not create, rename or
  delete a file, and do not touch any other file.
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
  bases) unless the violation's principle says `permits: {SIGNATURE_PERMISSION}`.
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
        "When you are done, reply with one line per violation: its fingerprint, "
        "then `fixed` or `skipped`, and for a skipped one the reason.\n"
    )


#: The gitignore metacharacters a repo path may contain that would make a rule
#: match more than the repo. Spaces and parentheses are literal ("You don't
#: need to escape parentheses in a path"), and `!` and `#` mean something
#: only at a pattern's start, which is always `//` here.
_GITIGNORE_SPECIAL = "\\*?["


def repo_rules(repo):
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
    the rule in two), a control character, or no absolute POSIX form."""
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
    escaped = ["".join("\\" + c if c in _GITIGNORE_SPECIAL + "]" else c for c in root)
               for root in roots]
    return [f"{tool}(/{root}/**)" for tool in ("Read", "Edit") for root in escaped]


def fix_step_argv(prompt, model, repo, max_turns=DEFAULT_MAX_TURNS, claude="claude"):
    """The `claude -p` command line for the fix step in `repo`. The prompt
    sits right after `-p`, as in the CLI reference's examples, and each
    list-valued flag takes one comma-separated value, so no flag can swallow
    the prompt. Raises ValueError as repo_rules does."""
    return [
        claude, "-p", prompt,
        "--model", model,
        "--tools", ",".join(FIX_TOOLS),
        "--allowedTools", ",".join(repo_rules(repo)),
        "--disallowedTools", "mcp__*",
        "--strict-mcp-config",
        "--restricted",
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
    decide that. `exit_code` is None when it was killed at the timeout."""

    argv: tuple
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False

    def payload(self):
        """The `--output-format json` result object, or None if stdout is not
        one (a crash, a timeout, an older CLI)."""
        try:
            document = json.loads(self.stdout)
        except ValueError:
            return None
        return document if isinstance(document, dict) else None


def run_fix_step(repo, prompt, model=None, timeout=DEFAULT_TIMEOUT,
                 max_turns=DEFAULT_MAX_TURNS, claude="claude", runner=subprocess.run):
    """Run the fix step in `repo` and return a FixStepRun. `model` defaults to
    the repository's `models.cheap`. `claude` and `runner` are seams for
    tests, which pass a stand-in script: no test calls a model."""
    if model is None:
        model = config_value(repo, "models", "cheap")
    argv = fix_step_argv(prompt, model, repo, max_turns=max_turns, claude=claude)
    try:
        result = runner(argv, cwd=str(repo), capture_output=True, timeout=timeout,
                        stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as expired:
        return FixStepRun(tuple(argv), None, _text(expired.stdout), _text(expired.stderr),
                          timed_out=True)
    except OSError as error:
        return FixStepRun(tuple(argv), None, "", f"the fix step could not start: {error}")
    return FixStepRun(tuple(argv), result.returncode, _text(result.stdout),
                      _text(result.stderr))


def _text(data):
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="backslashreplace")
    return data
