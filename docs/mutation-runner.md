# The mutation runner

`curamoir run mutations` runs a brief's mutation list: for each entry it
makes a mutation in a throwaway worktree and checks that the tests named for
it fail. No model is involved. It is the mechanical half of the R5 gate's
provenance lens, callable on its own:

```
curamoir run mutations --repo <root> --base <sha> --head <sha> --brief <path>
```

## The mutation list

The brief is a Markdown file holding exactly one fenced code block whose info
string is `r5-mutations`. The block's content (as CommonMark defines a fenced
block's content) is a YAML list:

```yaml
- path: src/opensrm/parser/v2.py
  locate: {lines: [118, 131]}          # or {anchor: "def classify("}
  mutation: invert_condition
  expect_red: "python -m pytest tests/test_parser_v2.py::test_judgment_slo_mapping -q"
  behaviour: "a judgment SLO maps to its own type"   # optional
```

| field | what it is |
|---|---|
| `path` | the file to mutate, repo-relative. |
| `locate` | `{lines: [first, last]}`, an inclusive 1-based range, or `{anchor: "text"}`, the one line containing the text, extended to the end of the statement that starts on it (an anchor on a `def` takes the whole function). |
| `mutation` | `invert_condition`, `drop_argument`, `return_constant`, `delete_statement`, `swap_operator` (Python only: each acts on the first matching node of the syntax tree that lies wholly inside the region, so a `lines` range must cover a whole statement, or an `anchor` on its first line be used), or `{replace: {from: "...", to: "..."}}` (any UTF-8 file: the text must occur exactly once in the region). |
| `expect_red` | the test command that must fail once the mutation is made. |
| `behaviour` | optional: what the entry protects. A surviving mutation's report names it. |

The block is refused, and nothing runs (exit 3, reason `mutation_list`),
when the brief has none, has more than one, or the list is empty; when an
entry has a key that is not a field (`behavior` is refused naming
`behaviour`), lacks one, has a path that is not plainly repo-relative, a
`locate` of neither form, an unknown mutation, or a Python operator on a file
that is not Python.

## What each entry reports

The runner makes a worktree at `--head`, runs `expect_red` there unmutated,
makes the mutation, and runs it again:

| result | means | blocks |
|---|---|---|
| `red` | the selection passed unmutated and fails with the mutation: the tests guard it | no |
| `green` | the selection still passes with the mutation made: nothing guards that behaviour | yes |
| `stale` | the entry no longer fits the code: the path is gone or goes through a symlink, the locate matches nothing (or an anchor matches twice), or the mutation finds nothing to act on | yes |
| `inconclusive` | the kill check did not run: the selection failed or errored unmutated, errored rather than failed with the mutation, timed out, or ran no test at all (none collected, or every one skipped or deselected) | yes |

Every selection runs with `PYTEST_ADDOPTS=--junitxml=<file>` in its
environment and its command exactly as written, so a pytest it starts,
however it is launched (`uv run`, `poetry run`, `coverage run -m`, a `make`
target), reports each test: a run whose every test was skipped is
`inconclusive`, not `green`. A selection that writes no such file (pytest
never ran) is read by its exit code: 0 passes, 1 fails, anything else is
`inconclusive`. Each run is bounded by
`sweep.test_timeout_seconds`.

## Output and exit codes

stdout carries one JSON object per entry, as each finishes, and nothing else:

```json
{"index": 2, "total": 3, "path": "src/x.py", "locate": "lines 4-4", "mutation": "return_constant", "behaviour": null, "result": "green", "reason": "the mutant survived: ...", "diff": "--- a/src/x.py\n+++ b/src/x.py\n...", "worktree": "...", "output_tail": "...", "duration_s": 1.42}
```

The run's sentence, and any notice (an orphan worktree from a dead run, below),
go to stderr. A run that executes no entry prints nothing on stdout.

| exit | when |
|---|---|
| 0 | every entry is `red` |
| 1 | any entry is `green`, `stale` or `inconclusive` |
| 2 | a usage error: an option missing, or given to a job that takes none |
| 3 | refused, no entry ran: `mutation_list` (above), `sandbox` (no OS sandbox here, a `--base`/`--head` that is not a commit, or the run's worktree directory cannot be made), or `runner` (`CURAMOIR_RUNNER` cannot be honoured) |
| 4 | an unusable root (below) |

## Where it runs, and what it writes

Every selection runs in a worktree under the repository's git common
directory, never in the checkout:

```
<git-common-dir>/r5-worktrees/standalone/<run id>/mutation-<n>/
```

On the local runner each run is confined by the OS sandbox, as the sweep's
test runs are: the worktree and the run's scratch directory are the only
places it may write, and it has no network. Without an OS sandbox the run is
refused (exit 3, `sandbox`). On a hosted runner (`CURAMOIR_RUNNER=hosted`)
the VM is the boundary and no OS sandbox is needed. At most
`gate.worktrees.max_parallel` worktrees run at once (default 2); a hosted
runner uses its core count, capped at 8, or that value if `curamoir.yaml`
sets a lower one.

The worktrees are removed when the run ends, also when it is terminated: a
stopped run kills its test processes first. A run that is killed outright
cannot clean up. Each run holds an exclusive lock on `owner.lock` in its run
directory while it lives, and the kernel releases it when the process dies,
so a run directory whose lock is free belongs to a dead run. The next run
names such an orphan on stderr, and `/curamoir:doctor` reports it as a
warning. Remove its worktrees with `git worktree remove --force <path>`, then
the directory, then `git worktree prune`.

**With `.curamoir/`** the run is a job like any other: behind the guards,
with one ledger line, and it keeps a record in
`.curamoir/state/r5/runs/<run id>/`: `mutations.yaml`, the block's content
byte for byte, and `results.jsonl`, the lines it printed.

**Without `.curamoir/`** (a checkout cúramóir was never set up in) it still
runs, but only at the top of the git work tree: no ledger line, no lock slot,
and nothing written under the root. A subdirectory, or a `.curamoir` that is
a file, is exit 4.

## Using it from another repository

cúramóir must be installed where the command runs, for example
`uv tool install git+https://github.com/rsionnach/curamoir` (or
`pip install` of a checkout), so that `curamoir` is on `PATH`. Then, from the
other repository's checkout, run the command above with that checkout's
`--repo`. Its own `.venv`, if it has one, is what the selections run with.
