# Bundled example rules

Working `curamoir-lint` rules written against the starter principles in
`templates/principles.md` — the set `/curamoir:init` seeds a repository with.
They exist for two reasons: so a new repository has something that runs on day
one, and so the rule format is shown working rather than described.
For the format itself, see `docs/rules-authoring.md`.

There is no `principles.md` in `examples/python/` or `examples/typescript/`, and
that is deliberate: both languages' rules cite the one shared, language-agnostic
set in `templates/principles.md`, which is what your repository's
`.curamoir/principles.md` starts as.

| file | principle | severity | pattern | finds |
|---|---|---|---|---|
| `python/no-print.yaml` | `structured-logging` | `error` | `print($$$ARGS)` | any call to `print`, outside `cli.py`, `__main__.py`, `conftest.py` |
| `python/no-raise-runtime-error.yaml` | `typed-errors` | `warning` | `raise RuntimeError($$$ARGS)` | `raise RuntimeError(...)`, outside `tests/`, `test_*.py`, `*_test.py`, `conftest.py` |
| `python/no-assert-mock-called.yaml` | `tests-assert-behaviour` | `warning` | `assert $MOCK.called` | `assert <anything>.called`, usually a test asserting that a mock was called |
| `typescript/no-console-log.yaml` | `structured-logging` | `error` | `console.log($$$ARGS)` | any call `console.log(...)` in `.ts`/`.tsx` |

All four are `ast-grep` rules, the default matcher. Each is tested against a
planted violation in `fixtures/sample-repo` and must find exactly what is
planted there — no more — by `tests/test_bundled_rules.py`.

## Installing them

Copy the files you want into your repository's `.curamoir/rules/`, **flat**:

```
cp -n examples/python/*.yaml     /path/to/repo/.curamoir/rules/
cp -n examples/typescript/*.yaml /path/to/repo/.curamoir/rules/
```

`-n` (no-clobber) because an installed rule is yours to edit — `no-print`'s
`exclude` is meant to grow your entry points — and a plain re-copy would
silently undo that. So re-running skips files already installed; to update one,
diff it against the example first so your `exclude` edits survive.

Not the directories: the loader refuses a subdirectory in `.curamoir/rules/`,
because a rule filed where it does not look would never run. The rule ids are
unique across both languages, so both sets can be installed together.

`/curamoir:init` does not copy them for you. At the default threshold an
`error` rule fails the lint (exit 1) and a `warning` rule is reported without
failing it; lower the threshold with `rules: {fail_on: warning}` in
`.curamoir/curamoir.yaml`, or `curamoir-lint --fail-on warning` for one run, and the
warnings fail too. Either way a repository should choose which of its
principles are worth reporting, let alone failing a build over — the ladder's
point is that a principle is promoted to a rule on evidence, not by default.
Each rule file's opening comment says why it has the severity it has.

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
- `no-raise-runtime-error` matches only the called form. It does not match
  `raise RuntimeError` with no call, `raise builtins.RuntimeError(...)`, or
  `raise Exception(...)`; each would be another rule with the same principle
  (one pattern per rule).
- `no-assert-mock-called` does not match `mock.assert_called_once_with(...)`,
  `assert mock.call_count == 1`, `assert m.called is True`,
  `assert not m.called` or `self.assertTrue(m.called)`. And `$MOCK` matches
  any receiver, not only a mock: a domain object with its own `called` flag is
  reported too, which is a false positive, so `exclude` that file. The
  principle needs judgement in general; a pattern catches only its most
  obvious form.
- `no-console-log` does not match `console.error`, `console.warn`,
  `console.debug` or optional chaining (`console?.log(...)`). It is here to
  show the rule format is not Python-specific, so it is kept deliberately
  minimal.
- `languages: [typescript]` covers `.ts` and `.tsx` only. Plain JavaScript
  (`.js`, `.jsx`, `.mjs`) is another language to the loader, so it needs a
  copy of `no-console-log` with its own `id` and `languages: [javascript]`.
  `.mts`, `.cts` and `.cjs` are covered by neither yet (bead cm-dpd), so a
  `console.log` in one of those is not reported.
- A rule with no files in its scope — the TypeScript rule in a Python-only
  repo, say — is still listed under `rules_checked`, so "checked" can mean
  zero files were searched. It is not evidence that the language is clean.
- There is no bundled `errors-carry-context` rule. Its violation is a `raise`
  inside an `except` block *without* `from`, and a single ast-grep pattern
  cannot say "without": `raise $E($$$A)` also matches `raise E(...) from err`.
  The rule format would need ast-grep's relational rules (`inside`, `not`) to
  express it; until then it stays a principle, checked in review.
- `no-file-variant-proliferation` (`handlers_v2.py`) is a filename, which no
  AST pattern can match. It is the documented case for the `regex` matcher with
  `target: path` (see `docs/rules-authoring.md`), and is not bundled here.
