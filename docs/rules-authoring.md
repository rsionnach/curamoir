# Writing a rule

A rule turns one principle into something a machine checks. It lives in
`.curamoir/rules/<rule-id>.yaml`, and `curamoir-lint` runs it.

Rules are how a principle gets promoted from `principle` to `rule` on the
ladder — so you write one when you have corrected the same violation about
three times and are tired of saying it.

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

Some directories are never searched whatever a rule says: `venv`, `.venv`,
`__pycache__`, `node_modules`, `dist`, `build`, `.git`, `.curamoir`,
`site-packages` and the usual caches.

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
```

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
| `2` | the result cannot be trusted: a rule, `principles.md` or `curamoir.yaml` did not load, `ast-grep` is needed and missing or failed, an `ast-grep` rule's scope includes a file ast-grep cannot parse, no Python was found, the report could not be written (stdout closed, or an encoding that cannot represent it — set `PYTHONIOENCODING=utf-8`), or an internal error |

`2` outranks `1`. A run that skipped rules has not shown them clean, so it is
reported as incomplete even if what did run found something. When rules were
skipped the partial report is still printed; for every other `2`, stdout is
empty and the reason is on stderr.

The threshold is `--fail-on`, else `rules.fail_on` in `.curamoir/curamoir.yaml`,
else `error`. Every violation is reported whatever its severity; the threshold
decides only the exit code. A `curamoir.yaml` that does not validate is a `2`,
not a fall-back to the default — a gate loosened without anyone being told is
the failure this tool exists to prevent.

No rules at all is a `0`, with a note on stderr that nothing was checked: a
fresh repo is not a failing one.

### JSON (`--format json`)

The sweep's work list (E3-R1), so the shape is a contract. `version` changes
only when a field is removed, renamed or retyped.

```json
{
  "version": 1,
  "fail_on": "error",
  "complete": true,
  "rules_checked": ["no-bare-print"],
  "rules_skipped": [],
  "violations": [
    {
      "location": "src/sample/core/billing.py:15:5",
      "path": "src/sample/core/billing.py",
      "line": 15,
      "col": 5,
      "severity": "error",
      "rule_id": "no-bare-print",
      "principle_id": "structured-logging",
      "principle_rule": "Emit diagnostics through the module logger, never through `print`.",
      "message": "print( writes to stdout with no level or logger name",
      "fix": "logger.info(...), or logger.debug(...) while debugging",
      "why_link": ".curamoir/principles.md#structured-logging",
      "match": "print("
    }
  ]
}
```

| field | what it is |
|---|---|
| `location` | `path:line:col`, for anything that opens a location |
| `path` | repo-relative, `/`-separated |
| `line`, `col` | 1-based. A `target: path` rule reports `1:1`, the file itself |
| `severity` | the rule's |
| `rule_id`, `principle_id` | the rule and the one principle it cites |
| `principle_rule` | the principle's one-sentence `rule`, so the agent need not open `principles.md` |
| `message` | the rule's `message`, rendered |
| `fix` | the rule's `fix`; if it has none, the principle's `good` example, which is a corrected form by definition |
| `why_link` | `.curamoir/principles.md#<anchor>`: the principle's heading, where its `why` is |
| `match` | the text that matched (for a path rule, the part of the path) |

`violations` is sorted by path, line, column and rule id, so two runs over the
same tree are byte-identical. `rules_skipped` entries are
`{"rule_id": ..., "reason": ...}`, and `complete` is false exactly when there are
any.

### Human (the default)

```
src/sample/core/billing.py:15:5: error [structured-logging/no-bare-print] print( writes to stdout with no level or logger name
    rule: Emit diagnostics through the module logger, never through `print`.
    why:  .curamoir/principles.md#structured-logging
    fix:  logger.info(...), or logger.debug(...) while debugging
```

### In CI and pre-commit

CI needs a checkout of cúramóir, not an install:

```yaml
- run: pip install pyyaml ast-grep-cli
- run: git clone --depth 1 https://github.com/rsionnach/curamoir /tmp/curamoir
- run: /tmp/curamoir/scripts/curamoir-lint --format json > lint.json
```

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
- Lines are counted the way ast-grep counts them: only `\n` ends one. A CRLF
  file reads as its lines; a form feed or a U+2028 inside a string does not
  start a new line, and neither does a lone `\r` (which some editors do break
  on — the two matchers agree with each other there, not with those editors).
  Columns are 1-based characters.
  A UTF-8 byte order mark is not a column: the regex matcher skips it, and
  ast-grep's line-1 columns in such a file (which 0.50.0 shifts by one) are
  corrected, so both matchers agree with the editor.
- The wrapper never starts Python inside the repository. `python -m` puts its
  working directory ahead of everything else on `sys.path`, so a `yaml.py` or
  `curamoir/` committed at the root of a PR would otherwise run as the linter.
  Python starts in cúramóir's own `src/` and moves back to the invoking
  directory only after pinning its import path, so `--repo` still means what
  it meant where you typed it. An inherited `PYTHONPATH` is kept (after
  `src/`), as in every cúramóir wrapper: it is set by whoever runs the lint,
  not by the repository.
- Symlinks are not followed, file or directory. In CI the tree is often an
  unreviewed PR, and `match` quotes matched text, so a committed
  `leak.py -> ~/.ssh/id_rsa` would otherwise have the lint print part of the
  target. A symlink's name (never its target or contents) is listed on stderr;
  it does not change the exit code, and is absent from the JSON. The cost: a
  symlink is not linted even when its target is inside the repo — that target
  is linted at its own path — and a `target: path` rule does not see a
  symlink's name.
- The repository's own `sgconfig.yml` is never read. ast-grep looks for one in
  its working directory and every ancestor, and one in a PR could re-map file
  types so an `ast-grep` rule silently matches nothing, break the run, or (on
  ast-grep versions that load `customLanguages` native libraries without
  `--custom-languages allow`) run native code. So ast-grep runs from a scratch
  directory outside the repo with `--config` pointing at an empty config of
  cúramóir's own. The same holds in reverse: settings a project put in
  `sgconfig.yml` for its own ast-grep use (custom languages, `languageGlobs`)
  do not apply to `curamoir-lint`'s rules.
