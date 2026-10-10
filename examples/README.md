# Bundled example rules

Working `curamoir-lint` rules written against the starter principles in
`templates/principles.md` — the set `/curamoir:init` seeds a repository with.
They exist for two reasons: so a new repository has something that runs on day
one, and so the rule format is shown working rather than described (E2-R6).
For the format itself, see `docs/rules-authoring.md`.

| file | principle | pattern | finds |
|---|---|---|---|
| `python/no-print.yaml` | `structured-logging` | `print($$$ARGS)` | any call to `print`, outside `cli.py`, `__main__.py`, `conftest.py` |
| `python/no-raise-runtime-error.yaml` | `typed-errors` | `raise RuntimeError($$$ARGS)` | raising the base `RuntimeError` in application code |
| `python/no-assert-mock-called.yaml` | `tests-assert-behaviour` | `assert $MOCK.called` | a test asserting that a mock was called |
| `typescript/no-console-log.yaml` | `structured-logging` | `console.log($$$ARGS)` | any `console.log` in `.ts`/`.tsx` |

All four are `ast-grep` rules (DEC-3). Each is tested against a planted
violation in `fixtures/sample-repo` and must find exactly what is planted
there — no more — by `tests/test_bundled_rules.py`.

## Installing them

Copy the files you want into your repository's `.curamoir/rules/`, **flat**:

```
cp -f examples/python/*.yaml     /path/to/repo/.curamoir/rules/
cp -f examples/typescript/*.yaml /path/to/repo/.curamoir/rules/
```

Not the directories: the loader refuses a subdirectory in `.curamoir/rules/`,
because a rule filed where it does not look would never run. The rule ids are
unique across both languages, so both sets can be installed together.

`/curamoir:init` does not copy them for you. A rule fails the lint, and a
repository should choose which of its principles are worth failing a build
over — the ladder's point is that a principle is promoted to a rule on
evidence, not by default.

Each rule cites one principle, and the loader refuses a rule whose principle is
not in your `principles.md`. If you deleted or renamed `typed-errors`, say,
delete or edit `no-raise-runtime-error.yaml` too; that refusal is what stops a
rule from linting while counting its fixes towards nothing.

`ast-grep` must be installed (`pip install ast-grep-cli`); without it
`curamoir-lint` skips these rules and exits 2 rather than passing.

## What they do not catch

Each is one pattern, and a pattern says exactly one thing. Stated so nobody
mistakes a clean run for more than it is:

- `no-print` exempts CLI entry points by file name only. A `print` to stderr
  in some other module that runs as an entry point (a hook script, say) is
  reported; exclude that file, or route it through a logger. Run against
  cúramóir's own `src/`, it reports four such lines.
- `no-raise-runtime-error` does not match `raise Exception(...)`; that is a
  second rule with the same principle (one pattern per rule).
- `no-assert-mock-called` does not match `mock.assert_called_once_with(...)` or
  `assert mock.call_count == 1`. The principle needs judgement in general; a
  pattern catches only its most obvious form.
- `no-console-log` does not match `console.error`, `console.warn` or
  `console.debug`. It is the second-language proof (operating assumption 6),
  kept deliberately minimal.
- There is no bundled `errors-carry-context` rule. Its violation is a `raise`
  inside an `except` block *without* `from`, and a single ast-grep pattern
  cannot say "without": `raise $E($$$A)` also matches `raise E(...) from err`.
  The rule format would need ast-grep's relational rules (`inside`, `not`) to
  express it; until then it stays a principle, checked in review.
- `no-file-variant-proliferation` (`handlers_v2.py`) is a filename, which no
  AST pattern can match. It is the documented case for the `regex` matcher with
  `target: path` (see `docs/rules-authoring.md`), and is not bundled here.
