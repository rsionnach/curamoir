# `.curamoir/decisions.jsonl` — the decision log

One JSON object per line, append-only, committed with the bead that produced
it. Written by an agent working under the `decision-log` project skill
(`.claude/skills/decision-log/SKILL.md`, phase 1 trial); the first record
creates the file. Three kinds of line, told apart by `record`.
`tests/test_decision_log.py` checks the skill's append script and the
examples below against the tables in this file.

The skill activates only on an explicit request, so sessions outside the trial
are unaffected. This is the normative text: its description opens with exactly
this sentence.

> Use ONLY when the user or a kickoff brief says a bead is worked "under the decision log", or the user invokes /decision-log; never otherwise.

Lines are written as `src/curamoir/ledger.py` writes its own: one object per
line, keys sorted, UTF-8, each line ending in a newline; a last line left
without its newline is closed with one before the next is appended. Every
`ts` is UTC to the whole second, `YYYY-MM-DDTHH:MM:SSZ`, so two `ts` values
compare correctly as text. A line is never edited or deleted: a correction is
a new line.

A valid decision line is never corrected or repeated, so each id has one: a
wrong pick is reversed (a `reversal` line), and a slip in the record is noted
in its outcome's `note`. Outcome lines are corrected by appending another (see
`outcome`).

**Invalid lines.** A line that does not parse as one JSON object (a torn
write, say), or that breaks this schema (a required field missing, a value
outside its set, a `ts` in another form), is invalid. It is left out of every
count, and how many there were is reported with the counts. A decision whose
only line is invalid is appended again, complete, with the same id. Identical
valid decision lines for one id (the same record written twice) count as one.
If an id has valid decision lines that differ, all of them are left out and
reported.

Two bead branches that both append to the file conflict when the second is
merged, whichever way the merge runs. Resolve it by keeping every line from
both sides, in any order: ids are bead-scoped, so no line duplicates the other
side's, and no rule here reads the order of lines.

## `decision`

| Field | Required | Content |
|---|---|---|
| `record` | yes | `"decision"` |
| `id` | yes | `d-<bead>-<n>`, `n` from 1 within the bead. Bead-scoped so two branches cannot mint the same id |
| `ts` | yes | `YYYY-MM-DDTHH:MM:SSZ`: when the decision took effect |
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
| `ts` | yes | `YYYY-MM-DDTHH:MM:SSZ`: when the user gave this answer, not when the line was written. A correction of an earlier outcome carries the time the correction was given, so it is later than the line it corrects |
| `value` | yes | `correct`, `incorrect` or `indeterminate` |
| `source` | yes | `human_audit` in phase 1 |
| `note` | no | One line |
| `reclassified_to` | no | `A`, `B` or `C`, when the reviewer moves the decision to another class |

The outcome in effect for a decision is the one with the latest `ts`. If
several share the latest `ts` and differ in `value` or `reclassified_to`, none
is in effect: the decision counts as unresolved, and is listed with the counts,
until an outcome with a later `ts` is appended. Line order never decides.

## `reversal`

| Field | Required | Content |
|---|---|---|
| `record` | yes | `"reversal"` |
| `decision_id` | yes | The decision reversed |
| `ts` | yes | `YYYY-MM-DDTHH:MM:SSZ`: when the user reversed it |
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

Each count uses a decision's outcome in effect. A decision with no outcome in
effect (none yet, or a tie) is unresolved: it is left out of the reversal rate
and the high-confidence failure rate. An outcome or reversal that names no
decision in the file, and an invalid line, is left out of every count, and how
many were left out is reported with the counts. A measure whose denominator is
zero is reported as `n/a (0)`, not as 0.

| Measure | Definition |
|---|---|
| Reversal rate, class A or B | Decisions of that class with an outcome and a reversal ÷ decisions of that class with an outcome |
| High-confidence failure rate | Among autonomous decisions logged at confidence ≥ 0.8 whose outcome is `correct` or `incorrect`: the share that are `incorrect` (unresolved and `indeterminate` excluded) |
| Misclassification | Outcomes carrying `reclassified_to` |
| Rework | For each reversed decision, the number of decisions whose `depends_on` reaches it, directly or through other decisions; the mean over reversed decisions |
| Prompts avoided | Class A and B decisions per bead |
| Findings traced to a decision | From each bead's digest |

## Thresholds (phase 1)

Confirmed by the user on 2026-11-17 for the trial that starts on the first
trial bead. Logging stops, as the skill says, when 30 decisions are logged or
three trial beads have closed. The trial ends once the user has reviewed every
decision it logged, which is at the close of the bead in which logging
stopped, so no review is still to come. Each threshold is applied to
the counts above at that point, except misclassification, which applies the
moment it is found.

"Reviewed" means the decision has an outcome in effect. A decision counts
under the class of that outcome's `reclassified_to` when it has one, and
under the class it was logged with otherwise, for the minimums and the rates
alike.

| Measure | Threshold | Applies when |
|---|---|---|
| Reversal rate, class A | Below 10%: stop at phase 1. 10% or more: build phase 2 | 15 or more class A decisions reviewed |
| Reversal rate, class B | 25% or more: class B becomes stop-and-ask | 8 or more class B decisions reviewed |
| Misclassification | Any class C decision logged as A or B: stop using the skill and say so | Always, at once |
| Rework | A mean above 2 dependents per reversed decision: review moves to checkpoints inside the bead, not at its close | At trial end |
| High-confidence failure rate | Reported only | — |
| Prompts avoided | Reported only | — |

Every other measure (misclassification between A and B, findings traced to a
decision) is reported only.

If a class is under its minimum when the trial ends, the trial is extended by
one bead, once: that bead is worked under the decision log, and the trial then
ends in the same way at its close. Every
threshold is then applied at the new end, for both classes. A class still
under its minimum after that is reported with its counts and no threshold
applied.
