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
installed. When it is not, the lint runner will **skip** those rules with a
warning rather than silently passing them — a lint that quietly checks nothing
is worse than one that fails. (That behaviour belongs to the runner, E2-R4,
which has not landed; the doctor check has.) Install it with one of:

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

`/curamoir:doctor` loads every rule for exactly this reason: so a broken rule is
something you find when you ask, rather than during an unattended run.
