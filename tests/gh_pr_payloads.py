"""The shape of what `gh` prints for a repository's open pull requests,
recorded, so the open-PR listing's tests are shaped by gh's output and not by
what curamoir.github happens to expect.

**Provenance.** Captured on 2026-10-23 with `gh version 2.86.0 (2026-01-21)`,
read-only, each command wrapped in `perl -e 'alarm 60; exec @ARGV' --`:

- Against this repository (public, no pull request ever opened):
  `gh pr list --state open --json headRefName,number,url,body,files,
  isCrossRepository --limit 5` printed `[]` and a newline, as did the same
  query with `--state all --limit 2`. NO_OPEN_PRS is that output.
- This repository having none, the shape of a non-empty listing was taken
  from gh's own public repository: `gh pr list -R cli/cli --state open
  --json headRefName,number,url,body,files,isCrossRepository --limit 3
  --jq '[.[] | {headRefName, number, url, body: (.body[:80]), files:
  (.files[:2]), isCrossRepository}]'`. The `--jq` only trims: each body to
  80 characters and each PR's files to the first two.

**Shape only: the edit.** OPEN_PRS keeps, from that capture, exactly: the
keys of each item and of each file object, every value's type, the file
objects as `{"additions": int, "deletions": int, "path": str}`, two files
per PR, and each PR's `isCrossRepository` (true, true, false: two forks,
then a same-repository branch). Everything that named or described
someone else's work was replaced with neutral synthetic values: every
`headRefName` (one was a person's username), `number`, `url`, `body` and
file `path`. The URLs follow gh's
`https://github.com/<owner>/<repo>/pull/<number>` form on the reserved
`example` owner. The synthetic paths within each PR are in path order,
as gh lists a PR's files (every captured PR's were). Nothing else
changed: the `additions`/`deletions` counts are as captured, in their
captured positions.

**Further edits, made by `as_sweep_pr` and never in OPEN_PRS.** No PR
there is a sweep PR, so a test that needs one takes an item and replaces
its `headRefName` and `body` (a sweep branch and a body carrying the
sweep marker), and, when it needs several PRs from one item, its `number`
(and the `url` that ends in it). `files` and `isCrossRepository` stay as
they are, which is the point: a fork's PR and a same-repository PR each
keep gh's own value.
"""

from curamoir.idempotence import sweep_marker

#: This repository's `gh pr list --state open --json ...`: no open PRs.
NO_OPEN_PRS = []

#: Open PRs in gh's shape (module docstring: the capture's shape, synthetic
#: values). The first two are from forks; the third is a same-repository
#: branch.
OPEN_PRS = [
    {"body": "Adds a retry to the upload client.",
     "files": [{"additions": 3, "deletions": 1, "path": "src/upload/client.py"},
               {"additions": 1, "deletions": 2, "path": "tests/test_client.py"}],
     "headRefName": "fork-feature-one", "isCrossRepository": True,
     "number": 101, "url": "https://github.com/example/repo/pull/101"},
    {"body": "Draft: verify downloads before writing them.",
     "files": [{"additions": 209, "deletions": 21, "path": "src/download/verify.py"},
               {"additions": 1, "deletions": 1, "path": "tests/test_verify.py"}],
     "headRefName": "fork-feature-two", "isCrossRepository": True,
     "number": 102, "url": "https://github.com/example/repo/pull/102"},
    {"body": "<!--\nA template comment.\n-->\n\nDocuments the new flags.",
     "files": [{"additions": 89, "deletions": 0, "path": "docs/development.md"},
               {"additions": 4, "deletions": 0, "path": "docs/flags.md"}],
     "headRefName": "same-repo-feature", "isCrossRepository": False,
     "number": 103, "url": "https://github.com/example/repo/pull/103"},
]

#: The fork PRs and the same-repository one.
FORK_PRS = OPEN_PRS[:2]
SAME_REPOSITORY_PR = OPEN_PRS[2]


def as_sweep_pr(recorded, branch, base, number=None):
    """`recorded` (one of the items above) as a sweep PR on `branch` made
    from `base`: `headRefName` and `body` replaced, and `number` and `url`
    when `number` is given; nothing else (module docstring)."""
    item = {**recorded, "headRefName": branch,
            "body": f"Sweep fixes.\n\n{sweep_marker(base)}\n"}
    if number is not None:
        item["number"] = number
        item["url"] = recorded["url"].rsplit("/", 1)[0] + f"/{number}"
    return item
