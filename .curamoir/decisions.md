# `.curamoir/decisions.jsonl` — the decision log

One JSON object per line, append-only, committed with the bead that produced
it. Written by an agent working under the `decision-log` project skill
(`.claude/skills/decision-log/SKILL.md`, phase 1 trial); the first record
creates the file. Three kinds of line, told apart by `record`.

Lines are written as `src/curamoir/ledger.py` writes its own: one object per
line, keys sorted, UTF-8, each line ending in a newline; a last line left
without its newline is closed with one before the next is appended. A line is
never edited or deleted: a correction is a new line.

## `decision`

| Field | Required | Content |
|---|---|---|
| `record` | yes | `"decision"` |
| `id` | yes | `d-<bead>-<n>`, `n` from 1 within the bead. Bead-scoped so two branches cannot mint the same id |
| `ts` | yes | ISO 8601, UTC: when the decision took effect |
| `bead` | yes | The bead in progress |
| `sha` | yes | `HEAD` when the decision was taken |
| `question` | yes | The question as it would have been asked |
| `options` | yes | Every option considered: `[{"label", "summary"}]`, two or more |
| `value` | yes | Label of the option taken. Never `escalate` (see Vocabulary) |
| `rationale` | yes | Why it was recommended, two sentences at most |
| `confidence` | yes | 0.0 to 1.0: probability the pick will be upheld, stated before acting. For class C, the confidence in the option recommended to the user |
| `class` | yes | `A` technical and reversible, `B` intent, `C` irreversible |
| `reversible` | yes | `false` for class C, else `true` |
| `autonomous` | yes | `true` when the agent decided, `false` when the user answered |
| `undo` | yes | What reversing it would take, one line |
| `falsifier` | yes | The observation that would show this was the wrong pick |
| `depends_on` | yes | Ids of earlier decisions this one builds on; may be empty |

## `outcome`

| Field | Required | Content |
|---|---|---|
| `record` | yes | `"outcome"` |
| `decision_id` | yes | The decision reviewed |
| `ts` | yes | When the outcome was determined (the review), not when the line was written |
| `value` | yes | `correct`, `incorrect` or `indeterminate` |
| `source` | yes | `human_audit` in phase 1 |
| `note` | no | One line |
| `reclassified_to` | no | `A`, `B` or `C`, when the reviewer moves the decision to another class |

The outcome in effect for a decision is the one with the latest `ts`; on equal
`ts`, the later line.

## `reversal`

| Field | Required | Content |
|---|---|---|
| `record` | yes | `"reversal"` |
| `decision_id` | yes | The decision reversed |
| `ts` | yes | When |
| `type` | yes | `human_override` in phase 1 |
| `actor_type` | yes | `human` |
| `new_value` | no | Label of the option now in force |
| `reason` | no | One line |

There is no field naming who reversed it: the file is public, and phase 1 has
one reviewer. A decision counts as reversed once, however many reversals name
it.

## Examples

One of each kind, every required field present:

```json
{"autonomous": true, "bead": "cm-abc", "class": "A", "confidence": 0.8, "depends_on": [], "falsifier": "A test or reviewer shows a caller that depends on the exception escaping", "id": "d-cm-abc-1", "options": [{"label": "a", "summary": "Catch the error at the call site and log it"}, {"label": "b", "summary": "Let the error propagate to the runner"}], "question": "Should a malformed config line be logged and skipped, or fail the job?", "rationale": "Every other reader in the module skips and counts bad lines. Failing the job would stop every later run over one typo.", "record": "decision", "reversible": true, "sha": "0123456789abcdef0123456789abcdef01234567", "ts": "2026-11-16T10:02:11Z", "undo": "Remove the except clause and its test", "value": "a"}
```

```json
{"decision_id": "d-cm-abc-1", "note": "Matches the module's convention", "record": "outcome", "source": "human_audit", "ts": "2026-11-17T09:30:00Z", "value": "correct"}
```

```json
{"actor_type": "human", "decision_id": "d-cm-abc-1", "new_value": "b", "reason": "The runner already reports this error to the user", "record": "reversal", "ts": "2026-11-17T09:31:00Z", "type": "human_override"}
```

(The outcome and the reversal above name the same decision only to keep the
example short. A real reversal goes with an outcome of `incorrect`.)

## Vocabulary

Field names follow the OpenSRM decision-telemetry terms where one exists:
`value`, `confidence` (ratio 0 to 1), `autonomous`, `reversible`, and the
separate `outcome` and `reversal` records with their field names and value
sets (`outcome.value`, `outcome.source`, `reversal.type`,
`reversal.actor_type`, `reversal.new_value`). Those terms reserve the decision
value `escalate` for a decision routed to a human; here that is a class C
record with `"autonomous": false`, so no option is labelled `escalate`.
`class`, `options`, `falsifier`, `undo` and `depends_on` are this log's own.
The log is a file, not telemetry: nothing here is emitted, and cúramóir works
with no NthLayer component installed.

## Measures (hand counts in phase 1)

Each count uses a decision's outcome in effect. An outcome or reversal that
names no decision in the file is left out of every count, and how many were
left out is reported with the counts.

| Measure | Definition |
|---|---|
| Reversal rate, class A or B | Decisions of that class with a reversal ÷ decisions of that class with an outcome |
| High-confidence failure rate | Among autonomous decisions logged at confidence ≥ 0.8 whose outcome is `correct` or `incorrect`: the share that are `incorrect` (unresolved and `indeterminate` excluded) |
| Misclassification | Outcomes carrying `reclassified_to` |
| Rework | Mean number of decisions whose `depends_on` reaches a reversed decision |
| Prompts avoided | Class A and B decisions per bead |
| Findings traced to a decision | From each bead's digest |
