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
- `--permission-mode dontAsk` with `--allowedTools` naming the same four:
  "Claude Code denies every call that would otherwise prompt … Actions that
  need no approval in Manual mode still run … and so do actions your
  `--allowedTools` entries or `permissions.allow` rules cover"
  (https://code.claude.com/docs/en/headless, "Auto-approve tools"), so an
  edit runs and anything else is refused rather than waited on. Named
  explicitly because a `-p` run otherwise "takes the built-in starting
  permission mode, which can be `auto`" (same section).
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
  The cost is that the run loads the user's hooks and plugins — cúramóir's
  own included, whose Stop hook writes `.curamoir/handoff/latest.md` — and
  fix_scope reports any such write as out of scope. The runner (cm-fkd.4)
  must keep its own writes out of the fix step's tree.
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
  of it. The one exception is adding an import, or a module-level
  assignment such as a logger, to the top of the module, before its first
  function or class.
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


def fix_step_argv(prompt, model, max_turns=DEFAULT_MAX_TURNS, claude="claude"):
    """The `claude -p` command line for the fix step. The prompt sits right
    after `-p`, as in the CLI reference's examples, and each list-valued flag
    takes one comma-separated value, so no flag can swallow the prompt."""
    tools = ",".join(FIX_TOOLS)
    return [
        claude, "-p", prompt,
        "--model", model,
        "--tools", tools,
        "--allowedTools", tools,
        "--disallowedTools", "mcp__*",
        "--strict-mcp-config",
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
    argv = fix_step_argv(prompt, model, max_turns=max_turns, claude=claude)
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
