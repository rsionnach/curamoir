# Running `curamoir-lint`

This page is for whoever runs the rules rather than writes them: a CI or
pre-commit integrator, and the author of a tool that reads the JSON report —
the sweep (E3-R1) and the promotion ladder (E4-R4) both do. It covers the
command line, the exit codes, both output formats, the JSON contract and its
versioning, wiring the runner into CI and pre-commit, and the limits that
matter there.

Writing a rule — its fields, matchers, scoping, messages, what is refused at
load time, and the limits that matter when writing one, such as which file
suffixes an `ast-grep` rule can search — is in
[Writing a rule](rules-authoring.md).

## Usage

```
scripts/curamoir-lint [--repo PATH] [--format human|json] [--fail-on info|warning|error]
                      [--timeout SECONDS]
scripts/curamoir-lint --version
```

`--timeout` bounds the whole run (default 600 seconds; `0` for no bound; at
most 1000000). Ten
minutes is far above a healthy run and inside CI job limits, so reaching it
means something is stuck, and the run exits 2 saying so rather than being
killed with no reason or hanging a pre-commit. It is the only bound there is
on a rule's regex that backtracks catastrophically on some line. Options must
be spelled in full: `--re` is not `--repo`. `--version` takes precedence over
every other argument, so `curamoir-lint --version --format json` prints the
version and exits 0 having checked nothing.

It makes no model calls and no network calls, and needs no cúramóir install
(E2-R5): only `scripts/curamoir-lint`, the `src/` directory beside it, a Python 3
that can import PyYAML (rule files are YAML), and `ast-grep` for ast-grep rules.
The wrapper finds `src/` from its own location — never from
`CLAUDE_PLUGIN_ROOT`, which a lint run outside Claude Code may have inherited
stale — and of the interpreters it tries (`python3.14` … `python`, the same list
as every wrapper) it takes the first that can import PyYAML.

## Exit codes

| code | meaning |
|---|---|
| `0` | every rule ran, and no violation is at or above the threshold |
| `1` | every rule ran, and at least one violation is at or above the threshold |
| `2` | the result cannot be trusted; the reasons are listed below |

`2` means one of these happened:

- a rule, `principles.md` or `curamoir.yaml` did not load (including a key
  given twice, or a `curamoir.yaml` that is a directory);
- `ast-grep` is needed and missing, failed, did not finish, or printed
  something other than a list of matches;
- a rule could not search every file in its scope (an `ast-grep` rule over a
  file it has no grammar for, or a file ast-grep skipped; any rule over a file
  that cannot be read);
- a directory in the repository could not be listed;
- the run exceeded `--timeout`;
- an option was invalid or empty (`--repo ""`);
- no Python was found;
- the report could not be written (stdout closed, or an encoding that cannot
  represent it — set `PYTHONIOENCODING=utf-8`);
- an internal error.

`2` outranks `1`. A run that skipped rules has not shown them clean, so it is
reported as incomplete even if what did run found something. When rules were
skipped — ast-grep missing, or a rule that could not search every file in
its scope — the partial report is still printed, each skipped rule is in
`rules_skipped` with the files named where they are known, and the other
rules' findings are kept. So are a skipped rule's own findings in the files
it did search: they are real violations, and are listed in `violations` like
any other, while the rule stays in `rules_skipped` (not `rules_checked`) and
the run exits 2, because what it could not search has not been shown clean. For every other `2`, stdout is empty and the reason
is on stderr.

The threshold is `--fail-on`, else `rules.fail_on` in `.curamoir/curamoir.yaml`,
else `error`. Every violation is reported whatever its severity; the threshold
decides only the exit code. A `curamoir.yaml` that does not validate is a `2`,
not a fall-back to the default — a gate loosened without anyone being told is
the failure this tool exists to prevent.

No rules at all is a `0`, with a note on stderr that nothing was checked: a
fresh repo is not a failing one.

## JSON (`--format json`)

The sweep's work list (E3-R1), and what the promotion ladder (E4-R4) reads to
tell a violation introduced from one merely found, so the shape is a contract.
`--version` prints `curamoir-lint <version> (JSON schema <n>)`.

```json
{
  "schema_version": 1,
  "tools": {"ast-grep": null},
  "fail_on": "error",
  "complete": true,
  "rules_checked": ["no-bare-print"],
  "rules_skipped": [],
  "notes": [],
  "violations": [
    {
      "location": "src/sample/core/billing.py:15:5",
      "path": "src/sample/core/billing.py",
      "line": 15,
      "col": 5,
      "end_line": 15,
      "end_col": 11,
      "severity": "error",
      "rule_id": "no-bare-print",
      "principle_id": "structured-logging",
      "principle_rule": "Emit diagnostics through the module logger, never through `print`.",
      "message": "print( writes to stdout with no level or logger name",
      "fix": "logger.info(...), or logger.debug(...) while debugging",
      "fix_source": "rule",
      "why_link": ".curamoir/principles.md#structured-logging",
      "match": "print(",
      "fingerprint": "66dc3a54eb3d313c"
    }
  ]
}
```

The top-level fields are always these, in this order:

- `schema_version`: which contract this is (see *Versioning* below).
- `tools`: the version of each external tool the results depend on, as that
  tool prints it. Today only `"ast-grep"`: the version of the binary that ran
  this run's `ast-grep` rules (`"0.50.0"`), or `null` when none ran (no
  `ast-grep` rules, ast-grep not installed, or every one skipped before it
  ran) or when its `--version` could not be read — the `notes` then say so.
  An unreadable version does not change the exit code: each ast-grep call's
  own output is checked whatever version it claims, so the version is
  provenance, not evidence.
- `fail_on`, `complete`, `rules_checked`, `rules_skipped`: the threshold and
  the verdict. `rules_skipped` entries are `{"rule_id": ..., "reason": ...}`,
  and `complete` is false exactly when there are any.
- `notes`: facts about the run that are not a rule's result (see below).
- `violations`: one record per violation.

Each violation record has exactly these fields, in this order:

| field | what it is |
|---|---|
| `location` | `path:line:col`, for anything that opens a location |
| `path` | repo-relative, `/`-separated |
| `line`, `col` | where the match starts, 1-based. Columns count characters (code points), not bytes; a UTF-8 BOM is not one. A `target: path` rule reports `1:1`, the file itself |
| `end_line`, `end_col` | where the match ends, 1-based and **exclusive**: the position just after its last character, as SARIF counts it, so a one-line match is `end_col - col` characters long. A `regex` match never spans lines (`end_line` is `line`); an `ast-grep` match can. A `target: path` rule reports `1:1`, the empty region: the match is in the name, not the text |
| `severity` | the rule's |
| `rule_id`, `principle_id` | the rule and the one principle it cites |
| `principle_rule` | the principle's one-sentence `rule`, so the agent need not open `principles.md` |
| `message` | the rule's `message`, rendered |
| `fix` | the rule's `fix`; if it has none, the principle's `good` example, which is a corrected form by definition |
| `fix_source` | `"rule"` when `fix` is the rule's own, `"principle"` when it is the principle's `good` example (a whole corrected example, not an edit to paste) |
| `why_link` | `.curamoir/principles.md#<anchor>`: the principle's heading, where its `why` is |
| `match` | the text that matched (for a path rule, the part of the path) |
| `fingerprint` | the violation's identity across runs; see below |

`violations` is sorted by path, line, column and rule id (then end and match,
which only one rule's overlapping `ast-grep` matches can need), so two runs
over the same tree are byte-identical. For the same reason the report carries
no timestamp and no run id, by design: the caller that needs them (the sweep's
ledger record) supplies its own. Adding a `generated_at` would be additive
under the versioning policy below and would still break that guarantee, so
don't.

Strings are exact, with control characters carried as JSON escapes, with one
exception: a filename that is not valid UTF-8 has its bad bytes shown escaped
(`bad\xff.py`), in `path`, `location` and a note's `path` alike.

### Fingerprint

A violation found again in a later run has the same `fingerprint`, so a
consumer can tell a violation it has seen (E3-R7: a second sweep with nothing
changed opens no PR) from one introduced since (E4-R4). The recipe, which a
test pins to one exact value so it cannot drift:

1. Take the record's `rule_id`, `path` and `match`, exactly as the JSON shows
   them — the decoded string values after parsing, not the escaped text in
   the file (the report writes control characters as JSON escapes, `\n` or
   `\u001b`, and other non-ASCII characters as raw UTF-8) — and remove every
   ASCII whitespace character (space, tab, `\n`, `\r`, form feed, vertical
   tab) from the match.
2. Take the occurrence index, in decimal: `0` for the first violation in the
   file with that rule, path and whitespace-free match, in report order; `1`
   for the next identical one; and so on.
3. Write each of the four as `<length of its UTF-8 bytes>:<those bytes>,` and
   concatenate them — e.g. `13:no-bare-print,8:src/a.py,10:print("x"),1:0,`.
4. Hash with SHA-256 and keep the first 16 hex digits (64 bits).

It survives code inserted above the violation (line and column are not in it)
and whitespace-only reformatting of the matched text (re-indenting,
re-wrapping, `f(a,b)` to `f(a, b)`). It changes when the rule, the file's path
(a rename or move is a new fingerprint) or any non-whitespace character of the
match changes. Its limit is the occurrence index: two identical matches in one
file are told apart only by their order, so deleting the first shifts the
second onto the first's fingerprint, and the change reads as the second one
going. Within one report every fingerprint is unique.

A match that is empty or only whitespace normalises to the empty string, so
its identity is just its occurrence index in that file: the count of such
violations stays right across runs, which one is which does not. A wide
pattern (a whole function body, say) puts the whole match into `match` and the
fingerprint, so any edit inside it is a new fingerprint, and the JSON grows
with it. `path` is the printable form of the raw path, which is one-way: two
distinct raw paths can print the same (a file literally named `bad\xff.py`
and one whose name holds byte 0xFF), so they stay unique within a report by
the occurrence index, but their identities can swap between runs.

### Notes

Each entry is an object with a `kind`, listed in this order and then by path:

- `no_rules` (keys: `kind`): there are no rules, so nothing was checked
  (exit 0).
- `ast_grep_version_unknown` (keys: `kind`, `reason`): an `ast-grep` rule ran
  but `ast-grep --version` could not be read — it failed, printed something
  else, printed output that is not UTF-8, or did not answer within 10 seconds
  (or `--timeout`, if shorter); `reason` says which, naming the stream it
  quotes. `tools["ast-grep"]` is `null`. The probe always spends up to the
  shorter of those two bounds of the run's `--timeout` budget; when
  `--timeout` is 10 seconds or less, a hanging probe can spend all of it and
  the run exits 2 as for any other hang.
- `symlink_not_followed` (keys: `kind`, `path`): a symlink at `path` was not
  linted (see *Limits* below).
- `not_regular_file` (keys: `kind`, `path`): a FIFO, socket or device at
  `path` was not read.

These are the facts the stderr notes give; stderr is unchanged by them. None
changes the exit code.

### Versioning

`schema_version` is bumped only for a change a parser could misread:

- Adding a top-level field, a record field, a `tools` entry or a note `kind`
  does **not** bump it. Consumers **must** ignore fields and note kinds they
  do not know.
- Removing, renaming or retyping a field bumps it.
- Changing what a field means while its type stays — how `col` is counted,
  whether `end_col` is exclusive, the fingerprint recipe — bumps it.
- A new value in a closed vocabulary — `severity`, `fix_source` — bumps it,
  because a consumer that ranks severities or branches on a fix's origin
  cannot place a value it has never seen, and guessing would loosen a gate.

`schema_version` is 1. (Before any release the field was named `version`; no
consumer read that shape, and it has no `schema_version` key to be mistaken
for this one.)

## Human (the default)

```
src/sample/core/billing.py:15:5: error [structured-logging/no-bare-print] print( writes to stdout with no level or logger name
    rule: Emit diagnostics through the module logger, never through `print`.
    why:  .curamoir/principles.md#structured-logging
    fix:  logger.info(...), or logger.debug(...) while debugging
...
5 violations from 3 rule(s); 3 at or above 'error'
```

One block per violation, in location order, then a summary line: how many
violations, from how many rules that ran (and, if any were skipped, how
many), and how many meet the threshold.
Each field is kept to its own line: newlines and other control characters in
a path, message, rule or fix are shown escaped (`\n`, `\x1b`), so a match
spanning lines cannot forge a `fix:` line. Every note on stderr is escaped
the same way, since most quote the repository: a filename, a config key,
ast-grep's own message. The escaping is one-way — a file named with a
backslash and an `n` prints exactly like one named with a newline — so where
the exact name matters, read the JSON: its `path`, `line` and `col` are
exact, with control characters carried as JSON escapes. In both formats a
filename that is not valid UTF-8 is shown with its bad bytes escaped
(`bad\xff.py`), the one case where the JSON is not the name byte for byte.

## In CI and pre-commit

CI needs a checkout of cúramóir, not an install:

```yaml
- run: pip install pyyaml 'ast-grep-cli==0.50.*'
- run: git clone --depth 1 --branch v0.1.0 https://github.com/rsionnach/curamoir /tmp/curamoir
- run: /tmp/curamoir/scripts/curamoir-lint --format json > lint.json
```

Pin both. An unpinned clone or ast-grep can change the gate's verdict between
two runs of the same commit, and the runner's handling of ast-grep (its JSON,
its skip counts, the BOM column) is verified against 0.50.0. `v0.1.0` stands
for a release tag; to pin a commit instead, clone without `--branch` and
`git -C /tmp/curamoir checkout <sha>` (a SHA cannot be shallow-cloned by
name). The report's `tools` says which ast-grep actually ran.

Pre-commit, as a `local` hook pointing at that checkout (`pass_filenames: false`:
the runner lints the repository, not a list of files):

```yaml
- repo: local
  hooks:
    - id: curamoir-lint
      name: curamoir-lint
      entry: /path/to/curamoir/scripts/curamoir-lint
      language: system
      pass_filenames: false
```

## Limits, for CI and pre-commit

What the runner does not do, or does differently from what you might assume,
that matters when wiring it into CI or pre-commit. The limits that matter when
writing a rule are under
[Limits, for rule authors](rules-authoring.md#limits-for-rule-authors).

- The wrapper never starts Python inside the repository. `python -m` puts its
  working directory ahead of everything else on `sys.path`, so a `yaml.py` or
  `curamoir/` committed at the root of a PR would otherwise run as the linter.
  Python starts in cúramóir's own `src/` and moves back to the invoking
  directory only after pinning its import path, so `--repo` still means what
  it meant where you typed it. An inherited `PYTHONPATH` is kept (after
  `src/`), as in every cúramóir wrapper: it is set by whoever runs the lint,
  not by the repository. Its relative entries are resolved against the
  directory you invoked the wrapper from, as you would expect; empty entries
  (which Python reads as "the working directory", i.e. the repository) are
  dropped.
- If the wrapper is reached through a symlink (a link in `~/bin`, say), it
  follows the link to find `src/`; that needs `readlink` on PATH, the one
  external command it uses. It is a bash script: run it as itself or with
  `bash`, not with `sh` or `dash`.
- `ast-grep` is looked up only in absolute PATH entries. A relative entry
  (`.`, `bin`, or an empty one) would resolve inside the repository, letting
  a PR choose the binary its own rules are checked with.
- `--timeout` uses `SIGALRM`, so it bounds the run on Linux and macOS when
  `curamoir-lint` runs as its own process (as the wrapper does). Where that
  is unavailable — Windows, or `curamoir.lint.main` called from a non-main
  thread — the run has no overall bound, and only each `ast-grep` call is
  limited, by the same number of seconds. When a call is cut off, ast-grep's
  whole process group is killed, so an `ast-grep` that is a wrapper script
  around the real binary leaves nothing running (Windows: only the child).
- `python -m curamoir.lint` directly (without the wrapper) lints relative to
  its own working directory, and reads `CURAMOIR_LINT_INVOKED_FROM` if set: a
  directory to change to first, which is how the wrapper passes on where it
  was invoked. Leave it unset when calling the module yourself.
- `--repo` should be the repository root: rules, principles and config are
  read from its `.curamoir/`. Pointed at a subdirectory with no `.curamoir/`,
  the run finds no rules and exits 0 with its note on stderr.
- Symlinks are not followed, file or directory. In CI the tree is often an
  unreviewed PR, and `match` quotes matched text, so a committed
  `leak.py -> ~/.ssh/id_rsa` would otherwise have the lint print part of the
  target. A symlink's name (never its target or contents) is listed on stderr
  and as a `symlink_not_followed` note in the JSON; it does not change the
  exit code.
- The repository's own `sgconfig.yml` is never read. ast-grep looks for one in
  its working directory and every ancestor, and one in a PR could re-map file
  types so an `ast-grep` rule silently matches nothing, break the run, or (on
  ast-grep versions that load `customLanguages` native libraries without
  `--custom-languages allow`) run native code. So ast-grep runs from a scratch
  directory outside the repo with `--config` pointing at an empty config of
  cúramóir's own.
