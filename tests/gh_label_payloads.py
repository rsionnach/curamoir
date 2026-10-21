"""What `gh` really prints for a repository's labels, recorded, so the kill
switch's tests are shaped by gh's output and not by what the guard happens to
expect.

**Provenance.** Both shapes were captured on 2026-10-21 with
`gh version 2.86.0 (2026-01-21)`, read-only, against this repository
(public), each command wrapped in `perl -e 'alarm 60; exec @ARGV' --`:

- `gh label list --json name --limit 5` gave LABEL_LIST below.
- `gh repo view --json labels --jq '{labels: .labels[:3]}'` gave REPO_VIEW
  below. The `--jq` only trims the list to three labels; each label object is
  as gh printed it, keys and all.

**One edit.** In each capture the label "bug" has been replaced by
"curamoir:paused", and nothing else changed. The repository does not carry
the pause label (if it did, every scheduled job here would refuse), so the
recorded output cannot contain it; the tests need it present to show that a
paused repository whose labels arrive in gh's shape is still not read as
unpaused. The description, colour and id beside it are still bug's.
"""

#: `gh label list --json name --limit 5`: a list of one-key objects.
LABEL_LIST = [
    {"name": "accessibility"},
    {"name": "curamoir:paused"},  # was "bug"; see the module docstring
    {"name": "documentation"},
    {"name": "duplicate"},
    {"name": "enhancement"},
]

#: `gh repo view --json labels`, trimmed to three labels: one mapping whose
#: "labels" holds full label objects.
REPO_VIEW = {
    "labels": [
        {"color": "f143ab", "description": "Barrier affecting people with disabilities",
         "id": "LA_kwDOU5ZXXc8AAAAC6hRXvw", "name": "accessibility"},
        {"color": "d73a4a", "description": "Something isn't working",
         "id": "LA_kwDOU5ZXXc8AAAAC6hRXug",
         "name": "curamoir:paused"},  # was "bug"; see the module docstring
        {"color": "0075ca", "description": "Improvements or additions to documentation",
         "id": "LA_kwDOU5ZXXc8AAAAC6hRXxw", "name": "documentation"},
    ]
}
