---
name: decision-log
description: >-
  Use ONLY when the user or a kickoff brief says a bead is worked "under the
  decision log", or the user invokes /decision-log; never otherwise. Phase 1
  trial: where you would stop to ask the user to pick between options, classify
  the decision; for a reversible technical or intent decision take your
  recommended option, append a record to .curamoir/decisions.jsonl before
  acting, and carry on; an irreversible decision still stops and asks.
---

# /decision-log — decide, record, carry on (phase 1 trial)

A blocking question costs hours when its recommended answer would have been
accepted anyway. Under this skill you take the recommended option yourself,
**but only where the choice stays reviewable and reversible**, and you write
down what you chose, what you rejected and what would prove you wrong.

This is a trial. It runs on the beads the user names, until 30 decisions are
logged or three beads close. Its one question: how often is the recommended
option wrong?

## When this applies

A **decision** is a point where you would otherwise end your turn to ask the
user to pick between options. It applies while you implement a bead, before
the gate.

It does **not** apply, and you stop and ask exactly as before, for:

- **Anything inside `/r5-supervise`.** The gate's rules are unchanged: a
  finding that needs a design decision is `design_question_raised` and asked.
- **Rule-mandated stops**: skipping a pass, deleting or renewing the R5 lock,
  a red baseline, a NEEDS WORK or CRITICAL verdict, leftover mutants, three
  fix iterations without a clean pass, resuming someone else's in-progress
  bead, starting a bead the user has not released.
- **Tool permission prompts.** This skill never answers them.
- A question with no options (you lack a fact only the user has). Ask it.

## Classify, then route

| Class | Test | Route |
|---|---|---|
| **A** technical, reversible | Has a checkable answer; undoing it is a local diff | Take the recommended option, log, continue |
| **B** intent | Depends on what the user wants; code cannot show it is wrong (scope cuts, command and flag names, which trade-off wins) | Take the recommended option, log, continue, **and list it in the digest's human queue** |
| **C** irreversible | On the fixed list below | **Stop and ask**, then log the answer with `"autonomous": false` |

**Class C, the fixed list.** Not a judgement call:

1. An on-disk format or schema change: anything under `.curamoir/` that
   cúramóir reads or writes (`curamoir.yaml` keys, `principles.md`, rule
   files, the handoff files, the ledger and the other `state/` files, this
   log's own schema), the beads data, and the R5 gate's state file, report
   files and findings contract.
2. A change to a public spec, the plugin manifest (`.claude-plugin/`), a
   command (`commands/`), a hook or its contract (`hooks/`, and the commands
   and exit codes of the helpers in `.claude/hooks/`), a shipped skill
   (`skills/`), a CLI command, flag or exit code, or any public API.
3. A new dependency, or a version floor or ceiling change (`pyproject.toml`,
   `requires-python`, the lock file).
4. Deleting files or data, or rewriting history.
5. Anything that leaves the repository: a push to `main` or a merge into it, a
   tag, a release, a publish, an issue, comment or tracker note on another
   repository, a message to a person.
6. A security boundary: what the OS sandbox, the git guard and its hardening,
   the guards and policy hooks, the redaction, the fix step's command line or
   a token may reach.

Pushing the bead's own `bead/<id>` branch and opening its PR is the normal
close flow and is **not** a decision.

**When unsure between two classes, take the later letter.**

## The record

Append one JSON object per line to `.curamoir/decisions.jsonl` **before you
act on the decision**, and commit the file with the bead. Schema, field
meanings and a full example of each kind of line: `.curamoir/decisions.md`.

```json
{"autonomous": true, "bead": "cm-abc", "class": "A", "confidence": 0.8, "depends_on": ["d-cm-abc-1"], "falsifier": "<the observation that would show this was the wrong pick>", "id": "d-cm-abc-3", "options": [{"label": "a", "summary": "<one line>"}, {"label": "b", "summary": "<one line>"}], "question": "<as you would have asked it>", "rationale": "<two sentences at most>", "record": "decision", "reversible": true, "sha": "<HEAD when taken>", "ts": "2026-11-16T10:02:11Z", "undo": "<one line>", "value": "a"}
```

- `id` is `d-<bead>-<n>`: `n` is one more than the highest `n` already logged
  for that bead on this branch, starting at 1 (`d-cm-rp1.2-1`).
- `options` lists **every** option you considered, including the ones you
  rejected. `falsifier` is required. A record without both is invalid: they
  are what make later review possible.
- `confidence` is your probability, 0 to 1, that the pick will be upheld,
  **stated before acting**. It cannot be added later. For class C it is your
  confidence in the option you recommended to the user; `value` is the option
  the user chose.
- `value` is an option's label and never `escalate`: in the telemetry terms
  this log borrows, that word means "routed to a human", which is what class C
  already records with `"autonomous": false`.
- `depends_on` names earlier decisions this one builds on.
- **This repository is public.** Write the question, options and rationale in
  your own words. Do not quote or cite private specs, private handoffs or
  another project's internals.
- `ts` is UTC to the whole second, `YYYY-MM-DDTHH:MM:SSZ`, on every line.
- Append only. Never edit or delete a line. A valid decision line is never
  corrected or written twice: a wrong pick is reversed, and a slip in the
  record goes in its outcome's `note`. A decision whose line is invalid (it
  does not parse, or breaks the schema) is appended again, complete, with the
  same id.
- If merging the bead's branch conflicts in `decisions.jsonl` (another
  branch appended too), keep every line from both sides, in any order.

**How to append**, as `src/curamoir/ledger.py` does: one object per line,
keys sorted, UTF-8, every line ending in a newline, and a last line left
without its newline (a crash mid-write) closed with one before the new line
is added. No tool does this for you in phase 1; this one-off script checks the
record against the schema first, and refuses a decision whose id already has
a valid line, so running it twice writes one line. It writes nothing when it
refuses, and says why. Fill in `record` with every field of its kind; it runs
from anywhere in the checkout:

```bash
python3 - <<'EOF'
import datetime, json, pathlib, re, subprocess, sys

top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
path = pathlib.Path(top, ".curamoir", "decisions.jsonl")
record = {"record": "decision", "id": "d-cm-abc-3"}  # every field of its kind

REQUIRED = {
    "decision": "autonomous bead class confidence depends_on falsifier id options question rationale reversible sha ts undo value",
    "outcome": "decision_id source ts value",
    "reversal": "actor_type decision_id ts type",
}

def text(value):
    return isinstance(value, str) and value.strip() != ""

def problems(r):
    """Why r breaks the schema in .curamoir/decisions.md; empty when valid."""
    if not isinstance(r, dict) or r.get("record") not in REQUIRED:
        return ["record: decision, outcome or reversal"]
    missing = [name for name in REQUIRED[r["record"]].split() if name not in r]
    if missing:
        return [f"missing: {missing}"]
    found = []
    def check(ok, message):
        if not ok:
            found.append(message)
    ts = r["ts"]
    try:
        real = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") is not None
    except (TypeError, ValueError):
        real = False
    check(real and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ts), "ts: YYYY-MM-DDTHH:MM:SSZ, a real time")
    if r["record"] == "decision":
        for name in ("bead", "id", "sha", "question", "rationale", "undo", "falsifier"):
            check(text(r[name]), f"{name}: non-empty text")
        options = r["options"]
        good = isinstance(options, list) and all(
            isinstance(o, dict) and text(o.get("label")) and text(o.get("summary")) for o in options)
        labels = [o["label"] for o in options] if good else []
        check(good and len(labels) >= 2 and len(set(labels)) == len(labels),
              "options: two or more, each a distinct label and a summary")
        check(r["value"] in labels and r["value"] != "escalate", "value: an option's label, never escalate")
        check(type(r["confidence"]) in (int, float) and 0 <= r["confidence"] <= 1, "confidence: a number, 0 to 1")
        check(r["class"] in ("A", "B", "C"), "class: A, B or C")
        check(r["reversible"] is (r["class"] != "C"), "reversible: false for class C only")
        check(r["autonomous"] is (r["class"] != "C"), "autonomous: false for class C only")
        check(text(r["bead"]) and text(r["id"])
              and re.fullmatch(rf"d-{re.escape(r['bead'])}-[1-9][0-9]*", r["id"]), "id: d-<bead>-<n>")
        check(isinstance(r["depends_on"], list) and all(text(d) for d in r["depends_on"]),
              "depends_on: a list of ids")
    elif r["record"] == "outcome":
        check(text(r["decision_id"]), "decision_id: non-empty text")
        check(r["value"] in ("correct", "incorrect", "indeterminate"), "value: correct, incorrect or indeterminate")
        check(r["source"] == "human_audit", "source: human_audit")
        check(r.get("reclassified_to", "A") in ("A", "B", "C"), "reclassified_to: A, B or C")
        check(isinstance(r.get("note", ""), str), "note: text")
    else:
        check(text(r["decision_id"]), "decision_id: non-empty text")
        check(r["type"] == "human_override" and r["actor_type"] == "human", "type, actor_type")
        check(text(r.get("new_value", "x")) and isinstance(r.get("reason", ""), str), "new_value, reason: text")
    return found

errors = problems(record)
data = path.read_bytes() if path.exists() else b""
if not errors and record["record"] == "decision":
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            old = json.loads(line)
        except ValueError:
            continue
        if isinstance(old, dict) and old.get("record") == "decision" and old.get("id") == record["id"] and not problems(old):
            errors.append(f"{record['id']} already has a valid decision line")
            break
if errors:
    sys.exit("not written: " + "; ".join(errors))
with path.open("a", encoding="utf-8") as handle:
    if data and not data.endswith(b"\n"):
        handle.write("\n")
    handle.write(json.dumps(record, sort_keys=True) + "\n")
EOF
```

## At bead close: the digest

Before opening the PR, write the digest into the PR body under
`## Decisions`, and put the counts in the `bd close` reason:

1. Counts per class, and how many were autonomous.
2. **Human queue:** every class B decision: id, question, what you chose, the
   options you rejected, and the ids of decisions that depend on it.
3. Every class A decision, one line each (phase 1: the user reviews these too).
4. **Findings traced to a decision:** for each R5 finding in this bead's gate,
   say whether it follows from a logged decision (which id), and whether a
   rejected option would have avoided it. "None" is a valid answer.
5. Prompts avoided: the number of A and B decisions.

## Review

The user marks each decision. Record their answers as new lines, appended the
same way:

```json
{"decision_id": "d-cm-abc-3", "note": "<one line>", "record": "outcome", "source": "human_audit", "ts": "2026-11-17T09:30:00Z", "value": "correct"}
{"actor_type": "human", "decision_id": "d-cm-abc-4", "new_value": "b", "reason": "<one line>", "record": "reversal", "ts": "2026-11-17T09:31:00Z", "type": "human_override"}
```

`value` is `correct`, `incorrect` or `indeterminate`. `ts` is when the user
gave the answer, not when you wrote the line; correcting an outcome you
recorded wrongly is a new outcome line whose `ts` is when the user gave the
correction. A reversed decision gets both
lines: the reversal, and an outcome of `incorrect`. If the user moves a
decision to another class, add `"reclassified_to":"B"` to its outcome line.

**If the user finds a class C decision that was logged as A or B, stop using
this skill and say so**: the fixed list needs fixing before the trial goes on.

Reversing a decision is the user's call and is ordinary work: a new change,
through the gate. Never reverse one automatically.
