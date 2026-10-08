# Golden principles — sample fixture

Three principles, in E2-R1's required structure, defining the rules this
fixture's planted violations violate.

**Why these exist here.** `PLANTED.json` tags each planted violation with a
`principle_id`, and until this file existed those ids resolved to nothing: the
fixture asserted a *reading* of three rules that were named but never defined.
Fixture provenance review flagged the consequence — when E2-R2 authors the real
starter set, the pressure would run the wrong way, with the rule written to
match the fixture rather than the fixture checked against the rule, making E3's
acceptance test self-confirming.

**Provenance.** All three concepts come from the spec, `docs/curamoir-spec.md`
E2-R2 (line 245), which names "structured logging only", "errors carry context"
and "no file-variant proliferation like `foo_v2.py`" among the starter set. The
*wording* of each rule, why, good and bad below is this fixture's, not the
spec's, and E2-R2 remains free to define them differently — in which case this
file and `PLANTED.json` follow, not the other way round.

---

## structured-logging

- **id:** `structured-logging`
- **title:** Structured logging only
- **rule:** Emit diagnostics through the module logger, never through `print`.
- **why:** A `print` goes to stdout with no level, no logger name and no
  structure, so it cannot be filtered, routed or correlated. In a job running
  unattended that is the difference between a signal and noise nobody reads.
- **good:** `logger.info("line total computed", extra={"total": total})`
- **bad:** `print("calculating line total")`
- **tier:** principle
- **promoted_on:** null
- **source:** docs/curamoir-spec.md E2-R2 — "structured logging only"

## errors-carry-context

- **id:** `errors-carry-context`
- **title:** Errors carry context
- **rule:** When re-raising, name the operation that failed and chain the cause.
- **why:** A bare re-raise discards both the original traceback and the input
  that failed, so the reader learns that something went wrong and nothing about
  what. The cause is the whole value.
- **good:** `raise RuntimeError(f"could not load rates from {path}") from error`
- **bad:** `raise RuntimeError("could not load rates")`
- **tier:** principle
- **promoted_on:** null
- **source:** docs/curamoir-spec.md E2-R2 — "errors carry context"

## no-file-variant-proliferation

- **id:** `no-file-variant-proliferation`
- **title:** One implementation per concept
- **rule:** Do not add a parallel `_v2`, `_new` or `_old` variant of a module;
  change the original.
- **why:** Two implementations of one concept drift, and every later reader has
  to work out which is live. The variant is usually created to avoid a risky
  edit and then outlives the reason, so the risk is paid twice.
- **good:** `handlers.py`, edited in place, with the old behaviour in git
- **bad:** `handlers.py` alongside `handlers_v2.py`
- **tier:** principle
- **promoted_on:** null
- **source:** docs/curamoir-spec.md E2-R2 — "no file-variant proliferation like `foo_v2.py`"
