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

Every call appends its argv, the directory it ran in (`cwd`: real gh picks
its repository from it) and the LOGGED_VARIABLES it was given (None when
unset) to `calls.jsonl` beside the script. The payloads are re-read
on every call, so a test can change what gh "sees" between two calls.
`fail` makes every call exit non-zero with a message on stderr, and `hang`
makes it sleep far past any timeout a test sets.
"""

import json
import stat
import sys
from pathlib import Path

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

#: The environment variables each call's log record carries.
LOGGED_VARIABLES = ("GIT_DIR", "GH_REPO", "GH_PROMPT_DISABLED", "GH_FORCE_TTY",
                    "CLICOLOR_FORCE", "NO_COLOR")

#: gh's `--limit` when none is given.
GH_DEFAULT_LIMIT = 30

_SCRIPT = '''#!{python}
import json, os, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
args = sys.argv[1:]
with open(here / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": args, "cwd": os.getcwd(),
                          **{{name: os.environ.get(name) for name in {logged!r}}}}}) + "\\n")
state = json.loads((here / "state.json").read_text(encoding="utf-8"))
if state["hang"]:
    time.sleep(600)
if state["fail"] is not None:
    sys.stderr.write(state["fail"] + "\\n")
    sys.exit(1)
if state["raw_stdout"] is not None:
    sys.stdout.buffer.write(bytes.fromhex(state["raw_stdout"]))
    sys.exit(0)


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


if args[:2] == ["label", "list"]:
    labels = select(state["labels"], {label_fields!r})
    jq = option("--jq")
    if jq is None:
        sys.stdout.write(coloured(json.dumps(labels)) + "\\n")
    elif jq == ".[].name":
        sys.stdout.write("".join(label["name"] + "\\n" for label in labels))
    else:
        refuse("fake gh: unrecorded --jq " + jq)
elif args[:2] == ["pr", "list"]:
    if option("--state") != "open":
        refuse("fake gh: only --state open is recorded")
    sys.stdout.write(coloured(json.dumps(select(state["prs"], {pr_fields!r}))) + "\\n")
else:
    refuse("fake gh: no recording for " + " ".join(args))
'''


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
                                            default_limit=GH_DEFAULT_LIMIT,
                                            logged=LOGGED_VARIABLES),
                             encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self._initial = (list(labels), list(prs))
        self.reset()

    def reset(self):
        """Back to the payloads it was made with, no failure, and an empty
        call log: for a fake shared between tests (tests/conftest.py)."""
        labels, prs = self._initial
        self._state = {"labels": labels, "prs": prs, "fail": None, "hang": False,
                       "raw_stdout": None}
        self._save()
        (self.directory / "calls.jsonl").unlink(missing_ok=True)

    def feed(self, **changes):
        """Change what later calls see: `labels`, `prs`, `fail` (a stderr
        message, and a non-zero exit), `hang`, or `raw_stdout` (hex of the
        exact bytes every call prints, exit 0, whatever was asked: for
        output gh's payloads cannot express, such as bytes that are not
        UTF-8)."""
        self._state.update(changes)
        self._save()

    def calls(self):
        """Each call's argv, in order."""
        return [entry["argv"] for entry in self.call_records()]

    def call_records(self):
        """Each call's log record, in order: a dict of its `argv`, its `cwd`
        and each of LOGGED_VARIABLES (None when unset)."""
        log = self.directory / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def _save(self):
        (self.directory / "state.json").write_text(json.dumps(self._state), encoding="utf-8")
