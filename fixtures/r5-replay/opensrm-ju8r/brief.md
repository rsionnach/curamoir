PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-ju8r. Read-only: do not modify any files.

Template: read and follow ~/.claude/skills/rule-of-five-review/correctness-reviewer.md.

Scope is THE DIFF: git -C <workspace>/nthlayer-workers-ju8r diff aedcf23..7c32022
Do not raise findings outside it. Pre-change: `git -C <path> show aedcf23:<file>`.

Bead: opensrm-ju8r — workers emitted gen_ai.decision.evaluated, gen_ai.calibration.report, gen_ai.override.applied and gen_ai.agent.state.changed; OpenSRM Addendum A1 §4.0 bars gen_ai. forms of OpenSRM-defined events. Plus the provider="unknown" sentinel reaching gen_ai.provider.name.

Rulings by Rob (do not relitigate): J1 decision.evaluated → one upstream gen_ai.evaluation.result per dimension (A1 §4.4): evaluation.name=dimension, score.value=score, nthlayer.evaluator.kind=implementation, nthlayer.decision.id=evaluated task; other fields as extensions. J2 override.applied → gen_ai.evaluation.result with nthlayer.evaluator.kind=human, name=dimension, score.value=corrected; original score extension. J3 calibration.report → nthlayer.calibration.report, agent.state.changed → nthlayer.autonomy.changed (attrs unchanged). J4 provider derived in workers (explicit provider/ prefix; claude→anthropic; gpt/o1/o3→openai; else "unknown"). Implementer refinements to check (not rulings — judge them): per-evaluation facts (cost, alert_count, dimension_count) moved to one nthlayer.evaluation.completed summary event to avoid N× repetition; auto-approved outputs emit only the summary (no grader events).

Spec: <workspace>/opensrm/OPENSRM-TELEMETRY-ADDENDUM-v2.md §4.4 (gen_ai.evaluation.result: which attributes are Required upstream / Conditionally required; nthlayer.decision.id; nthlayer.evaluator.kind values; score.value MUST NOT carry the decision's own confidence), §3 (grader admissibility), §4.0.

Check: attribute names and types exactly per A1 §4.4; is gen_ai.evaluation.score.label needed (upstream "Cond. required")?; is task_id actually the decision id of the evaluated output (read measure's types and how task_id is produced)?; nthlayer.evaluator.confidence vs A1's rule about score.value; every caller of the changed functions (emit_override_event signature gained decision_id kwarg; store passes exists["task_id"] — what if the row's task_id is NULL?); anything in workers (or nthlayer-core / bench / generate / nthlayer front door demo, <workspace>) that reads the old event names; provider_for_model edge cases (model None? callers' types). Run: cd <path> && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -o addopts="" -q

Focus: correctness only. Bullet list CRITICAL / IMPORTANT / MINOR with file:line, under 400 words, verdict. "No correctness issues found" is valid.