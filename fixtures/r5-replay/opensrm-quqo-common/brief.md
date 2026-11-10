PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-quqo (checkpoint 1: the nthlayer-common slice).

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it. SINGLE FOCUS: correctness.

Scope is THE DIFF:
  git -C <workspace>/nthlayer-common-quqo-log-events diff 1c65336..c471901
Files: src/nthlayer_common/telemetry.py (rewritten), tests/test_telemetry.py, pyproject.toml (opentelemetry-api>=1.38,<2; new `otel` extra with sdk + otlp grpc exporter; sdk in dev), uv.lock, docs/architecture.md.

Bead: workers and common never emitted OTel telemetry. Events were span events, and nothing ever made a span recording.

Rulings (Rob, 2026-11-02):
- every NthLayer event becomes an OTel LOG RECORD (OpenSRM Addendum A1 §4.0, opensrm/OPENSRM-TELEMETRY-ADDENDUM-v2.md:76);
- carriage: event_name = name, no body, attributes only, severity INFO, Timestamp = the event's own time (A1 T1 at :125), never ObservedTimestamp.

The consumers come in later checkpoints:
- workers' measure emitters move to emit_event, and the workers CLIs call init_telemetry;
- the override adapter moves its `nthlayer.reversal` span to emit_event.

Existing callers of emit_llm_event: ../nthlayer-workers/src/nthlayer_workers/respond/agents/base.py and correlate/summary.py.

Verify:
- emit_event against the OTel Python logs API, across the 1.38 floor and the locked 1.44. Read the installed SDK source in this worktree's .venv. Check the Logger.emit signature, the severity and the event_name handling.
- the `_epoch_ns` arithmetic, including negative or pre-epoch values, and timezone handling.
- init_telemetry:
  - endpoint precedence;
  - whether OTLPLogExporter(endpoint=...) with an http:// scheme works over gRPC, and what it does with insecure/TLS;
  - Resource.create merging defaults;
  - set_logger_provider being set-once;
  - the atexit shutdown;
  - idempotency through the module-level _provider;
  - the exporter override.
- whether swallowing every exception in emit_event can hide a programming error that tests should catch.
- that emit_llm_event's attributes are unchanged from before. Check `nthlayer.llm.error` and the `error or None` handling.
- that the api floor and `<2` ceiling, the extras, and the uv.lock all agree.
- what happens to a consumer pinned to opentelemetry-api 1.41: the workers, core and nthlayer venvs.

Report a bullet list of CRITICAL / IMPORTANT / MINOR. Keep it under 350 words. "No correctness issues found" is a valid result.