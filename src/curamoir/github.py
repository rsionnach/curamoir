"""The `gh` adapter: the repository's labels and its open pull requests.

Two read-only queries, the two callables the guard layer is given
(cm-fkd.10, discharging fix_scope's runner steps 0 (ii) and (iii)), both
through `run_gh`, the one way cúramóir runs gh. curamoir.publish
(cm-fkd.12) uses `run_gh` for its write too (`gh pr create`); there a
GhTimeout means the outcome is UNKNOWN: the PR may or may not exist.

- `repo_labels(repo)`, the kill switch's `curamoir:paused` check
  (guards.kill_switch), runs exactly `gh label list --limit 1000 --json
  name --jq '.[].name'` and returns its lines. WHY that command: gh lists
  30 labels by default, so without a high `--limit` the pause label can be
  cut off; and gh's text output is `name<TAB>description<TAB>color`, a
  line that never equals the label. Either way a paused repository would
  run with nothing recorded.
- `open_sweep_prs(repo)`, the open-PR listing shared by the open-PR cap
  (guards.pr_cap, which holds the contract) and the sweep's idempotence
  decision, runs exactly `gh pr list --state open --limit 1000 --json
  headRefName,number,url,body,files,isCrossRepository` and maps each item
  with idempotence.sweep_pr_listing. `isCrossRepository` is the trust
  boundary (guards.pr_cap, "The trust boundary"): without it every record
  is of unknown provenance. It is NOT cached: the sweep lists twice per
  run, once for the cap and once for the decision, and the second must see
  what was opened or closed in between.

**Every failure raises GhError**: gh missing or not runnable, a non-zero
exit (not logged in, no GitHub remote, an outage), no answer within
GH_TIMEOUT (the whole process group is killed, git_guard.run_in_group),
a PR listing that is not a JSON list of objects, and a listing as long as
its `--limit`, which may have been cut short. The label lines are taken
as gh printed them, without a shape check (a leading byte-order mark
dropped, split at "\n" only, a trailing "\r" dropped): KillSwitch compares
them stripped and casefolded, and anything that is not the pause label is
just another label. The trade-off of `--jq`, accepted: output that exits
0 but is not label names (a shim printing a banner, say) cannot be told
from label names, and reads as a repository without the pause label.
WHY raise and not return nothing: an empty answer reads as "no pause label" and "no open PRs",
which would fail open. A raise degrades the kill switch's label check and
the cap (a `degraded` guard line, the job still runs) and refuses the
sweep's decision (`idempotence`): never silence. WHY a timeout here: a gh
that hangs until the scheduler kills the run leaves no line at all.

**A repository at the limit never sweeps.** One with 1000 or more open PRs
(PR_LIMIT) has every listing refused as possibly cut short, so the cap
degrades and the sweep's decision refuses, run after run; one with 1000 or
more labels (LABEL_LIMIT) has its pause label never checked (degraded).
Both are stated on every line, never silent.

**What an error quotes.** gh's stderr, or output that could not be read,
lands in the GhError's message and from there on a ledger line and in a
terminal. It is quoted through redaction.redact (a token gh echoed is
replaced), with control characters shown escaped (an ANSI colour code
becomes `\x1b[...`, a newline `\n`), and cut to _CLIP characters.

**The environment** is the inherited one with these changes:
- No GIT_* (a GIT_DIR from a hook would point gh at another repository)
  and no GH_REPO, which would redirect both queries to the repository it
  names.
- No GH_FORCE_TTY, and CLICOLOR_FORCE=0 and NO_COLOR=1. WHY: with
  GH_FORCE_TTY or CLICOLOR_FORCE=1 set, gh 2.86.0 colours its `--json`
  output even into a pipe (`\033[1;38m[...`), which is not JSON, so every
  listing would fail and every sweep refuse. Observed read-only: NO_COLOR=1
  does not undo either, CLICOLOR_FORCE=0 undoes CLICOLOR_FORCE=1, and
  GH_FORCE_TTY colours whatever else is set, so it is removed. The
  `--jq` output is not coloured either way (`CLICOLOR_FORCE=1 gh label
  list --json name --jq '.[].name' | od -c` and the same with
  GH_FORCE_TTY=1 printed the plain names, 2026-10-23), so the label query
  would survive; the PR listing would not.
- GH_PROMPT_DISABLED=1, so gh fails rather than waits for input nobody
  will give.
gh's own credentials (its keyring, GH_TOKEN, GITHUB_TOKEN) are left as
they are: in CI and on a laptop gh authenticates the way the developer
set it up.

**Which repository.** With GH_REPO gone, gh resolves the base repository
from `gh repo set-default`, else by remote priority (upstream, then
github, then origin). In a fork's clone that may not be the remote the
sweep pushes to, so the labels and PRs read here could be another
repository's than the one the PR lands in. So both queries take
`gh_repo`, `[HOST/]OWNER/REPO` as publish.origin_gh_repo names origin's
repository, and pass it as `--repo`, which gh obeys over every remote.
The sweep resolves it once (sweep_job.resolve_sweep_base) for the
decision's listing and for publish, which refuses to push unless it is
still origin's (publish's docstring, "Which repository"). The cap's
listing and the label check resolve the same origin_gh_repo themselves
when the guard calls them (curamoir.runner, "Which repository gh is asked
about"); the label check alone goes without it when origin names no
GitHub repository, and gh then resolves as above (the cm-fkd.10
behaviour). `--repo` goes after the subcommand, where gh accepts it like
any other flag.

`GH` is the executable, a module attribute so tests can point it at a fake
gh (tests/fake_gh.py).
"""

import json
import re
import subprocess

from curamoir.git_guard import run_in_group, scrubbed_environment
from curamoir.idempotence import sweep_pr_listing
from curamoir.redaction import redact

#: The gh executable (the seam tests replace).
GH = "gh"

#: Seconds one gh call may take before its process group is killed.
GH_TIMEOUT = 120

#: `--limit` for the label list: far above any repository's label count. A
#: listing this long may be cut short, and raises.
LABEL_LIMIT = 1000

#: `--limit` for the open-PR list, by the same rule.
PR_LIMIT = 1000

#: Removed from gh's environment, and set in it (module docstring, "The
#: environment").
_UNSET = ("GH_REPO", "GH_FORCE_TTY")
_SET = {"CLICOLOR_FORCE": "0", "NO_COLOR": "1", "GH_PROMPT_DISABLED": "1"}

#: The fields every reader of the open-PR listing needs (guards.pr_cap).
PR_FIELDS = "headRefName,number,url,body,files,isCrossRepository"

#: How much of gh's stderr an error quotes: it lands on a ledger line.
_CLIP = 300


class GhError(Exception):
    """gh could not answer (module docstring, "Every failure raises")."""


class GhTimeout(GhError):
    """gh did not finish within its timeout and was stopped. For a write
    the outcome is unknown (run_gh)."""


def run_gh(args, *, cwd, timeout=None):
    """Run `GH args` in `cwd` and return its stdout as text. `timeout`
    defaults to GH_TIMEOUT. GhError on every failure (module docstring),
    GhTimeout (a GhError) when the timeout stopped it.

    The entry point for every gh call, publish's write (`gh pr create`)
    included. For a write, a GhTimeout means the outcome is unknown: gh
    was stopped, not refused, so the PR may or may not exist and the
    caller must find out before it records either. gh's stdin is
    /dev/null (run_in_group), so `--body-file -` reads an empty body: pass
    a file. The command an error shows is the argv joined by spaces,
    unquoted and unredacted: keep free text out of `args`."""
    timeout = GH_TIMEOUT if timeout is None else timeout
    argv = [GH, *args]
    shown = "gh " + " ".join(args)
    environment = gh_environment()
    try:
        completed = run_in_group(argv, cwd=cwd, env=environment, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GhTimeout(f"`{shown}` did not finish within {timeout} second(s) and was "
                        "stopped") from None
    except OSError as error:
        raise GhError(f"could not run `{shown}` ({error}); is the GitHub CLI installed "
                      "and on PATH?") from None
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise GhError(f"`{shown}` exited {completed.returncode}: {_clip(stderr) or 'no message'}")
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GhError(f"`{shown}` printed output that is not UTF-8 ({error})") from None


def gh_environment():
    """The environment gh runs with (module docstring, "The environment")."""
    environment = scrubbed_environment()
    for name in _UNSET:
        environment.pop(name, None)
    environment.update(_SET)
    return environment


def repo_option(gh_repo):
    """`--repo gh_repo` as arguments, or none when `gh_repo` is None
    (module docstring, "Which repository")."""
    return [] if gh_repo is None else ["--repo", gh_repo]


def repo_labels(repo, gh_repo=None):
    """The repository's label names, for guards.kill_switch: a list of str.
    `gh_repo` names the repository (module docstring, "Which repository")."""
    printed = run_gh(["label", "list", *repo_option(gh_repo), "--limit", str(LABEL_LIMIT),
                      "--json", "name", "--jq", ".[].name"], cwd=repo)
    # "\n" only: splitlines() also splits at \x0b, \x0c, U+2028 and others,
    # which can occur inside a label name and could make "curamoir:paused"
    # out of a label that is not it.
    lines = (line.removesuffix("\r") for line in printed.removeprefix("\ufeff").split("\n"))
    names = [line for line in lines if line]
    if len(names) >= LABEL_LIMIT:
        raise GhError(f"gh listed {len(names)} labels, its --limit, so the list may be cut "
                      "short and the pause label may be the one left off")
    return names


def open_sweep_prs(repo, gh_repo=None):
    """The repository's open pull requests as the open-PR listing contract's
    records (guards.pr_cap), each mapped by idempotence.sweep_pr_listing.
    `gh_repo` names the repository (module docstring, "Which repository")."""
    printed = run_gh(["pr", "list", *repo_option(gh_repo), "--state", "open",
                      "--limit", str(PR_LIMIT), "--json", PR_FIELDS], cwd=repo)
    try:
        items = json.loads(printed)
    except ValueError as error:
        raise GhError(f"gh pr list printed something that is not JSON ({error}): "
                      f"{_clip(printed)}") from None
    if not (isinstance(items, list) and all(isinstance(item, dict) for item in items)):
        raise GhError(f"gh pr list printed {_clip(printed)}, not a list of pull requests")
    if len(items) >= PR_LIMIT:
        raise GhError(f"gh listed {len(items)} open pull requests, its --limit, so the "
                      "list may be cut short")
    return [sweep_pr_listing(item) for item in items]


#: Characters an error message shows escaped: C0 and C1 controls, and the
#: line and paragraph separators.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _clip(text):
    """`text` (gh's output) as an error may quote it (module docstring, "What
    an error quotes"): redacted before it is cut, so a cut cannot leave half
    a token unrecognised."""
    text = _CONTROL.sub(lambda match: repr(match.group())[1:-1], redact(text.strip()))
    return text if len(text) <= _CLIP else text[:_CLIP] + "...(clipped)"


#: _clip for other gh callers (curamoir.publish quotes git's stderr the same
#: way): redacted, controls escaped, cut to _CLIP characters.
quote_output = _clip
