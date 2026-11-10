# opensrm-jmy.22: interactive TUI walkthrough for learn recommendations

Add `--interactive` TUI walkthrough for `nthlayer-workers learn recommendations`. Operator inspects each recommendation, accepts/rejects/modifies, then the accepted set is applied.

Discovered during jmy.6 scoping (2026-05-26). Original jmy.6 description listed `--interactive` as item #2 but it's UX work distinct from the Learn→Spec loop foundation (which is `--output` + `--apply-to` + `--pr`).

Scope:
- Textual-based screen showing one recommendation at a time
- Bindings: a (accept), r (reject), m (modify proposed_value inline), n (next), p (previous), q (quit, applies accepted set)
- Visual diff preview: current spec value vs proposed_value (side-by-side or before/after)
- Reuse VerdictAccuracy-style render conventions from nthlayer-bench (markup=False, scenario-named tests, async logic in pure module + Textual widget on top)
- Apply set flows back into the same `--apply-to` machinery jmy.6 built

Design questions to resolve:
- Single screen with carousel, or list view with detail pane?
- Where does the TUI live — nthlayer-workers (with the CLI) or nthlayer-bench (with the operator UI)? Bench seems more natural; workers ships agentic code.
- Can the TUI emit a partial-accept set + a deferred set (a "review again later" bucket)?

Priority: P3 — UX polish on the Learn→Spec loop; not required for the loop itself.
Effort: 1-2 sessions including R5.
Discovery: jmy.6 scoping (decomposition approved 2026-05-26).
Dependency: depends-on jmy.6 (the apply-to machinery is the consumer of the accepted set).
