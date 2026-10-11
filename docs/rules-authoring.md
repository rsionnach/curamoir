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

## Limits, for rule authors

What the runner does not do, or does differently from what you might assume,
that matters when writing a rule. The limits that matter when wiring the runner
into CI or pre-commit are in
[Running `curamoir-lint`](curamoir-lint.md#limits-for-ci-and-pre-commit).

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
- Symlinks are not linted (see
  [why](curamoir-lint.md#limits-for-ci-and-pre-commit)), even when the target
  is inside the repo — that target is linted at its own path — and a `target: path` rule
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
  `languageGlobs`) do not apply to `curamoir-lint`'s rules; see
  [why](curamoir-lint.md#limits-for-ci-and-pre-commit).

## Running it

```
scripts/curamoir-lint [--repo PATH] [--format human|json] [--fail-on info|warning|error]
```

Run it from the repository root, or point `--repo` at it. It loads every rule
in `.curamoir/rules/`, prints one block per violation, and exits `0` when no
violation is at or above the threshold (`--fail-on`, else `rules.fail_on` in
`.curamoir/curamoir.yaml`, else `error`), `1` when one is, and `2` when the
result cannot be trusted — a rule that did not load, say, or one that could
not search every file in its scope. Everything else about running it — every
option, the exit codes in full, the JSON report and its stability promise,
wiring it into CI and pre-commit — is in
[Running `curamoir-lint`](curamoir-lint.md), which is written for whoever wires
the gate or parses its output rather than for rule authors.
