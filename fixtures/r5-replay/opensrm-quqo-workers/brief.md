PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-quqo, checkpoint 2 (nthlayer-workers).

Template body:
You are reviewing code changes with a SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.
Checklist: logic errors; type mismatches across function boundaries; error handling / uncaught propagation; unguarded None access; off-by-one; race conditions; return values; API contracts (do signatures match ALL callers — grep for every caller of changed functions, including tests and other packages); state mutations; resource cleanup.
What to read: the diff; the requirements below; for each changed function, its callers.

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-workers-quqo-log-events diff a0276ad..48f1f1b
Do not raise findings on code outside that diff. Pre-change view: `git -C <path> show a0276ad:<file>`; post-change: read files at the path.

Affected repos:
nthlayer-workers: base=a0276ad, head=48f1f1b, path=<workspace>/nthlayer-workers-quqo-log-events
The shared helper it builds on (read-only, already gated and released as 5.11.0): <workspace>/nthlayer-common/src/nthlayer_common/telemetry.py (emit_event, init_telemetry, capture_events).

Bead: opensrm-quqo — OTel telemetry was never emitted: workers' measure emitters called span.add_event on the current span, but nothing ever made a span recording, so every event was dropped. Rulings: Q1 all NthLayer events become OTel log records via common's emit_event, no TracerProvider in workers. Q2 carriage: LogRecord.event_name = name, no body, attributes only, severity INFO(9), Timestamp = the event's own time (A1 T1, never ObservedTimestamp). Q5 tests assert via capture_events(), never by patching telemetry internals.
Checkpoint 2 scope: move measure/telemetry.py's four emitters onto emit_event with real event timestamps; call init_telemetry("nthlayer-workers") in every CLI entry point (workers serve/gate, the measure, observe, learn, respond and correlate CLIs; the __main__ modules call those mains); add nthlayer-common[otel]; rewrite tests with capture_events(). The store now writes the correction / autonomy-change time explicitly into its rows (same text format as schema.sql's strftime('%Y-%m-%dT%H:%M:%fZ','now') defaults) and passes the same instant to the event; correction events now emit after commit.

Things worth checking specifically: does the explicit created_at/updated_at text sort and compare consistently with existing rows and with get_overrides' `since` formatting; does init_telemetry placement after parse_args leave any command path (e.g. serve's asyncio.run, gate) uninitialised or break flush-at-exit; are there callers of emit_correction_event / emit_state_transition_event (now with required keyword args) that were not updated.

Focus: correctness only.
Report format: bullet list of CRITICAL / IMPORTANT / MINOR with file:line. Under 350 words. "No correctness issues found" is a valid result.