# Writing a rule

A rule turns one principle into something a machine checks. It lives in
`.curamoir/rules/<rule-id>.yaml`, and `curamoir-lint` runs it.

Rules are loaded from that directory itself, from files ending in lower-case
`.yaml` or `.yml`. Notes (`*.md`) and dotfiles (`.gitkeep`) may sit beside
them; anything else — a subdirectory, `X.YAML`, `x.yaml.disabled` — is a load
error, because a rule filed where the loader does not look would never run
while you believe it does.

Rules are how a principle gets promoted from `principle` to `rule` on the
ladder — so you write one when you have corrected the same violation about
three times and are tired of saying it.

Working examples against the starter principles, in Python and TypeScript, are
in `examples/`; its README says how to install them and what each does not
catch.

## The shape

Required fields only:

```yaml
id: no-bare-print
principle: structured-logging
severity: error
matcher: ast-grep
pattern: 'print($$$ARGS)'
message: "{match} writes to stdout with no level or logger name"
```

Everything else is optional:

```yaml
fix: "logger.info(...), or logger.debug(...) while debugging"
languages: [python]              # scope by language
files: ["**/*.py"]               # or by glob, which wins over `languages`
exclude: ["**/cli.py"]           # carve out what the principle itself exempts
target: line                     # or `path`, to match a filename
```

| field | what it is |
|---|---|
| `id` | this rule's name. Findings carry it and fixes are counted against it, so it must be unique across the directory. |
| `principle` | **exactly one** principle id from `principles.md`. See below. |
| `severity` | `info`, `warning` or `error`. `curamoir-lint` exits non-zero at or above the configured threshold. |
| `matcher` | `ast-grep` or `regex`. See below. |
| `pattern` | the ast-grep pattern, or the regex. |
| `message` | what the agent reads. May use `{path}`, `{line}`, `{col}`, `{match}`, `{rule}`, `{principle}`. |
| `fix` | the remediation. Optional, because some principles have no mechanical fix — but write one if you can, because it is the field that stops an agent retrying the same thing. |
| `target` | `line` (default) or `path`, for rules about filenames. |

## Exactly one principle

This is the constraint everything else rests on, so it is worth being plain
about why.

E4 promotes a principle on evidence: it counts how often violations of it are
actually being fixed (E3-R5). That count is per principle, and it comes from
the rules that cite it. So:

- a rule citing **no** principle contributes its fixes to nothing;
- a rule citing **two** contributes each fix to neither, because a fix
  attributed to two causes is attributed to no cause;
- a rule citing an id that **does not exist** lints perfectly normally and
  silently stops the ladder for that principle.

The last one is the dangerous case, because nothing looks wrong. So the id is
resolved against `principles.md` when the rule loads, and an unknown id is a
hard error that lists the ids that do exist. If you want one rule for two
principles, you want two rules.

## Which matcher

**Reach for `ast-grep` first.** It matches the parsed structure of the code, so
it survives reformatting, renaming and line wrapping — the three things that
make a regex rule fire on Tuesday and not on Wednesday. `print($$$ARGS)` finds
a call however its arguments are laid out; `print\(` finds the four characters
and also finds them in a string, a comment and the word `sprint(`.

**Reach for `regex` when there is no AST to match**, which is a real and
recurring situation:

| case | why ast-grep cannot |
|---|---|
| a file NAME, like `handlers_v2.py` | the filename is not in the file's syntax tree |
| a comment, like `TODO` without an issue id | comments are not nodes in most grammars |
| prose — a commit message, a changelog entry | not code at all |
| a config file with no supported grammar | nothing to parse |
| a value spelled across a language boundary | one tree per language |

Use `regex` for those and say so in the rule. Do not use it because the
ast-grep pattern was fiddly: a fiddly structural pattern is still the one that
will be right in six months.

`ast-grep` is a separate binary. `/curamoir:doctor` reports whether it is
installed. When it is not, `curamoir-lint` **skips** those rules, still runs the
regex ones and prints what they found, names the skipped rules on stderr, and
exits 2 — a lint that quietly checks nothing is worse than one that fails, and
a gate that passes on a machine missing its matcher passes everywhere it is
missing. Install it with one of:

```
pip install ast-grep-cli
npm install -g @ast-grep/cli
brew install ast-grep
```

## Scoping

A rule with neither `languages` nor `files` applies to every language cúramóir
knows how to name. That is usually right for something like an orphaned TODO,
and usually wrong for anything syntactic.

`exclude` is for the exemptions the principle itself states. `structured-logging`
says CLI entry points are the exception, because that is where logging is
initialised — so its rule excludes them, and the exclusion and the principle
agree because both are written down:

```yaml
exclude: ["**/cli.py", "**/__main__.py", "**/conftest.py"]
```

Globs are matched against repo-relative, `/`-separated paths. `**/` matches
zero or more directories (so `**/*.py` includes a root-level `setup.py`); `*`,
`?` and character classes match within one path segment. Classes follow
fnmatch: `[abc]`, `[a-z]`, `[!abc]` for "none of these", `[[]` for a literal
`[` — so a Next.js route file `app/[id]/page.tsx` is `app/[[]id]/page.tsx` —
and an unclosed `[` is literal. A class that cannot compile, like `[z-a]`, is a
load error.

An `ast-grep` rule with no `languages` or `files` searches only the languages
ast-grep can parse (not `markdown` or `toml`).

Some directories are never searched whatever a rule says:

- at any depth: `.git`, `.venv`, `__pycache__`, `node_modules`,
  `site-packages`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.beads` and
  `.curamoir` — tooling and dependency directories no source package is
  named;
- at the repository root only: `build`, `dist` and `venv` — output and
  environment directories there, but ordinary package names below it
  (`src/app/build/`, `cmd/dist/`), which are searched. A nested virtualenv
  named `venv` is therefore searched too; exclude it in the rule
  (`exclude: ["**/venv/**"]`). "Root" is the directory given as `--repo`:
  linting a monorepo's subpackage with `--repo packages/web` prunes
  `packages/web/build`, not `build` at the top of the monorepo.

## The message is read by an agent

E2-R4's output goes to a machine that will try to act on it, which changes what
a good message is. It is not a label — it is an instruction.

```yaml
# Weak: true, and leaves the agent to guess.
message: "unstructured logging"

# Better: says what is wrong with THIS line.
message: "{match} writes to stdout with no level or logger name"
```

And `fix` is the half that stops a retry loop. An agent told only "no" tries the
same thing again or invents something worse; an agent told what to do instead
does that. This is the same reasoning as the command guard's refusals, which
are required to name the safe alternative.

## Everything is checked when the rule loads

Not when it runs. A sweep that dies on rule 7 of 11 has already paid for six,
and the failure arrives without a file to open. So all of these are load errors
naming the file and the field:

- a missing required field
- a principle id that does not resolve
- more than one principle
- an unknown severity or matcher
- a regex that does not compile
- a regex that matches the empty string in any text (`x*`, `^`, `(foo)?`,
  `.*`, `$`, `\s*$`), which would report every line; `^$`, which matches only
  empty lines, and `\s+$` are fine
- a `target: line` regex containing a newline (`\n`, `\x0a`, or a literal
  one) outside a character class: lines are matched without their newline,
  so it could never match. `[^\n]` is fine
- a key given twice in one mapping (YAML would silently keep the last)
- a YAML anchor, alias or merge key (`&name`, `*name`, `<<:`), here and in
  `curamoir.yaml`: each sets a value away from the key it applies to, where a
  reader of that key does not see it, and nothing in files this small needs
  them
- anything in `.curamoir/rules/` that is not a rule, a note or a dotfile (see
  the top of this page), or a `.curamoir/rules` that is not a directory
- a message using a variable that does not exist
- two rules sharing an id
- an unknown language
- `target: path` with the `ast-grep` matcher (a filename is in no syntax tree,
  so the rule could never fire)
- the `ast-grep` matcher over `languages: [markdown]` or `[toml]`, which
  `curamoir-lint` has no ast-grep grammar for (the rule would search nothing)
- a `files` or `exclude` glob that does not compile

`/curamoir:doctor` loads every rule for exactly this reason: so a broken rule is
something you find when you ask, rather than during an unattended run.

## Running `curamoir-lint`

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

### Exit codes

| code | meaning |
|---|---|
| `0` | every rule ran, and no violation is at or above the threshold |
| `1` | every rule ran, and at least one violation is at or above the threshold |
| `2` | the result cannot be trusted: a rule, `principles.md` or `curamoir.yaml` did not load (including a key given twice, or a `curamoir.yaml` that is a directory); `ast-grep` is needed and missing, failed, did not finish, or printed something other than a list of matches; a rule could not search every file in its scope (an `ast-grep` rule over a file it has no grammar for, or a file ast-grep skipped; any rule over a file that cannot be read); a directory in the repository could not be listed; the run exceeded `--timeout`; an invalid or empty option (`--repo ""`); no Python was found; the report could not be written (stdout closed, or an encoding that cannot represent it — set `PYTHONIOENCODING=utf-8`); or an internal error |

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

### JSON (`--format json`)

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
over the same tree are byte-identical.

Strings are exact, with control characters carried as JSON escapes, with one
exception: a filename that is not valid UTF-8 has its bad bytes shown escaped
(`bad\xff.py`), in `path`, `location` and a note's `path` alike.

#### Fingerprint

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

#### Notes

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
  linted (see *Limits*).
- `not_regular_file` (keys: `kind`, `path`): a FIFO, socket or device at
  `path` was not read.

These are the facts the stderr notes give; stderr is unchanged by them. None
changes the exit code.

#### Versioning

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

### Human (the default)

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

### In CI and pre-commit

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

### Limits, stated

What the runner does not do, or does differently from what you might assume.
The first group matters when writing a rule; the second when wiring the
runner into CI or pre-commit.

**For rule authors**

- Files are found by walking the repository, not with `git ls-files`, so the
  runner works on an export or a shallow checkout. `.gitignore` is not read; the
  directories listed under *Scoping* are always skipped.
- An `ast-grep` rule searches only suffixes with a mapped grammar: `.py`
  `.pyi`, `.ts` `.mts` `.cts`, `.tsx`, `.js` `.jsx` `.mjs` `.cjs`, `.go`,
  `.rs`, `.yaml` `.yml`, `.json`, `.java`, `.kt`, `.scala`, `.c`, `.cpp` `.cc`
  `.hpp`, `.cs`, `.rb`, `.php`, `.swift`, `.lua`, `.sh` `.bash`, `.html`,
  `.css` (each checked against ast-grep 0.50.0). Markdown, TOML and `.h` are
  not among them; use `regex` there. If a rule's `files:` scope takes in any
  other file, the rule is **skipped**, not narrowed: it is listed in
  `rules_skipped` with the files named, the other rules' report is still
  printed, and the run exits 2. Dropping the file instead would report the
  rule as checked having searched nothing. Narrow `files:` or add the file to
  `exclude:`.
- The one file an `ast-grep` rule leaves out of its scope is one whose whole
  name is a suffix, such as a file named `.py`: it matches `**/*.py`, but
  ast-grep does not search it under `--lang=python` either, and counting it
  as a file with no grammar would let any commit skip the rule. A `regex`
  rule still searches it.
- Lines are counted the way ast-grep counts them: only `\n` ends one. A CRLF
  file reads as its lines; a form feed or a U+2028 inside a string does not
  start a new line, and neither does a lone `\r` (which some editors do break
  on — the two matchers agree with each other there, not with those editors).
  Columns are 1-based characters.
  A UTF-8 byte order mark is not a column: the regex matcher skips it, and
  ast-grep's line-1 columns in such a file (which 0.50.0 shifts by one) are
  corrected, so both matchers agree with the editor.
- Symlinks are not linted (see below), even when the target is inside the
  repo — that target is linted at its own path — and a `target: path` rule
  does not see a symlink's name. Nor are FIFOs, sockets or devices (opening a
  FIFO blocks forever); like symlinks, they are named on stderr and do not
  change the exit code — git cannot store them, so they are not repository
  content.
- ast-grep silently skips a file it cannot read, cannot decode as UTF-8 (a
  Latin-1 source with a coding cookie, say), or finds too large (0.50.0
  searched a 3 MB file and skipped a 4 MB one). `curamoir-lint` asks for its
  `--inspect=summary` count, and a rule whose files were skipped is
  **skipped** like one over a file with no grammar: listed in `rules_skipped`
  with the likely files named, exit 2. Re-encode the file as UTF-8 or add it
  to `exclude:`. Empty files are not given to ast-grep at all (0.50.0 counts
  them as skipped, and they can hold no match). UTF-16 sources are not
  handled specially by either matcher.
- A file a `regex` rule cannot read (permissions) skips that rule, named, the
  same way; a directory that cannot be listed fails the whole run, since no
  rule could check what is under it.
- A `regex` rule decodes each file as UTF-8, replacing bytes that are not; a
  binary file in a broad `files:` scope is searched as such. Scope rules to
  the files they mean.
- Settings a project put in its own `sgconfig.yml` (custom languages,
  `languageGlobs`) do not apply to `curamoir-lint`'s rules; see below for why.

**For CI and pre-commit**

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
