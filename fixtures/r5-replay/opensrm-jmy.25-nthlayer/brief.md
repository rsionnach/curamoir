# opensrm-jmy.25: machine-readable JSON output for learn recommendations CLI

Add machine-readable JSON output to `nthlayer-workers learn recommendations --pr` so CI integrations and automation tooling can parse the PR-creation outcome without grepping stdout.

Discovered during jmy.6 brainstorming (2026-05-26). The v1.5 jmy.6 ships a single human-readable line `PR created: <url>` that operators copy-paste. Automation tooling parsing this is workable (grep `https://`) but fragile. File-as-needed bead when CI integrations request structured output.

Scope:
- Add `--json` flag to `nthlayer-workers learn recommendations`. When set, all human-readable output goes to stderr; stdout emits a single JSON document at end-of-run with structured outcome:
  ```json
  {
    "applied": [{"id": "rec-...", "type": "...", "service": "...", "field": "..."}],
    "skipped": [{"id": "rec-...", "reason": "drift_detected", "details": {...}}],
    "pr_url": "https://github.com/org/repo/pull/42",
    "pr_number": 42,
    "exit_code": 0
  }
  ```
- Flag is additive — when unset, current human-readable behaviour preserved
- Tests for both flag-set and flag-unset paths

Effort: 0.5 session including R5.
Priority: Adopt-when-needed. Not v1.5 critical.
Discovery: jmy.6 brainstorming (2026-05-26).
Dependency: depends-on jmy.6 (the human-readable structures + exit codes land there first).
