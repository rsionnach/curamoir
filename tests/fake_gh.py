"""A fake `gh` executable for curamoir.github's seam (`github.GH`), and the
`gh` first on PATH for every test (tests/conftest.py, `no_real_gh`).

It is a real script, run as a real process through the adapter's real
subprocess path, so the argv the adapter builds, its timeout and its error
handling are what is tested. It answers the two read-only queries the
adapter makes, and nothing else, and fails where gh 2.86.0 fails
(tests/gh_label_payloads.py and tests/gh_pr_payloads.py hold the recorded
output it is fed and the provenance of each behaviour below):

- `gh label list ... --json <fields> [--jq '.[].name']`: the label objects
  given, cut to `--limit`, each holding only the fields asked for; with
  that `--jq`, their names one per line, each followed by a newline, which
  is gh's output for that command as recorded in
  gh_label_payloads.LABEL_NAMES_JQ (and checked against it in
  tests/test_github.py). Any other `--jq` is refused rather than guessed at.
- `gh pr list --state open ... --json <fields>`: the PR items given, cut to
  `--limit`, each holding ONLY the fields asked for, as gh's `--json` does.
  An item carrying a `state` other than "OPEN" (gh_pr_payloads.STATE_PRS)
  is left out; `--state all` lists every item.
  So an adapter that forgets `isCrossRepository` gets items without it.
- With no `--limit`, at most 30 items: gh's default for both commands
  (`gh label list --help`, `gh pr list --help`: "--limit int ... (default
  30)"), which is why the adapter's high `--limit` matters.
- A `--json` field gh does not have exits 1 with gh's own message
  (`Unknown JSON field: "<name>"`, then the available fields), observed
  read-only with `gh label list --json nosuch` and `gh pr list --state
  open --json headRefName,nosuch`; the field lists below are the ones
  those two commands printed.
- With GH_FORCE_TTY set (any non-empty value) or CLICOLOR_FORCE set to
  anything but "0", its `--json` output is wrapped in ANSI colour codes,
  so it is no longer JSON: real gh 2.86.0 does that into a pipe, observed
  read-only with `CLICOLOR_FORCE=1 gh pr list --json number`
  (`\033[1;38m[\033[m...`), and NO_COLOR=1 does not stop it. The `--jq`
  output stays plain: `CLICOLOR_FORCE=1 gh label list --json name --jq
  '.[].name' | od -c | head`, and the same with GH_FORCE_TTY=1, printed
  `a c c e s s i b i l i t y \n b u g \n ...` with no escape byte
  (observed 2026-10-23, against this repository). The
  fake only wraps the whole output in one colour code, which is enough to
  make it unparseable as gh's is; gh also colours each token.
- Without `--json` gh prints a text table, which is never recorded here:
  the fake refuses it, as the adapter must never parse it.
- `gh pr list ... --head <branch>`: only the items whose `headRefName` is
  that branch (`gh pr list --help`: "-H, --head string  Filter by head
  branch"), as curamoir.publish's find_open_pr asks after a `pr create`
  that may have opened the PR without saying so.
- `gh pr create --repo <[HOST/]OWNER/REPO> --head <b> --base <b> --title
  <t> --body-file <path>`: prints the new PR's URL and a newline on
  stdout, which is all `gh pr create --help` (2.86.0) promises: "Upon
  success, the URL of the created pull request will be printed." The URL
  is `https://<host>/<owner>/<repo>/pull/<number>`, host github.com when
  `--repo` names none, numbers counting up from `next_number`. The
  created PR is added to `prs` (headRefName, number, url, body, files [],
  isCrossRepository false), so a later `pr list` sees it. The fake
  refuses a create without `--repo`, `--head`, `--base`, `--title` or
  `--body-file`: real gh would prompt or guess (the current branch, the
  remote it resolves) where cúramóir must not let it. `--body-file -`
  reads stdin as real gh does, which the adapter's processes get as
  /dev/null: the trap the adapter's temporary file avoids. The created
  PR's `headRefOid` is what origin's push URL holds for the branch (`git
  ls-remote`, run in the directory gh was run in, as GitHub knows the head
  it was given; None if that fails), and its `baseRefName` the `--base`.
  How a create can go wrong is FakeGh.feed's `create_*` options.
- `gh repo view <[HOST/]OWNER/REPO> --json defaultBranchRef`: the
  repository's default branch as `{"defaultBranchRef": {"name": "<b>"}}`
  and a newline, the shape gh 2.86.0 printed read-only for `gh repo view
  github.com/rsionnach/curamoir --json defaultBranchRef` (2026-10-26),
  which also showed that the HOST/OWNER/REPO positional is accepted and
  that an unknown field exits 1 with `Unknown JSON field`. The name is
  `default_branch`. Without the positional the fake refuses: real gh
  would pick the repository from the remotes, which publish never lets it.

- `gh api [--hostname H] <endpoint>` (cm-2qp.9, the REST path a Claude Code cloud session takes;
  tests/gh_api_payloads.py records GitHub's answers): `repos/O/R` (the
  recorded repository object, its `default_branch` the fed one),
  `repos/O/R/labels` (the fed labels as REST label objects),
  `repos/O/R/pulls?state=open|all[&head=O:B]` (the fed PRs as REST pull
  objects, isCrossRepository True as a head repository of another id,
  False of the same id, missing as no `head.repo` at all; or `rest_pulls`
  verbatim), and `repos/O/R/pulls/N/files` (the PR's `files` as REST file
  objects; or `rest_files`). Pagination is GitHub's (`per_page`, default
  30, at most 100; `page` from 1) and gh's printing of it as observed
  (gh_api_payloads' docstring): one JSON array per page asked. Any other
  option (`--paginate`, `--slurp`, `--jq`) is refused as not recorded.
  `repos/{owner}/{repo}` stands for `placeholder_repo`, as real gh
  fills the placeholders from the remotes. `gh api -X POST repos/O/R/pulls
  --input -` reads the JSON body from stdin (logged as `input_content`),
  creates the PR as `pr create` does (the same `create_*` options) and
  prints it as a REST pull object.
- `proxy` (FakeGh.feed): every call that is not `gh api` exits 1 with
  the GitHub proxy's GraphQL refusal, as `gh pr`, `gh label list` and
  `gh repo view` do in a cloud session.

Every `--repo` value is taken as given and logged with the argv; the fake
answers every repository alike. gh accepts `--repo` after the
subcommand, where the adapter puts it, so `args[:2]` still names the
command.

Every call appends its argv, the directory it ran in (`cwd`: real gh picks
its repository from it), the LOGGED_VARIABLES it was given (None when
unset) and, when it was given `--body-file`, the text it read from it
(`body_file_content`, else None), and for `gh api --input`, the text it
read (`input_content`, else None) to `calls.jsonl` beside the script: the
body file is the adapter's temporary file, gone once the call returns, so
the log is the only record of what gh was sent. The payloads are re-read
on every call, so a test can change what gh "sees" between two calls.
The failure and timing options (`fail`, `fail_on`, `hang`, `raw_stdout`,
`create_*`) and the order they are checked in are FakeGh.feed's.
"""

import json
import stat
import sys
from pathlib import Path

from tests.gh_api_payloads import PROXY_GRAPHQL_REFUSAL, REPO

#: gh 2.86.0's `--json` fields, as its "Unknown JSON field" error lists them.
LABEL_FIELDS = ("color", "createdAt", "description", "id", "isDefault", "name",
                "updatedAt", "url")
PR_FIELDS = (
    "additions", "assignees", "author", "autoMergeRequest", "baseRefName", "baseRefOid",
    "body", "changedFiles", "closed", "closedAt", "closingIssuesReferences", "comments",
    "commits", "createdAt", "deletions", "files", "fullDatabaseId", "headRefName",
    "headRefOid", "headRepository", "headRepositoryOwner", "id", "isCrossRepository",
    "isDraft", "labels", "latestReviews", "maintainerCanModify", "mergeCommit",
    "mergeStateStatus", "mergeable", "mergedAt", "mergedBy", "milestone", "number",
    "potentialMergeCommit", "projectCards", "projectItems", "reactionGroups",
    "reviewDecision", "reviewRequests", "reviews", "state", "statusCheckRollup", "title",
    "updatedAt", "url")

#: The `gh repo view --json` fields the fake answers (gh has many more; any
#: other is refused as unknown, as gh refuses a field it lacks).
REPO_FIELDS = ("defaultBranchRef", "nameWithOwner")

#: The environment variables each call's log record carries.
LOGGED_VARIABLES = ("GIT_DIR", "GH_REPO", "GH_PROMPT_DISABLED", "GH_FORCE_TTY",
                    "CLICOLOR_FORCE", "NO_COLOR")

#: gh's `--limit` when none is given.
GH_DEFAULT_LIMIT = 30

_SCRIPT = '''#!{python}
import json, os, subprocess, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
args = sys.argv[1:]
body_file_content = None
if "--body-file" in args:
    source = args[args.index("--body-file") + 1]
    if source == "-":
        body_file_content = sys.stdin.read()
    else:
        body_file_content = Path(source).read_text(encoding="utf-8")
input_content = None
if args[:1] == ["api"] and "--input" in args:
    source = args[args.index("--input") + 1]
    if source == "-":
        input_content = sys.stdin.read()
    else:
        input_content = Path(source).read_text(encoding="utf-8")
with open(here / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": args, "cwd": os.getcwd(),
                          "body_file_content": body_file_content,
                          "input_content": input_content,
                          **{{name: os.environ.get(name) for name in {logged!r}}}}}) + "\\n")
state = json.loads((here / "state.json").read_text(encoding="utf-8"))
if state["hang"]:
    time.sleep(600)
if state["fail"] is not None:
    sys.stderr.write(state["fail"] + "\\n")
    sys.exit(1)
if " ".join(args[:2]) in state["fail_on"]:
    sys.stderr.write(state["fail_on"][" ".join(args[:2])] + "\\n")
    sys.exit(1)
if state["raw_stdout"] is not None:
    sys.stdout.buffer.write(bytes.fromhex(state["raw_stdout"]))
    sys.exit(0)
if state["proxy"] and args[:1] != ["api"]:
    # The GitHub proxy of a Claude Code cloud session: every GraphQL-backed
    # subcommand gets its 403 (tests/gh_api_payloads.py).
    sys.stderr.write({proxy_refusal!r} + "\\n")
    sys.exit(1)


def coloured(text):
    force = os.environ.get("CLICOLOR_FORCE")
    if os.environ.get("GH_FORCE_TTY") or force not in (None, "", "0"):
        return "\\033[1;38m" + text + "\\033[m"
    return text


def option(name):
    return args[args.index(name) + 1] if name in args else None


def refuse(message):
    sys.stderr.write(message + "\\n")
    sys.exit(1)


def select(items, known):
    asked = option("--json")
    if asked is None:
        refuse("fake gh: gh's text output is not recorded; ask for --json")
    fields = asked.split(",")
    for field in fields:
        if field not in known:
            refuse('Unknown JSON field: "' + field + '"\\nAvailable fields:\\n'
                   + "".join("  " + name + "\\n" for name in known).rstrip("\\n"))
    limit = int(option("--limit") or {default_limit})
    return [{{key: item[key] for key in fields if key in item}} for item in items[:limit]]


def listed_prs():
    """The PR items a listing sees: those not hidden, counting each hidden
    one's listings down (FakeGh.feed's `create_hidden_from_lists`)."""
    items = [item for item in state["prs"] if not item.get("hidden_from_lists")]
    if len(items) != len(state["prs"]):
        for item in state["prs"]:
            if item.get("hidden_from_lists"):
                item["hidden_from_lists"] -= 1
        (here / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return items


def create(host, owner, name, head, base, body, hidden):
    """Record a new PR from `head` into `base` and return its URL."""
    number = state["next_number"]
    url = "https://" + host + "/" + owner + "/" + name + "/pull/" + str(number)
    try:
        push_url = subprocess.run(["git", "remote", "get-url", "--push", "origin"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        listed = subprocess.run(["git", "ls-remote", push_url, "refs/heads/" + head],
                                capture_output=True, text=True, check=True).stdout.split()
        head_oid = listed[0] if listed else None
    except (OSError, subprocess.CalledProcessError):
        head_oid = None
    state["prs"].append({{"headRefName": head, "number": number, "url": url,
                         "body": body, "files": [],
                         "isCrossRepository": False, "hidden_from_lists": hidden,
                         "headRefOid": head_oid, "baseRefName": base}})
    state["next_number"] = number + 1
    (here / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return url


{api_handler}


if args[:1] == ["api"]:
    api(args)
elif args[:2] == ["label", "list"]:
    labels = select(state["labels"], {label_fields!r})
    jq = option("--jq")
    if jq is None:
        sys.stdout.write(coloured(json.dumps(labels)) + "\\n")
    elif jq == ".[].name":
        sys.stdout.write("".join(label["name"] + "\\n" for label in labels))
    else:
        refuse("fake gh: unrecorded --jq " + jq)
elif args[:2] == ["pr", "list"]:
    if option("--state") not in ("open", "all"):
        refuse("fake gh: only --state open and --state all are recorded")
    items = listed_prs()
    if option("--state") == "open":
        items = [item for item in items if item.get("state", "OPEN") == "OPEN"]
    if option("--head") is not None:
        items = [item for item in items if item.get("headRefName") == option("--head")]
    sys.stdout.write(coloured(json.dumps(select(items, {pr_fields!r}))) + "\\n")
elif args[:2] == ["pr", "create"]:
    for needed in ("--repo", "--head", "--base", "--title", "--body-file"):
        if option(needed) is None:
            refuse("fake gh: pr create without " + needed + " is not recorded")
    if state["create_hang"] == "before":
        time.sleep(600)
    hidden = 1 if state["create_hang"] == "late" else state["create_hidden_from_lists"]
    parts = option("--repo").split("/")
    host, owner, name = parts if len(parts) == 3 else ["github.com", *parts]
    url = create(host, owner, name, option("--head"), option("--base"), body_file_content,
                 hidden)
    if state["create_hang"] in ("after", "late"):
        time.sleep(600)
    if state["create_fails_with"] is not None:
        sys.stderr.write(state["create_fails_with"] + "\\n")
        sys.exit(1)
    sys.stdout.write(url + "\\n" if state["create_stdout"] is None else state["create_stdout"])
elif args[:2] == ["repo", "view"]:
    if len(args) < 3 or args[2].startswith("-"):
        refuse("fake gh: repo view without a repository is not recorded")
    asked = option("--json")
    if asked is None:
        refuse("fake gh: gh's text output is not recorded; ask for --json")
    known = {{"defaultBranchRef": {{"name": state["default_branch"]}},
              "nameWithOwner": "/".join(args[2].split("/")[-2:])}}
    for field in asked.split(","):
        if field not in {repo_fields!r}:
            refuse('Unknown JSON field: "' + field + '"')
    sys.stdout.write(coloured(json.dumps({{f: known[f] for f in asked.split(",")}})) + "\\n")
else:
    refuse("fake gh: no recording for " + " ".join(args))
'''


_API_HANDLER = r"""
#: gh api's options that take a value, and the base repository's id the
#: REST items carry (a fork's head repository gets FORK_ID).
API_VALUED = ("--hostname", "-X", "--method", "--input", "-H", "--header")
BASE_ID, FORK_ID = 1000, 2000


def rest_label(index, label):
    return {"id": index + 1, "node_id": "LA_fake" + str(index), "url": None,
            "name": label["name"], "color": label.get("color", "ededed"),
            "default": False, "description": label.get("description"),
            "archived_at": None, "archived_by": None}


def rest_pull(item, owner, name):
    # A CLI-shaped item (what `pr list --json` answers from) as a REST pull.
    full_name = owner + "/" + name
    head = {"label": owner + ":" + str(item.get("headRefName")),
            "ref": item.get("headRefName"), "sha": item.get("headRefOid")}
    cross = item.get("isCrossRepository")
    if cross is True:
        head["repo"] = {"id": FORK_ID, "full_name": "fork-owner/" + name}
    elif cross is False:
        head["repo"] = {"id": BASE_ID, "full_name": full_name}
    # isCrossRepository missing: no head.repo key at all (unknown).
    state_word = item.get("state", "OPEN")
    return {"number": item.get("number"), "html_url": item.get("url"),
            "title": item.get("title", ""), "body": item.get("body"),
            "state": "open" if state_word == "OPEN" else "closed",
            "merged_at": item.get("mergedAt"), "head": head,
            "base": {"label": owner + ":" + str(item.get("baseRefName")),
                     "ref": item.get("baseRefName"), "sha": None,
                     "repo": {"id": BASE_ID, "full_name": full_name}}}


def api(args):
    import urllib.parse
    opts, positional = {}, []
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in API_VALUED:
            opts[arg] = args[index + 1]
            index += 2
        elif arg.startswith("-"):
            # --paginate, --slurp, --jq and the rest: the adapter pages by
            # hand and passes none of them (curamoir.github).
            refuse("fake gh: api " + arg + " is not recorded")
        else:
            positional.append(arg)
            index += 1
    if len(positional) != 1:
        refuse("fake gh: api with endpoints " + repr(positional) + " is not recorded")
    endpoint = positional[0]
    method = opts.get("-X") or opts.get("--method") or ("POST" if "--input" in opts else "GET")
    for needle, message in state["api_fail_on"].items():
        if needle in method + " " + endpoint:
            sys.stderr.write(message + "\n")
            sys.exit(1)
    path, _, query = endpoint.partition("?")
    params = dict(urllib.parse.parse_qsl(query))
    parts = path.split("/")
    if parts[:1] != ["repos"] or len(parts) < 3:
        refuse("fake gh: api " + endpoint + " is not recorded")
    owner, name = parts[1], parts[2]
    if (owner, name) == ("{owner}", "{repo}"):
        # Real gh fills these from the checkout's remotes.
        owner, name = state["placeholder_repo"].split("/")
    host = opts.get("--hostname", "github.com")
    rest = parts[3:]
    if method == "POST" and rest == ["pulls"]:
        return api_create(host, owner, name, opts)
    if method != "GET":
        refuse("fake gh: api -X " + method + " " + endpoint + " is not recorded")
    if rest == []:
        repository = dict(state["rest_repo"])
        repository.update({"name": name, "full_name": owner + "/" + name,
                           "default_branch": state["default_branch"]})
        sys.stdout.write(json.dumps(repository))
        return
    if rest == ["labels"]:
        return pages([rest_label(i, label) for i, label in enumerate(state["labels"])], params)
    if rest == ["pulls"]:
        if state["rest_pulls"] is not None:
            items = state["rest_pulls"]
        else:
            items = [rest_pull(item, owner, name) for item in listed_prs()]
        wanted = params.get("state", "open")
        if wanted not in ("open", "all"):
            refuse("fake gh: only state=open and state=all are recorded")
        if wanted == "open":
            items = [item for item in items if item.get("state") == "open"]
        if "head" in params:
            branch = params["head"].split(":", 1)[-1]
            items = [item for item in items if (item.get("head") or {}).get("ref") == branch]
        return pages(items, params)
    if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "files":
        if rest[1] in state["rest_files"]:
            files = state["rest_files"][rest[1]]
        else:
            found = [item for item in state["prs"] if str(item.get("number")) == rest[1]]
            if not found:
                sys.stdout.write(json.dumps({"message": "Not Found", "status": "404"}))
                sys.stderr.write("gh: Not Found (HTTP 404)\n")
                sys.exit(1)
            files = [{"sha": None, "filename": entry["path"], "status": "modified",
                      "additions": entry.get("additions", 0),
                      "deletions": entry.get("deletions", 0), "changes": 0}
                     for entry in found[0].get("files") or []]
        return pages(files, params)
    refuse("fake gh: api " + endpoint + " is not recorded")


def pages(items, params):
    # GitHub's pagination (per_page, default 30, at most 100; page from 1)
    # and gh's printing of it: the one page asked, as one JSON array.
    per_page = min(int(params.get("per_page", 30)), 100)
    chunks = [items[at:at + per_page] for at in range(0, len(items), per_page)] or [[]]
    page = int(params.get("page", 1))
    fail_at = state["api_fail_at_page"]
    if fail_at is not None and page >= fail_at:
        sys.stderr.write("gh: Server Error (HTTP 502)\n")
        sys.exit(1)
    sys.stdout.write(json.dumps(chunks[page - 1] if page - 1 < len(chunks) else []))


def api_create(host, owner, name, opts):
    if opts.get("--input") != "-":
        refuse("fake gh: api POST without --input - is not recorded")
    try:
        sent = json.loads(input_content)
    except ValueError:
        sys.stdout.write(json.dumps({"message": "Problems parsing JSON", "status": "400"}))
        sys.stderr.write("gh: Problems parsing JSON (HTTP 400)\n")
        sys.exit(1)
    for needed in ("title", "head", "base", "body"):
        if not isinstance(sent.get(needed), str):
            refuse("fake gh: create without " + needed + " is not recorded")
    if state["create_hang"] == "before":
        time.sleep(600)
    hidden = 1 if state["create_hang"] == "late" else state["create_hidden_from_lists"]
    create(host, owner, name, sent["head"], sent["base"], sent["body"], hidden)
    made = rest_pull(state["prs"][-1], owner, name)
    made["title"] = sent["title"]
    if state["create_hang"] in ("after", "late"):
        time.sleep(600)
    if state["create_fails_with"] is not None:
        sys.stderr.write(state["create_fails_with"] + "\n")
        sys.exit(1)
    sys.stdout.write(json.dumps(made) if state["create_stdout"] is None
                     else state["create_stdout"])
"""


class FakeGh:
    """The script at `path`, fed `labels` (gh label objects, with any of
    LABEL_FIELDS as keys) and `prs` (gh pr list items, with any of
    PR_FIELDS)."""

    def __init__(self, directory, labels=(), prs=()):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "gh"
        self.path.write_text(_SCRIPT.format(python=sys.executable, label_fields=LABEL_FIELDS,
                                            pr_fields=PR_FIELDS,
                                            repo_fields=REPO_FIELDS,
                                            default_limit=GH_DEFAULT_LIMIT,
                                            logged=LOGGED_VARIABLES,
                                            api_handler=_API_HANDLER,
                                            proxy_refusal=PROXY_GRAPHQL_REFUSAL),
                             encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self._initial = (list(labels), list(prs))
        self.reset()

    def reset(self):
        """Back to the payloads it was made with, no failure, and an empty
        call log: for a fake shared between tests (tests/conftest.py)."""
        labels, prs = self._initial
        self._state = {"labels": labels, "prs": prs, "fail": None, "hang": False,
                       "raw_stdout": None, "fail_on": {}, "create_hang": None,
                       "create_stdout": None, "create_fails_with": None, "create_hidden_from_lists": 0,
                       "next_number": 1,
                       "default_branch": "main", "proxy": False, "api_fail_on": {},
                       "api_fail_at_page": None, "rest_pulls": None, "rest_files": {},
                       "rest_repo": REPO, "placeholder_repo": "placeholder-owner/placeholder-repo"}
        self._save()
        (self.directory / "calls.jsonl").unlink(missing_ok=True)

    def feed(self, **changes):
        """Change what later calls see. Built on the state as the script
        last left it, so PRs it created are kept. The keys (the one list
        of them), each with its default:

        - `labels` (as made): the label objects `label list` answers from.
        - `prs` (as made): the PR items `pr list` answers from and
          `pr create` appends to.
        - `hang` (False): every call sleeps far past any test's timeout.
        - `fail` (None): a stderr message every call prints, exiting 1.
        - `fail_on` ({}): the same, only for the commands it names by their
          first two arguments: {"pr create": message, "pr list": ...}.
        - `raw_stdout` (None): hex of the exact bytes every call prints,
          exit 0, whatever was asked (output gh's payloads cannot express,
          such as bytes that are not UTF-8).
        - `create_hang` (None): "before" makes `pr create` sleep without
          creating; "after" creates (and records) the PR, then sleeps (a
          timeout whose outcome is a PR); "late" is "after" with the PR
          kept out of the next `pr list` (GitHub still finishing it).
        - `create_stdout` (None): printed instead of the URL; the PR is
          still created (gh exiting 0 with output that is not the URL).
        - `create_fails_with` (None): printed on stderr after the PR is
          created, exiting 1 (GitHub made the PR, then answered a 5xx).
        - `create_hidden_from_lists` (0): how many `pr list` calls the
          created PR is kept out of, without any sleep (GitHub still
          finishing it); `create_hang="late"` sets 1 for its PR.
        - `next_number` (1): the next created PR's number.
        - `default_branch` ("main"): what `repo view --json
          defaultBranchRef` names, and `gh api repos/O/R`'s
          `default_branch`.
        - `proxy` (False): every call but `gh api` gets the cloud proxy's
          GraphQL refusal (module docstring).
        - `api_fail_on` ({}): {substring of "<METHOD> <endpoint>" of a `gh
          api` call (`GET repos/o/r/pulls?head`, `POST repos/o/r/pulls`):
          stderr message}: such a call exits 1 with it, before it does
          anything (a create that fails so opens no PR).
        - `api_fail_at_page` (None): a listing exits 1 (HTTP 502) when
          that page or a later one is asked, as gh does when a page fails.
        - `rest_pulls` (None): REST pull objects `gh api .../pulls`
          answers from verbatim, in place of the fed `prs`.
        - `rest_files` ({}): {"<number>": REST file objects} for `gh api
          .../pulls/<number>/files`, in place of the PR's `files`.
        - `rest_repo` (the recorded repository): the object `gh api
          repos/O/R` prints, its names and `default_branch` replaced.
        - `placeholder_repo`: what `{owner}/{repo}` stands for.

        Precedence, in the order the script checks: `hang`, then `fail`,
        then `fail_on`, then `raw_stdout`; a call any of them answers never
        reaches its command. Within `pr create`: `create_hang` "before"
        stops before creating; otherwise the PR is created, then "after"
        or "late" sleeps; then `create_fails_with`, if set, exits 1;
        otherwise `create_stdout` or the URL is printed."""
        self._state = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))
        self._state.update(changes)
        self._save()

    def prs(self):
        """The PR items the fake holds now, those `pr create` added included,
        without the fake's own bookkeeping key (`hidden_from_lists`, how
        `create_hang="late"` keeps a PR out of the next listing)."""
        held = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))["prs"]
        return [{key: value for key, value in item.items() if key != "hidden_from_lists"}
                for item in held]

    def calls(self):
        """Each call's argv, in order."""
        return [entry["argv"] for entry in self.call_records()]

    def call_records(self):
        """Each call's log record, in order: a dict of its `argv`, its `cwd`,
        its `body_file_content` (the text of its `--body-file`, else None),
        its `input_content` (what `gh api --input` read, else None) and
        each of LOGGED_VARIABLES (None when unset)."""
        log = self.directory / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def _save(self):
        (self.directory / "state.json").write_text(json.dumps(self._state), encoding="utf-8")
