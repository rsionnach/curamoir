"""What `gh api` (GitHub's REST API) prints for the calls curamoir.github
and curamoir.publish make in a Claude Code cloud session (cm-2qp.9), so
the REST path's tests are shaped by GitHub's responses and not by what
the adapter happens to expect. The files are in tests/gh_api_recordings/.

**Captured** on 2026-11-01 with `gh version 2.86.0 (2026-01-21)`,
read-only, each command wrapped in `perl -e 'alarm 60; exec @ARGV' --`,
authenticated as this repository's owner:

- `curamoir_repo.json`: `gh api repos/rsionnach/curamoir`, verbatim
  (REPO). Checked for secrets before it was committed: the repository is
  public, the one field named like a credential is `temp_clone_token`,
  which is `""`, and `permissions` is the capturing account's own access
  (admin), not a credential. Its `default_branch` is
  `feat/e0-r0-guard-layer`, as the repository's default was that day.
- `curamoir_labels.json`: `gh api 'repos/rsionnach/curamoir/labels?
  per_page=100'`, verbatim (LABELS): the repository's ten labels.
- `curamoir_pulls_state_all.json`: `gh api 'repos/rsionnach/curamoir/
  pulls?state=all&per_page=5'`, verbatim (NO_PULLS): `[]`, since no PR
  has ever been opened on this repository.
- `notfound_stdout.json` and `notfound_stderr.txt`: `gh api
  repos/rsionnach/nosuchrepo-xyz`, exit 1: GitHub's error body on stdout
  and `gh: Not Found (HTTP 404)` on stderr (NOT_FOUND_STDOUT,
  NOT_FOUND_STDERR). So a REST error exits non-zero, as every gh failure
  the adapter reads as GhError.
- Pagination, observed and not stored: `gh api --paginate
  'repos/rsionnach/curamoir/labels?per_page=3' --jq '.[].name'` printed
  the ten names one per line across the four pages, byte for byte the
  `gh label list --jq '.[].name'` output tests/gh_label_payloads.py
  records (LABEL_NAMES_JQ); and `--paginate --slurp` printed `[[...3
  labels...],[...],[...],[...1...]]`, one array per page. The adapter
  uses neither (it pages by hand, `page=<n>`: curamoir.github, "Each
  query"), so tests/fake_gh.py models neither.

**Captured, shape only** (the same day, same gh), because this repository
has no PRs: `gh api 'repos/cli/cli/pulls?state=all&per_page=30'` and
`gh api 'repos/cli/cli/pulls/14846/files?per_page=100'`, gh's own public
repository. `cli_pulls_shape.json` (PULLS_SHAPE) keeps four of the thirty
items, each picked for a case the mapping must get right: #14846 open
from a same-repository branch, #14879 merged from a same-repository
branch, #14885 closed from a fork, #14823 closed from a fork since
deleted (`head.repo` is `null`; `gh pr view 14823 -R cli/cli --json
isCrossRepository` said `true` the same day). Of each item it keeps only
the keys the adapter reads, at their captured nesting: `number`, `state`,
`html_url`, `merged_at`, `body`, `title`, and `head`/`base` as `label`,
`ref`, `sha`, `repo` (`id`, `full_name`), every value's type as
captured. Kept as captured: `number`, `state`, `html_url`, `merged_at`,
each `sha`, each `repo.id` and cli/cli's `full_name`, `base.ref` and
`base.label`. Replaced with neutral synthetic values, as
tests/gh_pr_payloads.py does, because they named or described someone
else's work: every `head.ref` (and the `head.label` made from it), every
`body` and `title`, and a fork's `full_name` (a person's username, now
`example-fork-owner/cli`). `cli_pull_files_shape.json` (PULL_FILES_SHAPE)
is #14846's two file objects with `sha`, `filename`, `status`,
`additions`, `deletions`, `changes` kept (`patch`, `blob_url`,
`raw_url`, `contents_url` dropped) and the first `filename` replaced
(`docs/workflow.md`).

**Documented, not captured**: `documented_create_pull.json`
(CREATE_RESPONSE), the 201 response of `POST /repos/{owner}/{repo}/pulls`
as GitHub's REST reference shows it ("Create a pull request",
https://docs.github.com/en/rest/pulls/pulls?apiVersion=2022-11-28,
the `octocat/Hello-World` #1347 example), trimmed to the keys above. No
PR was opened to record a real one. Its `merged_at` is the example's own
value, which a real create would give as null; nothing here reads it.

**Documented, not captured**: the GitHub proxy's refusal of GraphQL in a
cloud session (PROXY_GRAPHQL_REFUSAL), whose message "starts with
`GitHub GraphQL is not available from Claude Code sessions` and names
the REST fallback, `gh api repos/{owner}/{repo}/...`"
(https://code.claude.com/docs/en/cloud-environments, "GitHub proxy",
read 2026-11-01). Only that prefix is documented; the rest of the
string here is a stand-in.
"""

import json
from pathlib import Path

_HERE = Path(__file__).parent / "gh_api_recordings"


def _load(name):
    return json.loads((_HERE / name).read_text(encoding="utf-8"))


#: `gh api repos/rsionnach/curamoir`, verbatim.
REPO = _load("curamoir_repo.json")

#: `gh api 'repos/rsionnach/curamoir/labels?per_page=100'`, verbatim.
LABELS = _load("curamoir_labels.json")

#: `gh api 'repos/rsionnach/curamoir/pulls?state=all&per_page=5'`: `[]`.
NO_PULLS = _load("curamoir_pulls_state_all.json")

#: Four cli/cli pulls, shape only (module docstring).
PULLS_SHAPE = _load("cli_pulls_shape.json")
SAME_REPOSITORY_OPEN, SAME_REPOSITORY_MERGED, FORK_CLOSED, DELETED_FORK_CLOSED = PULLS_SHAPE

#: cli/cli #14846's files, shape only.
PULL_FILES_SHAPE = _load("cli_pull_files_shape.json")

#: GitHub's documented create response (module docstring).
CREATE_RESPONSE = _load("documented_create_pull.json")

#: A REST error: stdout and stderr of `gh api repos/rsionnach/nosuchrepo-xyz`.
NOT_FOUND_STDOUT = (_HERE / "notfound_stdout.json").read_text(encoding="utf-8")
NOT_FOUND_STDERR = (_HERE / "notfound_stderr.txt").read_text(encoding="utf-8")

#: The proxy's documented refusal prefix, and a stand-in for the rest.
PROXY_GRAPHQL_PREFIX = "GitHub GraphQL is not available from Claude Code sessions"
PROXY_GRAPHQL_REFUSAL = (PROXY_GRAPHQL_PREFIX + "; use the REST API instead: "
                         "gh api repos/{owner}/{repo}/... (HTTP 403)")
