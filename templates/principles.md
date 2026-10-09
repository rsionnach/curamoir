# Golden principles

The rules this repository holds itself to. `cúramóir` reads this file to decide
what to look for; `curamoir-lint` enforces the ones that have a rule written
against them; `/curamoir:promote` moves one up the ladder when it has earned it.

**Edit this file.** It is yours, not the plugin's. A principle nobody in this
repository believes is one that gets argued with in every review and enforced in
none, which is worse than not having it. Delete what does not apply, reword what
does, and add the corrections you find yourself repeating.

**What belongs here.** A golden principle is:

- **opinionated** — it makes a choice. "Prefer X over Y", not "consider X".
- **mechanical** — it can be checked without human judgement.
- **justified** — the `why` names the real failure mode, not the preference.
- **enforceable** — there is a clear path to checking it in code.

If a rule needs human judgement to evaluate, it is a *convention*, not a golden
principle. Write it in a CONTRIBUTING or conventions document instead; putting
it here means it can never be promoted, and a tier it can never leave is a
permanent argument.

**The ladder** (E4). Each principle carries a `tier`:

| tier | what it means | who enforces it |
|---|---|---|
| `principle` | written down and reviewed against | a human, or a review pass |
| `rule` | has a rule file and fails the lint | `curamoir-lint`, CI, pre-commit |
| `guard` | blocks the edit that would introduce it | a `PreToolUse` hook |

A principle starts at `principle` and is promoted on **evidence**: when you have
corrected the same violation three times, promote it. `cúramóir` counts those
corrections for you (E3-R5) and `/curamoir:promote` records the date in
`promoted_on`. A principle nobody violates does not need to become a guard.

**Where this set came from.** Extracted from the NthLayer ecosystem's
`docs/golden-principles.md` and `docs/conventions.md`, genericised to drop
project-specific references — per the spec's operating assumption 5,
"extraction, not invention". Each principle's `source` field says which one it
came from, and the two marked `spec` are additions the specification names that
the source set did not have. The `why` sections are kept close to the original
wording, because they describe failures someone actually observed; several of
them name *agent* behaviour specifically, which is the point.

---

## validate-at-the-boundary

- **id:** `validate-at-the-boundary`
- **title:** Validate at the boundary, not inline
- **rule:** Validate every external input at the entry point, with a schema or
  typed parse.
- **why:** Agents love writing `if config.get("field")` checks deep in call
  chains. That produces duplicated validation, inconsistent error messages, and
  fields that are validated on some paths and not others — so the same bad
  input is rejected in one code path and silently accepted in another. Checking
  once, at the edge, is also the only version a reader can verify by looking at
  one place.
- **good:** a schema or typed parse at the CLI, handler or loader, passing
  validated values inward
- **bad:** `if config.get("timeout"):` in a function three layers down
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer golden-principles #1; spec E2-R2 "no config checks deep
  in call chains"

## shared-utilities

- **id:** `shared-utilities`
- **title:** Shared utilities over hand-rolled helpers
- **rule:** If a pattern appears more than twice, move it to a shared module.
- **why:** Every fresh agent context window reinvents helpers slightly
  differently, so the same concept ends up with three implementations that
  disagree at the edges — and the disagreement is invisible, because each one is
  locally correct. Centralising keeps the invariant in one place. The usual
  culprits are duration formatting, label and input validation, string
  sanitisation, retry and backoff, and collection helpers.
- **good:** one `format_duration` in a common module, imported where needed
- **bad:** three near-identical `format_duration` functions in three packages
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer golden-principles #2; spec E2-R2 "one implementation per
  concept"

## structured-logging

- **id:** `structured-logging`
- **title:** Structured logging only
- **rule:** Emit diagnostics through the configured logger, never through
  `print`.
- **why:** A `print` carries no level, no logger name and no structure, so it
  cannot be filtered, routed or correlated — and it cannot be turned off. In a
  job running unattended for a month that is the difference between a signal and
  output nobody reads. It is also the most common thing left behind by
  debugging, which makes it a reliable marker of unfinished work. CLI entry
  points, where logging is initialised, are the exception.
- **good:** `logger.info("line total computed", extra={"duration_ms": 12})`
- **bad:** `print("calculating line total")`
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer golden-principles #3 (LINT:
  check-no-unstructured-logging.sh); spec E2-R2 "structured logging only"

## consistent-log-fields

- **id:** `consistent-log-fields`
- **title:** One name per logged concept
- **rule:** Use the same field name for the same concept in every log line.
- **why:** Structured logging buys you queryability and nothing else, and a
  field spelled three ways cannot be queried: a dashboard filtering on
  `duration_ms` silently misses every line that said `elapsed`. The cost is
  paid entirely at read time, during an incident, by someone who did not write
  the line. Pick the names once — `error` for errors, `component` for the
  subsystem, `duration_ms` for timing — and write them down here.
- **good:** `logger.warning("fetch failed", extra={"error": err, "component": "sync"})`
- **bad:** the same event logged with `exc`, `pkg` and `took` elsewhere
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer conventions "Structured Logging" — field naming

## errors-carry-context

- **id:** `errors-carry-context`
- **title:** Errors carry context at layer boundaries
- **rule:** When re-raising, name the operation that failed and chain the cause.
- **why:** Uncontextualised exceptions produce tracebacks with no indication of
  what the code was trying to do, and the chained cause is what makes the
  difference between a stack trace and an explanation. A bare re-raise also
  discards the input that failed, so the reader learns that something went wrong
  and nothing about what. The cost lands weeks later on whoever reads the log,
  which is why it survives review: the person writing it is not the person who
  pays.
- **good:** `raise RateLoadError(f"loading rates from {path}") from error`
- **bad:** `raise RuntimeError("could not load rates")`
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer golden-principles #4, first half (LINT:
  check-exception-handling.sh); spec E2-R2 "errors carry context"

## no-silent-failure

- **id:** `no-silent-failure`
- **title:** No silent failure
- **rule:** Never swallow an exception without recording it or saying why.
- **why:** Agents frequently write bare `except: pass` blocks or catch-all
  handlers that discard error context, which converts a loud failure into a
  wrong answer — and a wrong answer propagates. This is the failure mode an
  autonomous agent handles worst: it will retry around a swallowed error
  indefinitely, because nothing told it the step did not happen. Deliberately
  ignoring an error is allowed when it is marked as such, so the next reader
  knows it was a decision.
- **good:** `except OSError as error:  # intentionally ignored: cache is optional`
- **bad:** `except Exception:\n    pass`
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer golden-principles #4, second half — split out because it
  has a different matcher and a different fix from the re-raise half

## typed-errors

- **id:** `typed-errors`
- **title:** Raise typed errors, not bare ones
- **rule:** Raise a domain error class, never a bare `Exception` or
  `RuntimeError`.
- **why:** A caller can only handle what it can name. Raising the base class
  forces every handler upstream to catch everything or nothing, so one
  recoverable condition cannot be distinguished from a bug — and the usual
  result is a catch-all that hides both. A small hierarchy rooted in one project
  error also gives the caller a single thing to catch at the boundary, which is
  what makes `errors-carry-context` practical rather than tedious.
- **good:** `class RateLoadError(ProjectError): ...` then `raise RateLoadError(...)`
- **bad:** `raise RuntimeError("could not load rates")` in application code
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer conventions "Error Handling"

## generated-output-via-template

- **id:** `generated-output-via-template`
- **title:** Generated output goes through a template
- **rule:** Build generated files from templates, never by string
  concatenation.
- **why:** Raw string construction is the single most likely source of
  correctness bugs in generated output: escaping, quoting and indentation are
  each one forgotten case away from producing a file that parses and means
  something else. A template centralises all three, and it fails loudly on a
  missing field where concatenation silently emits an empty one. The output of
  a generator is also usually unreviewed, so it has to be right by construction.
- **good:** a template rendered with validated values
- **bad:** `yaml = "name: " + name + "\n  rules:\n" + "".join(parts)`
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer golden-principles #5, genericised from its
  Prometheus/Grafana wording

## todo-carries-an-id

- **id:** `todo-carries-an-id`
- **title:** No orphaned TODOs
- **rule:** Every TODO, FIXME, HACK or XXX cites a tracked issue id.
- **why:** Agents generate TODOs liberally as placeholders, and without
  tracking they accumulate indefinitely: nothing lists them, nothing schedules
  them, and the context that would make one actionable leaves with the session
  that wrote it. An id turns a deferred decision into work that can be
  prioritised — or closed as wontfix on purpose, which is also a result.
- **good:** `# TODO(cm-82e): stream the ledger instead of materialising it`
- **bad:** `# TODO: make this faster later`
- **tier:** principle
- **promoted_on:** null
- **source:** nthlayer golden-principles #6 (LINT: check-no-orphan-todos.sh)

## no-file-variant-proliferation

- **id:** `no-file-variant-proliferation`
- **title:** One file per concept
- **rule:** Do not add a parallel `_v2`, `_new` or `_old` variant of a module;
  change the original.
- **why:** Two implementations of one concept drift, and every later reader has
  to work out which one is live. The variant is usually created to avoid a risky
  edit, then outlives the reason, so the risk is paid twice — once in the
  duplication and again when the two diverge. Version control already keeps the
  old behaviour; a file does not need to.
- **good:** `handlers.py`, edited in place, with the previous behaviour in git
- **bad:** `handlers.py` alongside `handlers_v2.py`
- **tier:** principle
- **promoted_on:** null
- **source:** spec E2-R2 "no file-variant proliferation like `foo_v2.py`" — an
  addition; the extracted set has no equivalent

## tests-assert-behaviour

- **id:** `tests-assert-behaviour`
- **title:** Tests assert behaviour, not implementation
- **rule:** Assert on what the code returns or changes, not on how it got there.
- **why:** A test that asserts a mock was called passes when the production code
  is wrong and fails when it is merely refactored — exactly inverted. It is the
  most expensive kind of test to own, because it blocks the changes you want and
  permits the bugs you do not. The question to ask of any assertion is: which
  production change would make this fail? If the answer is "none", the test is
  decoration.
- **good:** `assert write_handoff(repo) is not None and latest.read_text()`
- **bad:** `assert mock_writer.called`
- **tier:** principle
- **promoted_on:** null
- **source:** spec E2-R2 "tests assert behaviour not implementation" — an
  addition. Note it needs judgement to evaluate, so by the criteria above it
  sits closer to a convention than a golden principle; it is here because the
  specification names it, and a rule for it will only ever catch the obvious
  cases.
