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

A third shape was captured on 2026-10-23 with the same gh version, the same
way, against this repository: `gh label list --limit 1000 --json name --jq
'.[].name'`, exactly the command curamoir.github.repo_labels runs, gave
LABEL_NAMES_JQ below, byte for byte (`od -c` of the output: each name
followed by a newline, spaces kept, nothing else). It is NOT edited: it is
what tests/fake_gh.py's `--jq '.[].name'` output is checked against, so it
must stay gh's own bytes, and the pause label is absent from it.

**One edit**, to the first two shapes only. In each of those captures the
label "bug" has been replaced by "curamoir:paused", and nothing else changed. The repository does not carry
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

#: `gh label list --limit 1000 --json name --jq '.[].name'`, verbatim: this
#: repository's ten labels, one per line. Not edited (module docstring).
LABEL_NAMES_JQ = ("accessibility\nbug\ndocumentation\nduplicate\nenhancement\n"
                  "good first issue\nhelp wanted\ninvalid\nquestion\nwontfix\n")
