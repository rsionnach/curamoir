PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-6vp9, nthlayer (front door).

SINGLE FOCUS: correctness — only what is wrong, broken or produces incorrect results. Check API contracts against the real callee.

Scope is THE DIFF: git -C <workspace>/nthlayer-smoke-telemetry diff ec159c3..56c998b (one file: test/test_jmy18_smoke.py).
Callee: <workspace>/nthlayer-override-adapter/src/nthlayer_override_adapter/app.py (build_app(config, *, telemetry: TelemetryStatus), telemetry_exporting — note it also returns False when OTEL_SDK_DISABLED=true), released as 0.4.0. TelemetryStatus: <workspace>/nthlayer-common/src/nthlayer_common/telemetry.py.

Bead: adapter 0.4.0 made build_app's telemetry status required (ruling Q7: it decides each binding's otel field, "ok" iff exporting). The three standalone smoke tests called build_app(cfg) and raised TypeError; they now pass telemetry=TelemetryStatus.INSTALLED and keep asserting otel == "ok". The file is a standalone smoke (excluded from CI and ruff floor; see docs/integration-testing.md:73 and pyproject.toml extend-exclude). It passes locally: run it with `cd <workspace> && PYTHONDONTWRITEBYTECODE=1 uv run --with pytest --with pytest-asyncio python -m pytest -q -p no:cacheprovider -o asyncio_mode=auto nthlayer-smoke-telemetry/test/test_jmy18_smoke.py`.

Consider: does the test now depend on the environment (e.g. OTEL_SDK_DISABLED set in a shell makes otel "failed")? Do the other two tests assert anything about otel? Any other call sites of the adapter API in the front door (grep the whole nthlayer-smoke-telemetry tree: demo/, test/, docs examples) that also break against 0.4.0 — e.g. emit_reason_on_span in a demo config, build_app, register_*_routes, override_collector_errors_total?

Report: CRITICAL / IMPORTANT / MINOR with file:line, under 300 words. "No correctness issues found" is valid. Do not modify files.