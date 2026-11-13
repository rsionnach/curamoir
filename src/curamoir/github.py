"""The `gh` adapter: the repository's labels and its pull requests.

Three read-only queries, below. Two are the callables the guard layer is
given (cm-fkd.10, discharging fix_scope's runner steps 0 (ii) and (iii));
the third is the brief's. All go through `run_gh`, the one way cúramóir
runs gh. curamoir.publish (cm-fkd.12) uses `run_gh` for its write too
(`gh pr create`); there a GhTimeout means the outcome is UNKNOWN: the PR
may or may not exist.

Each query has two paths, and its `github_api` argument picks one. That
argument is a config.GithubApi, decided once at the entry point
(runner.run_job, cli's `brief`) and passed down; nothing here reads the
environment for it. The CLI path (GithubApi.CLI, the default) runs the
commands below, as at 3f50a3f: a laptop's and GitHub Actions'. The REST
path (GithubApi.REST) asks the same questions through `gh api`, for a
Claude Code cloud session ("The REST path", at the end).

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

- `pr_states(repo)`, the brief's (cm-fkd.9): which of the sweep PRs the
  ledger records are open, merged or closed. Unlike the other two it
  does NOT raise at the limit: gh lists newest first, so a cut-short
  listing only leaves out old PRs, and a PR it leaves out reads as
  "state unknown" in the brief, which is true.

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

**The REST path** (cm-2qp.9).

Why. In a Claude Code cloud session (the entry point saw
CLAUDE_CODE_REMOTE=true, config.resolve_github_api) the GitHub proxy
rejects every request to GitHub's GraphQL endpoint with a 403 ("GitHub
GraphQL is not available from Claude Code sessions"), and `gh pr`, `gh
label list` and `gh repo view` all use GraphQL. `gh api
repos/{owner}/{repo}/...`, GitHub's REST API, is the documented fallback
(https://code.claude.com/docs/en/cloud-environments, "GitHub proxy").

How the repository is named (rest_target). Every call is `gh api
[--hostname HOST] repos/OWNER/REPO/...`, with HOST, OWNER and REPO taken
from `gh_repo`: the same origin_gh_repo value the CLI path passes as
`--repo`. A query asked without `gh_repo` (the label check, when origin
names no GitHub repository) uses gh's own `repos/{owner}/{repo}`
placeholder, which gh fills from the remotes, as it picks a repository on
the CLI path. That call names no host, so gh asks its default host
(github.com unless GH_HOST says otherwise): on GitHub Enterprise it may
ask the wrong server, fail, and the label check is degraded, never "not
paused".

Each query, mapped to the CLI path's shape so no caller changes:
- Every listing is read page by page by hand (`per_page=100&page=<n>`,
  _rest_pages), stopping at a short page or once the CLI path's `--limit`
  is reached. Not `--paginate`, which reads every page the repository has
  (every PR it ever had) before any limit can apply, nor `--slurp`, which
  needs gh 2.48 or later. Each page must be a JSON list of objects; HTML,
  an error object or any other answer at exit 0 is a GhError.
- labels: `labels?...`, each label's `name`. LABEL_LIMIT holds as on the
  CLI path: a listing of LABEL_LIMIT or more raises, so the pause label
  is never checked (degraded), never "not paused". A page that fails is
  an error, never the names before it.
- open PRs: `pulls?state=open&...`, each pull mapped by rest_pull_fields
  to the `gh pr list --json` item (headRefName from head.ref, url from
  html_url, isCrossRepository from the head and base repositories' ids,
  rest_cross_repository). PR_LIMIT holds as on the CLI path. REST's list
  has no files, so `pulls/<n>/files?per_page=GH_FILES_CAP` is asked, one
  page, as GraphQL's `files` is the first GH_FILES_CAP; and only for a
  same-repository PR whose body carries a sweep marker, the only PRs
  whose paths any reader uses (idempotence._account uses `paths` only
  beside a trusted marker's `base`; the cap and status read branches).
  Any other PR has `files` None, so `paths` None, which is what it
  already meant.
- PR states: `pulls?state=all&...`, the newest PR_LIMIT, as `gh pr list
  --limit` gives them. `state` maps to the CLI's words: open is OPEN,
  closed with a `merged_at` is MERGED, closed without is CLOSED.
curamoir.publish's default branch, create and lookup take the same
`github_api` (its "The REST path"). A rate limit (403 or 429) is a
non-zero exit like any other: a GhError, with no retry.

Never read here, on either path: the value of GITHUB_TOKEN or GH_TOKEN.
In a cloud session both read as the placeholder `proxy-injected`, and the
proxy substitutes the real credential on the way out; gh_environment
passes them through as before.

`GH` is the executable, a module attribute so tests can point it at a fake
gh (tests/fake_gh.py).
"""

import contextlib
import json
import re
import subprocess
import tempfile
from urllib.parse import quote

from curamoir.config import GithubApi
from curamoir.git_guard import run_in_group, scrubbed_environment
from curamoir.idempotence import GH_FILES_CAP, parse_sweep_marker, sweep_pr_listing
from curamoir.ledger import parse_ledger_timestamp
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

#: The fields pr_states asks for.
STATE_FIELDS = "headRefName,number,state,mergedAt"

#: The fields every reader of the open-PR listing needs (guards.pr_cap).
PR_FIELDS = "headRefName,number,url,body,files,isCrossRepository"

#: How much of gh's stderr an error quotes: it lands on a ledger line.
_CLIP = 300

#: GitHub's largest page (REST `per_page`), which every REST listing asks
#: for (module docstring, "Each query").
REST_PER_PAGE = 100

#: The `repos/OWNER/REPO` gh fills from the checkout's remotes, for a query
#: asked without `gh_repo` (rest_target).
_PLACEHOLDER = "repos/{owner}/{repo}"


class GhError(Exception):
    """gh could not answer (module docstring, "Every failure raises")."""


class GhTimeout(GhError):
    """gh did not finish within its timeout and was stopped. For a write
    the outcome is unknown (run_gh)."""


def run_gh(args, *, cwd, timeout=None, stdin_bytes=None):
    """Run `GH args` in `cwd` and return its stdout as text. `timeout`
    defaults to GH_TIMEOUT. GhError on every failure (module docstring),
    GhTimeout (a GhError) when the timeout stopped it.

    The entry point for every gh call, publish's write (`gh pr create`, or
    `gh api -X POST .../pulls` on the REST path) included. For a write, a
    GhTimeout means the outcome is unknown: gh was stopped, not refused,
    so the PR may or may not exist and the caller must find out before it
    records either. gh's stdin is /dev/null (run_in_group), so
    `--body-file -` reads an empty body: pass a file, or `stdin_bytes`,
    which gh then reads as its stdin from an unnamed temporary file (the
    REST create's `--input -`). The command an error shows is the argv
    joined by spaces, unquoted and unredacted: keep free text out of
    `args`; `stdin_bytes` is never shown. An OSError making that file is
    raised as it is, not as a GhError: gh never ran."""
    timeout = GH_TIMEOUT if timeout is None else timeout
    argv = [GH, *args]
    shown = "gh " + " ".join(args)
    environment = gh_environment()
    with contextlib.ExitStack() as stack:
        # Made before gh runs and outside the `try` below: an OSError here
        # is the input's (no space, no temporary directory), not gh's, and
        # reaches the caller as an OSError, meaning gh never ran.
        stdin = subprocess.DEVNULL if stdin_bytes is None else stack.enter_context(
            _stdin_file(stdin_bytes))
        try:
            completed = run_in_group(argv, cwd=cwd, env=environment, timeout=timeout,
                                     stdin=stdin)
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


@contextlib.contextmanager
def _stdin_file(data):
    """An unnamed temporary file holding `data`, at its start: gh's stdin
    for `stdin_bytes` (unlinked as it is made, on POSIX, so the body is
    never in a path another process could open by name)."""
    with tempfile.TemporaryFile() as handle:
        handle.write(data)
        handle.seek(0)
        yield handle


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


def repo_labels(repo, gh_repo=None, *, github_api=GithubApi.CLI):
    """The repository's label names, for guards.kill_switch: a list of str.
    `gh_repo` names the repository (module docstring, "Which repository");
    `github_api` is the run's config.GithubApi (module docstring, "The REST
    path")."""
    if github_api is GithubApi.REST:
        names = _rest_label_names(repo, gh_repo)
    else:
        names = _cli_label_names(repo, gh_repo)
    if len(names) >= LABEL_LIMIT:
        raise GhError(f"gh listed {len(names)} labels, "
                      f"{_limit_phrase(github_api, 'LABEL_LIMIT', LABEL_LIMIT)}, so the list may "
                      "be cut short and the pause label may be the one left off")
    return names


def _limit_phrase(github_api, name, limit):
    """How a refusal at the limit names it: the CLI path's `--limit`, or on
    the REST path (which passes none) the constant `name` and its value."""
    return f"as many as {name} ({limit})" if github_api is GithubApi.REST else "its --limit"


def _cli_label_names(repo, gh_repo):
    """repo_labels on the CLI path: `gh label list`'s names, as printed."""
    printed = run_gh(["label", "list", *repo_option(gh_repo), "--limit", str(LABEL_LIMIT),
                      "--json", "name", "--jq", ".[].name"], cwd=repo)
    # "\n" only: splitlines() also splits at \x0b, \x0c, U+2028 and others,
    # which can occur inside a label name and could make "curamoir:paused"
    # out of a label that is not it.
    lines = (line.removesuffix("\r") for line in printed.removeprefix("\ufeff").split("\n"))
    return [line for line in lines if line]


def _rest_label_names(repo, gh_repo):
    """repo_labels on the REST path: every label's `name`, page by page up
    to LABEL_LIMIT (_rest_pages). GhError for a label without a text name:
    a listing that cannot be read is never read as "not paused"."""
    labels = _rest_pages(repo, gh_repo, "labels", "", LABEL_LIMIT)
    names = [label.get("name") for label in labels]
    if not all(isinstance(name, str) for name in names):
        raise GhError(f"gh api .../labels listed a label without a name: "
                      f"{_clip(json.dumps(labels))}")
    return names


def open_sweep_prs(repo, gh_repo=None, *, github_api=GithubApi.CLI):
    """The repository's open pull requests as the open-PR listing contract's
    records (guards.pr_cap), each mapped by idempotence.sweep_pr_listing.
    `gh_repo` names the repository (module docstring, "Which repository");
    `github_api` is the run's config.GithubApi (module docstring, "The REST
    path")."""
    if github_api is GithubApi.REST:
        items = _rest_open_prs(repo, gh_repo)
    else:
        printed = run_gh(["pr", "list", *repo_option(gh_repo), "--state", "open",
                          "--limit", str(PR_LIMIT), "--json", PR_FIELDS], cwd=repo)
        items = _json_pulls(printed, "gh pr list")
    if len(items) >= PR_LIMIT:
        raise GhError(f"gh listed {len(items)} open pull requests, "
                      f"{_limit_phrase(github_api, 'PR_LIMIT', PR_LIMIT)}, so the list may be "
                      "cut short")
    return [sweep_pr_listing(item) for item in items]


def pr_states(repo, gh_repo=None, *, github_api=GithubApi.CLI):
    """{number: {"branch", "state", "merged_at"}} for the repository's pull
    requests in any state, newest PR_LIMIT of them (module docstring):
    `state` as gh gives it ("OPEN", "MERGED", "CLOSED"), `merged_at` an
    aware datetime or None. An item without a whole `number` or a string
    `state` is left out; a `mergedAt` that is not a timestamp reads as
    None. GhError when gh fails or prints something other than a list.
    `github_api` is the run's config.GithubApi: on REST the same answer, from
    rest_pull_fields (module docstring, "The REST path")."""
    if github_api is GithubApi.REST:
        items = _rest_recent_prs(repo, gh_repo)
    else:
        printed = run_gh(["pr", "list", *repo_option(gh_repo), "--state", "all",
                          "--limit", str(PR_LIMIT), "--json", STATE_FIELDS], cwd=repo)
        items = _json_pulls(printed, "gh pr list")
    states = {}
    for item in items:
        number, state = item.get("number"), item.get("state")
        if isinstance(number, bool) or not isinstance(number, int) or not isinstance(state, str):
            continue
        states[number] = {"branch": item.get("headRefName"), "state": state,
                          "merged_at": parse_ledger_timestamp(item.get("mergedAt"))}
    return states


def _json_pulls(printed, command):
    """`printed` as a JSON list of objects; GhError, quoting it, when it is
    not one. `command` names what printed it."""
    try:
        items = json.loads(printed)
    except ValueError as error:
        raise GhError(f"{command} printed something that is not JSON ({error}): "
                      f"{_clip(printed)}") from None
    if not (isinstance(items, list) and all(isinstance(item, dict) for item in items)):
        raise GhError(f"{command} printed {_clip(printed)}, not a list of pull requests")
    return items


# --- the REST path (cm-2qp.9) ----------------------------------------------------------

def rest_target(gh_repo, *, placeholder=False):
    """(`--hostname HOST` as arguments, `repos/OWNER/REPO`) for `gh api`.
    Two rules:
    - `gh_repo` as publish.origin_gh_repo names it, `HOST/OWNER/REPO`,
      gives `--hostname HOST` and `repos/OWNER/REPO`; `OWNER/REPO` gives
      no `--hostname` (gh's default host).
    - None, when `placeholder` allows it (the queries here, which take
      None as the CLI path does; never publish, which always names the
      repository), gives no `--hostname` and gh's own
      `repos/{owner}/{repo}`, which gh fills from the remotes (module
      docstring, "How the repository is named").
    Anything else is a GhError, before gh runs (rest_repository)."""
    if gh_repo is None and placeholder:
        return [], _PLACEHOLDER
    hostname, owner, name = rest_repository(gh_repo)
    return hostname, f"repos/{owner}/{name}"


def rest_repository(gh_repo):
    """rest_target's first rule, the target parsed: (`--hostname HOST` as
    arguments, OWNER, REPO) for a `gh_repo` that is `HOST/OWNER/REPO` or
    `OWNER/REPO`; GhError for anything else, before gh runs."""
    parts = gh_repo.split("/") if isinstance(gh_repo, str) else []
    if len(parts) == 2:
        parts = [None, *parts]
    if len(parts) != 3 or not all(parts[1:]) or parts[0] == "":
        raise GhError(f"{gh_repo!r} names no GitHub repository as HOST/OWNER/REPO, so gh api "
                      "cannot be asked about it")
    host, owner, name = parts
    return ([] if host is None else ["--hostname", host]), owner, name


def rest_pull_fields(item):
    """A REST pull request object (`gh api repos/O/R/pulls...`) as the
    `gh pr list --json` item the CLI path reads, so every reader below is
    the same on both paths: `number`, `url` (html_url), `body`, `title`,
    `headRefName` (head.ref), `headRefOid` (head.sha), `baseRefName`
    (base.ref), `state` ("OPEN", "MERGED" for a closed PR with a
    `merged_at`, "CLOSED"; None for anything else), `mergedAt`
    (merged_at), `isCrossRepository` (rest_cross_repository). A field the
    object lacks, or holds in another shape, is None; nothing raises."""
    head = item.get("head") if isinstance(item.get("head"), dict) else {}
    base = item.get("base") if isinstance(item.get("base"), dict) else {}
    state, merged_at = item.get("state"), item.get("merged_at")
    if state == "open":
        word = "OPEN"
    elif state == "closed":
        word = "MERGED" if isinstance(merged_at, str) else "CLOSED"
    else:
        word = None
    return {"number": item.get("number"), "url": item.get("html_url"),
            "body": item.get("body"), "title": item.get("title"),
            "headRefName": head.get("ref"), "headRefOid": head.get("sha"),
            "baseRefName": base.get("ref"), "state": word, "mergedAt": merged_at,
            "isCrossRepository": rest_cross_repository(head, base)}


def rest_cross_repository(head, base):
    """gh's `isCrossRepository` from a REST pull's `head` and `base`: True
    when the head repository is another repository (by GitHub's numeric
    `id`, which a rename does not change) or is gone (`head.repo` null: a
    fork deleted since, which `gh pr view --json isCrossRepository` calls
    true, tests/gh_api_payloads.py); False when it is the base repository;
    None (unknown provenance, guards.pr_cap's "The trust boundary") when
    either id is missing or not a whole number."""
    if "repo" in head and head["repo"] is None:
        return True
    head_repo, base_repo = head.get("repo"), base.get("repo")
    ids = [repository.get("id") if isinstance(repository, dict) else None
           for repository in (head_repo, base_repo)]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in ids):
        return None
    return ids[0] != ids[1]


def rest_json(repo, args, command, *, timeout=None, stdin_bytes=None):
    """`gh api args` run, its stdout parsed as JSON; GhError (quoting it)
    when it is not JSON. `command` names the call in that message."""
    printed = run_gh(["api", *args], cwd=repo, timeout=timeout, stdin_bytes=stdin_bytes)
    try:
        return json.loads(printed)
    except ValueError as error:
        raise GhError(f"{command} printed something that is not JSON ({error}): "
                      f"{_clip(printed)}") from None


def _rest_pages(repo, gh_repo, resource, query, limit):
    """The items of a REST listing of `resource` (`labels`, `pulls`) with
    `query` (`state=open&`, or ""): page by page,
    `per_page=REST_PER_PAGE&page=<n>`, from page 1 always,
    stopping at a short page or once `limit` or more items are read, so at
    most ceil(limit / REST_PER_PAGE) pages are asked (module docstring,
    "Each query"). WHY "or more" and no page count: a listing that reaches
    `limit` must come back with at least `limit` items for the caller's
    `>= limit` check to refuse it, whatever `limit` is (below one page, or
    not a multiple of one); a loop that never ran would hand back an empty
    list that reads as "no labels" and "no PRs". GhError when gh fails or
    a page is not a JSON list of objects."""
    hostname, path = rest_target(gh_repo, placeholder=True)
    items = []
    page = 1
    while True:
        listed = rest_json(repo, [*hostname, f"{path}/{resource}?{query}"
                                             f"per_page={REST_PER_PAGE}&page={page}"],
                           f"gh api .../{resource}")
        if not (isinstance(listed, list) and all(isinstance(item, dict) for item in listed)):
            raise GhError(f"gh api .../{resource} printed {_clip(json.dumps(listed))}, not a "
                          "list of objects")
        items += listed
        if len(listed) < REST_PER_PAGE or len(items) >= limit:
            return items
        page += 1


def _rest_open_prs(repo, gh_repo):
    """open_sweep_prs's items on the REST path: the open PRs up to PR_LIMIT
    (_rest_pages; open_sweep_prs then raises at the limit, as on the CLI
    path), as rest_pull_fields gives them, with `files` for those whose
    paths a reader uses (module docstring, "Each query")."""
    pulls = _rest_pages(repo, gh_repo, "pulls", "state=open&", PR_LIMIT)
    hostname, path = rest_target(gh_repo, placeholder=True)
    items = []
    for pull in pulls:
        item = rest_pull_fields(pull)
        number = item["number"]
        if (item["isCrossRepository"] is False and parse_sweep_marker(item["body"]) is not None
                and isinstance(number, int) and not isinstance(number, bool) and number > 0):
            item["files"] = _rest_files(repo, hostname, path, number)
        items.append(item)
    return items


def _rest_files(repo, hostname, path, number):
    """PR `number`'s first GH_FILES_CAP files as the CLI's `files` items
    (`{"path": filename}`): one page, as GraphQL's `files` is, so a PR with
    GH_FILES_CAP or more reads as possibly cut short (sweep_pr_listing)."""
    files = rest_json(repo, [*hostname, f"{path}/pulls/{number}/files?per_page={GH_FILES_CAP}"],
                      "gh api .../files")
    if not isinstance(files, list):
        raise GhError(f"gh api .../files printed {_clip(json.dumps(files))}, not a list of "
                      "files")
    return [{"path": entry.get("filename")} if isinstance(entry, dict) else entry
            for entry in files]


def _rest_recent_prs(repo, gh_repo):
    """pr_states's items on the REST path: the newest PR_LIMIT PRs in any
    state (_rest_pages), as rest_pull_fields gives them."""
    pulls = _rest_pages(repo, gh_repo, "pulls", "state=all&", PR_LIMIT)
    return [rest_pull_fields(pull) for pull in pulls]


def rest_head_query(owner, branch):
    """The `head=OWNER:BRANCH` query value for `owner`'s own branch (OWNER
    as rest_repository parses it), percent-encoded as UTF-8 (a branch name
    may hold `&`, `#`, `+` or any character git allows)."""
    return quote(f"{owner}:{branch}", safe=":/")


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
