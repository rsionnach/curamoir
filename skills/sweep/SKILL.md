---
name: sweep
description: The GC sweep's janitor pass — run curamoir-lint and work only from its list of violations
disable-model-invocation: true
allowed-tools: Bash(*/scripts/curamoir:*)
---

# The GC sweep (Epic 3)

You are the janitor, not the architect. You fix mechanical violations that the
lint has already found; you never redesign. The runner starts you on the cheap
model tier (`models.cheap` in `.curamoir/curamoir.yaml`); the model is chosen
there, not here, so a repository can change it without editing this skill.

Model invocation is disabled on purpose: a sweep edits code and, once it is
complete, opens a PR, so it runs only when the scheduler or the developer asks
for it, never because a conversation looked like it wanted one.

## 1. Get the work list (E3-R1)

Run the lint through cúramóir's helper, which runs `scripts/curamoir-lint`
exactly as CI does and refuses a report it cannot trust:

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir sweep-work-list --repo "${1:-.}"
```

- **Exit 2:** the lint's result cannot be trusted — a rule or the config did
  not load, a rule was skipped, ast-grep is missing or failed, the run hung,
  the work list could not be written. Stop. Edit nothing. Report the stderr
  as it is: it is the reason the job records. Do not run the lint yourself,
  and do not look for violations by reading the code instead; a work list you
  assembled is not the work list. The helper prints nothing on stdout for
  this; if something is there anyway, it is not the work list.
- **Any other non-zero exit, or stdout that is not the JSON described below:**
  treat it exactly as exit 2 — stop, edit nothing, and report what was
  printed (stdout and stderr) as it is. The helper can still exit 1 for a
  fault it does not anticipate (a crash, or a filesystem error it turns into
  one sentence on stdout), and that is no more a work list than exit 2 is.
- **`"outcome": "no_work"`:** the lint found nothing. Stop and report
  `no_work`, with the kinds of note in `notes`, if any (`no_rules` means
  there was nothing to check, which is worth saying). Do not go looking for
  something to fix.
- **`"outcome": "work"`:** `principles` holds every violation, grouped by
  principle id and ordered within each by path and line. Each violation has
  its exact `path`, `line`/`col` and `end_line`/`end_col` (1-based, end
  exclusive), the `message`, the `fix`, and its `fingerprint`. Each principle
  carries its one-sentence `principle_rule` and a `why_link` to its reasoning
  in `.curamoir/principles.md`. When `fix_source` is `"principle"`, the fix is
  the principle's whole corrected example, not text to paste.

The work list is everything the lint reported, at every severity. Nothing on
it is optional because of its severity, and nothing off it is yours to fix.

## 2. The limits on a fix (E3-R2)

E3-R2, verbatim: "Fixes MUST be scoped: only files with violations; no
refactors beyond the violation; no new dependencies; no signature changes
without an explicit principle permitting it."

After you finish, cúramóir checks every change against these limits in code
and opens no PR from a fix that breaks any of them:

- Edit only the files in the work list's `paths`, and touch no other file.
  Do not create, rename or delete a file. A violation that is the file
  itself (reported at line 1, column 1 to line 1, column 1 with an empty
  `match`, which is how a rule about a file's name is reported) is not
  yours to fix: where its rule says `remedy: delete_file`, cúramóir handles
  it before you start and leaves it out of your work list; any other
  such violation, leave as it is and say so.
- Change only a violation's own lines and at most 3 lines either side
  of it. The one exception is adding, at the top of the module before its
  first function or class, an import, a logger (`logger =
  logging.getLogger(__name__)`) or a new constant set to a plain literal;
  any other code there is held to the rule above.
- Do not change a dependency manifest or lockfile (pyproject.toml,
  requirements files, package.json, uv.lock and the like), and do not import
  a package the repository does not already use.
- Do not add, remove or change the signature of any function, method or
  class (its parameters, defaults, annotations, return type, decorators or
  bases) unless the principle of a violation in that function or class
  says `permits: signature_change`.
- If a violation cannot be fixed within these limits, leave it as it is and
  say so. An unfixed violation is reported; an out-of-scope fix is discarded.

The files with violations are the work list's `paths`, and a principle's
`permits` field is in `.curamoir/principles.md`.

These limits are checked in code, not taken on trust: `src/curamoir/fix_scope.py`
compares the tree after a fix with the commit the lint ran on, and the
scheduled sweep (`curamoir run sweep`) runs the fix step headless in a
disposable clone of that commit, never in your checkout, with these same
limits as its prompt (`src/curamoir/fix_step.py`), then refuses any change
that breaks them. The principles it reads `permits` from are the ones
committed at that commit. This text and that prompt are one text; a test
fails if they differ.

Until `curamoir run sweep` lands (cm-fkd.4), this skill stops after step 1:
report the work list (its count, its principle ids, and each violation's
location) and change nothing. A fix made here would reach no review step.

## 3. Still to come

Each is its own requirement, and none is done by this skill yet:

- Tier-1 findings for principles with no rule (E3-R1b, cm-fkd.2), each tagged
  with its principle id. `no_work` means no lint work AND no tier-1 findings.
- The project's test command before any PR, and no red PR (E3-R4, cm-fkd.5).
- Exactly one PR, capped at `sweep.max_files_per_pr`, on `curamoir/sweep-<date>`,
  listing every fix with its principle id (E3-R3, cm-fkd.4).
- Per-principle fix counts to the ledger (E3-R5, cm-fkd.6).
- The open-PR cap and the budget (E3-R6, cm-fkd.7), and no second PR from an
  unchanged tree (E3-R7, cm-fkd.8).
