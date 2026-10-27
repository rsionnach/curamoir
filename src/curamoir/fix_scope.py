"""E3-R2's second half: the PR review step verifies a sweep fix's scope.

E3-R2: "Fixes MUST be scoped: only files with violations; no refactors beyond
the violation; no new dependencies; no signature changes without an explicit
principle permitting it. The skill prompt MUST state these limits and the PR
review step MUST verify them."

The prompt states them (src/curamoir/fix_step.py, SCOPE_LIMITS). This module
is the verification, and it is written so that it does not need the prompt to
have been obeyed, read, or even sent: it compares the tree the fix agent left
behind with the commit the lint ran on, and the work list that commit
produced, and nothing else. E0-R1's "enforced in code, not prompt" applied to
the sweep — a cheap-tier model that ignores every word of its instructions
produces a verdict that says so, with file and line, and the runner opens no
PR from it.

Two layers, so the rules can be tested without git:

- `check_scope(changes, violations, ...)`: the pure core. It takes each
  changed file's before and after text and the violations, and returns a
  `ScopeVerdict`: every breach, each with a kind (the KINDS below), a path,
  a line where there is one, and a sentence saying what was seen.
- `verify_fix(sandbox, git_state=...)`: the git adapter. The fix step runs
  in a sandbox, a disposable clone of the base commit (git_guard), never in
  the user's checkout. verify_fix first holds the sandbox's git files and
  work tree to the snapshot taken before the fix step (git_guard), then
  reads what changed between the base commit and the work tree (tracked
  changes with `git diff --raw`, hunks with `git diff -U0`, untracked files
  with `git ls-files --others`, nothing excluded), the sandbox's work list
  and rules (lint_sandbox), the principles at the base commit, and what the
  repository already imports there, and calls the core.

**What the runner does** (curamoir.sweep_job, cm-fkd.4: this list is its
contract, in its order; sweep_job's "What each outcome writes" says
which line each path records).

0. Wrap the whole pipeline, steps 1 to 10, in the guards (E3-R6), in four
   parts:
   (i) The wrap (done, cm-fkd.14): `runner.JOBS["sweep"]` is
       `Job(body=sweep_job.run_sweep, opens_prs=True,
       open_pr_listing_for=...)`, `body(repo, run)` being everything
       below, which records its outcome on `run`, the `JobRun` writing to
       the repo's own ledger. The body never calls
       `job_guard.run_guarded` itself: `curamoir run sweep` reaches it
       through `runner.run_job`, which runs every job behind the guards in
       E10-R1's order and gives the gateway the gh defaults of (ii) and
       (iii). Use `run`, not a ledger of your own, for every line below,
       and read history as `run.ledger.entries()`, the same ledger. A
       refusal (`paused`, `locked`, `budget`, `pr_cap`, `guard_error`)
       then writes the job's one `job` line, beside the gateway's `guard`
       line, before the body starts: before any clone, fix step or spend.
       `DefaultBranchGuard` is not among those guards: apply it at push/PR
       time, on the `<guardrails.branch_prefix>sweep-...` branch
       (job_guard's docstring), which publish.publish does (step 9).
   (ii) `repo_labels` (done, cm-fkd.10: curamoir.github.repo_labels, which
       `runner.run_job` binds to the root and to origin's repository for
       EVERY job, the kill switch being per run; gh's own resolution when
       origin names no GitHub repository, runner's "Which repository gh is
       asked about", cm-fkd.14): the `gh`-backed callable returning the
       repository's labels, for the `curamoir:paused` kill switch
       (guards.kill_switch), as an iterable of label-name strings (not
       gh's `{name: ...}` objects; any other shape degrades the check).
       Run exactly
       `gh label list --limit <high> --json name --jq '.[].name'`, with
       <high> well above any repository's label count (github.LABEL_LIMIT,
       1000), and return its
       output's lines. WHY: gh lists 30 labels by default, so without a
       high `--limit` `curamoir:paused` can be cut off and the job runs
       although paused, with nothing recorded: the check fails open. And
       gh's text output is `name<TAB>description<TAB>color`, a line that
       never equals the label, which fails open too. Like (iii)'s, it must
       time out and raise: a raise degrades the check (a degraded `guard`
       line, and the job still runs), but a hang the scheduler then kills
       writes no line at all.
   (iii) `open_sweep_prs` (done, cm-fkd.10: curamoir.github.open_sweep_prs,
       registered as `runner.JOBS["sweep"].open_pr_listing_for` and bound
       to the root by `runner.run_job`): the same function as step 2b's
       (bound differently, below),
       and it must time out and raise, because a hang the scheduler then
       kills writes no line at all. It is called twice per sweep, once by
       the cap here and once by step 2b's decision, so it must not cache
       across the two: the second call must see what was opened or closed
       in between. github.open_sweep_prs caches nothing. The body gets no
       parameter for it and builds its own: `open_sweep_prs =
       functools.partial(github.open_sweep_prs, repo, gh_repo=gh_repo)`,
       with origin's `gh_repo` from step 1, so the decision lists the PRs
       of the repository the PR opens on (step 9). The cap's listing,
       runner._gh_open_prs, passes origin's `gh_repo` too, resolved when
       the cap calls it (cm-fkd.14), so the cap counts the same
       repository's PRs. Never
       the bare function (it needs the repo) and never its result (a
       list, not a callable): sweep_decision calls it with no arguments,
       and either mistake raises inside its broad `except`, so every
       sweep would be refused as UNKNOWN with nothing else said.
       `run_job(open_pr_listing=...)` replaces only the cap's listing,
       so a test that must feed both the same PRs points `github.GH` at
       a fake gh (tests/fake_gh.py) instead.
   (iv) The result: `runner.run_job` maps the job_guard.GuardedRun's
       `outcome` to the process's exit code through `runner._EXIT_CODES`
       (EXIT_SUCCESS, EXIT_FAILURE, EXIT_REFUSED, EXIT_UNUSABLE_ROOT), and
       a crash to EXIT_FAILURE with one sentence; the body adds nothing
       here. Lint's EXIT_CLEAN, EXIT_VIOLATIONS and EXIT_ERROR
       (docs/curamoir-lint.md) are the lint gate's, not a job's. What
       each `outcome` (job_run's constants) means for the sweep:
       - job_run.SUCCESS: the body recorded a success (a sweep, or a
         terminal NO_WORK or DUPLICATE_OPEN_PR decision); `value` is what
         `body` returned.
       - job_run.REFUSED: a guard or the body refused; `reason` is the
         refusal's Reason (`pr_cap`, `budget`, `idempotence`, ...).
       - job_run.FAILURE: the body recorded a failure.
       - job_run.UNKNOWN: the body returned without recording an
         outcome, which is a bug in the runner, not a success.
       - job_guard.UNUSABLE_ROOT: the repo root could not be
         inspected, so the job did not run and no line was written (the
         reason is on stderr). A ledger write that fails is NOT this: the
         outcome is still the one the body recorded, and JobRun reports
         the failed write on stderr.
       A body that raises does not return a GuardedRun at all: JobRun
       writes a `failure` line and the exception propagates out of
       run_guarded, and `runner.run_job` reports it as a crash
       (EXIT_FAILURE).
1. Make a sandbox: `git_guard.prepare_sandbox(repo, base_ref)`, and use it
   as a context manager so it is removed whatever happens. The base is
   `refs/remotes/origin/<default>` as last fetched, `<default>` being
   GitHub's answer as publish reads it, resolved once with origin's
   `gh_repo` and pinned to its commit (curamoir.sweep_job.resolve_sweep_base,
   "Which default branch"). If that branch or its ref cannot be
   resolved, the sweep is refused (Reason.SWEEP_BASE) before any sandbox
   is made.
2. Lint it before anything edits it: `lint_sandbox(sandbox)`, which records
   the work list of the base, and the rules the lint ran, on the sandbox.
   Then compute what was found (E4-R4, fix_counts' "Introduced vs found"):
   `current = fix_counts.found_set(sandbox.work_list, sandbox.base)` and
   `delta = fix_counts.found_delta(fix_counts.snapshot_for(repo,
   sandbox.base, fix_counts.read_snapshot(fix_counts.snapshot_path(repo))),
   current)`. Read the snapshot from the user's checkout
   (`snapshot_path(repo)`), not the sandbox; the found set itself comes
   from the sandbox's work list. snapshot_for drops a snapshot whose base
   is not an ancestor of this one (a rewritten history), so the line is a
   baseline. Every line recorded from here on except a refusal carries
   `found_delta=delta`. After it, the snapshot write (step 2a).
2a. After any non-refusal line is recorded (success, failure, no_work,
   duplicate_open_pr):
   `fix_counts.write_snapshot_or_warn(fix_counts.snapshot_path(repo),
   current, sandbox=sandbox)`. It warns and keeps the old snapshot if the
   disk refuses: the line is already recorded, and the next line's
   `previous_digest` shows the gap. A refusal, even one after the lint,
   carries no `found_delta` and writes no snapshot: the next line's delta
   then covers what it saw.
2b. Decide whether to sweep at all (E3-R7, curamoir.idempotence), after
   step 2 (the work list and `current` are the whole base's) and before
   step 3 (the remedies are fixes too, and nothing has edited the sandbox
   yet), in four sub-steps:
   (i) Plan tier 1 (E3-R1b, curamoir.tier1) on the still-pristine
       sandbox: `plan = tier1.plan_scan(sandbox, run.ledger.entries())`,
       and `tier1_state = plan` (the tier1 field note below).
   (ii) Decide: `decision = idempotence.sweep_decision(
       sandbox.work_list, sandbox.base, run.ledger.entries(), open_sweep_prs,
       branch_prefix=repo_config.strict_config(repo)["guardrails"]
       ["branch_prefix"], tier1=plan.will_scan)`. The prefix is required
       and is read exactly as job_guard.job_gateway reads it for the cap,
       so the decision and the cap read one setting and see the same sweep
       PRs (idempotence, "The configured prefix"), of one repository,
       origin's (step 0 (iii)).
       `open_sweep_prs` is
       `functools.partial(github.open_sweep_prs, repo, gh_repo=gh_repo)`,
       step 0 (iii)'s binding with step 1's `gh_repo`, never the bare
       function or its result: the `gh`-backed
       callable curamoir.github.open_sweep_prs (`gh pr list --state open
       --limit 1000 --json
       headRefName,number,url,body,files,isCrossRepository`, each item
       mapped through idempotence.sweep_pr_listing;
       `isCrossRepository` is the trust boundary, guards.pr_cap's
       docstring), the one the open-PR cap also counts with (guards.pr_cap,
       whose docstring holds the listing contract). The sweep puts
       `idempotence.sweep_marker(sandbox.base)` on a line of the sweep PR's
       body, so a fresh CI checkout whose ledger lacks this line still
       knows the PR's base.
   (iii) If `decision.terminal` (NO_WORK, DUPLICATE_OPEN_PR, or UNKNOWN):
       `decision.record(run, delta, **tier1_state.ledger_fields())`,
       which writes a success with `work` and `found_delta` or, for
       UNKNOWN, a refusal (Reason.IDEMPOTENCE, without them); then the
       snapshot (step 2a) unless it was the refusal; and stop: no fix
       step, no PR.
   (iv) Otherwise `decision.apply_to(sandbox)`, which narrows
       `sandbox.work_list` to the violations in files no open sweep PR
       changes, so steps 3 to 8 see only those and the scope check
       refuses an edit to a held file; and pass
       `**decision.ledger_fields()` on the line the sweep later writes,
       whatever its outcome.
2c. Tier 1 (E3-R1b), only when `plan.will_scan`, still before anything
   edits the sandbox. First scan: `scan = tier1.run_tier1_scan(sandbox,
   plan, decision.held_paths, run.ledger.entries())`, which runs the
   read-only scan call on `models.cheap` (bounded by tier1.SCAN_MAX_TURNS,
   inside the run's one `per_run_ceiling_usd` estimate) and returns the
   findings it accepted; then `tier1_state = scan`. Its tier1.ScanRefused
   (a ScopeCheckError: the scan changed the sandbox) is a refusal, as in
   step 6: `except tier1.ScanRefused as error: tier1_state = error`,
   record the refusal with its field, and stop (the findings are not
   installed). Otherwise install the findings: `sandbox.work_list =
   tier1.with_findings(sandbox.work_list, scan)`. Then settle the
   decision: `decision = decision.after_tier1(sandbox.work_list, scan)`.
   If that is terminal (TIER1_FAILED, which `record` writes as a failure,
   DUPLICATE_OPEN_PR, or NO_WORK): `decision.record(run, delta,
   **tier1_state.ledger_fields())`, the snapshot (step 2a), and stop.
   Otherwise go on.

   **The tier1 field.** Every line from step 2b(iii) on, refusals
   included, carries `**tier1_state.ledger_fields()`, whatever it holds
   by then (steps 2b(iii), 2c, 6, 7, 8 and 9), except UNKNOWN's refusal
   at step 2b(iii): Decision.record drops the field there, because that
   line carries no fields and no scan has run by then. Steps 2b(i) and
   2b(ii) write no line. The field is there so that the once-per-base
   rule and the attempt cap (curamoir.tier1, "Once per base") can read
   it back, and so the scan's spend is on record. The runner holds ONE
   object, `tier1_state`: the Tier1Plan from step 2b(i), replaced in
   step 2c by the Tier1Scan run_tier1_scan returns or the ScanRefused it
   raises. All three have `ledger_fields()`:
   - a plan that does not scan (OFF, NO_ELIGIBLE, ALREADY_SCANNED,
     ATTEMPTS_EXHAUSTED) gives its skipped scan's field; a READY plan
     gives {} (a READY plan whose sweep ended at 2b never scanned, and a
     line must not say it did);
   - a scan gives its own field;
   - a ScanRefused gives its scan's field (`.scan`, status
     SANDBOX_CHANGED).
   WHY refusals too: a refusal after the scan (the ScanRefused of step
   2c, and the refusals of steps 6, 7 and 8, written as
   `run.refused(refusal, **tier1_state.ledger_fields())`) is an attempt
   that did not end the matter, and without the field the attempt cap
   never sees it (so the scan retries without bound) and the scan's
   cost goes unrecorded. A line carrying `scanned: true` counts as
   "already scanned" only if it is a success or carries `pr`; any other
   (a failure, a refusal) counts as an attempt, and the next sweep at
   that base scans again, up to MAX_SCAN_ATTEMPTS.

   **The line's cost.** Spend is derived at read time from recorded
   tokens, never from a stored dollar figure (budget.py): the
   authoritative spend of a sweep line is its top-level `input_tokens`
   and `output_tokens`, priced at its top-level `model`. That is what
   budget.month_to_date reads (the E0-R4 ceiling and /curamoir:status),
   and what E11-R2 re-prices when a price changes; it reads nothing
   inside `tier1`. So the runner writes those two counts as TOTALS: the
   fix step's usage plus, once step 2c ran the scan, the scan's
   (Tier1Scan.usage, or a ScanRefused's `.scan.usage`); a line with no
   fix step has the scan's alone. Without the scan's share the ceiling
   never sees what the scan spent.
   A call that ran spent money even when it reported no usage, so the
   line must not read as $0. That is a scan that is `called`
   (Tier1Scan.called: its status is in tier1's `_CALLED`, SCANNED,
   FAILED or SANDBOX_CHANGED, and its `claude` started), or a fix step
   that started (FixStepRun.started: it exited or timed out), whose
   usage is None (it timed out, crashed or printed something that is not
   JSON) or lacks a numeric `input_tokens` or `output_tokens`. If any
   call on the line ran and reported no usable usage, the runner writes
   BOTH top-level counts as budget.UNRECORDED, with `model` as usual: not
   the other call's counts alone, and never 0.
   budget.month_to_date counts that line as `unrecorded` (apart from
   unreadable lines, since it is not damage), so the month's total is a
   stated lower bound and never a silent zero. The other call's known
   tokens leave the total with it; one count per line cannot say "this
   much, and more", and the hedge is the safer error (cm-kpz.1's
   per-call record would keep them).
   A scan or fix step whose `claude` could not start at all (an OSError:
   no executable) made no call: it spends nothing and adds no counts,
   though such a scan is FAILED and still counts as an attempt
   (tier1.scan_attempts).
   The counts are noted before the call and corrected after it:
   immediately before a call's `claude` process starts (run_fix_step's
   and run_tier1_scan's `before_call`, after every step that can fail
   without a call, so a failure there leaves neither counts nor an
   attempt), the runner notes both counts as UNRECORDED with `model`
   (and, for the scan, the FAILED `tier1` field of this call, an
   attempt), so a run that ends during or right after the call, by a
   signal, ^C or an exception, still records spend and attempt; once the
   call's outcome is known they are replaced by its own fields, and
   removed for a call that never started (curamoir.sweep_job, "The
   line's cost").
   A line on which no call started (no scan is
   `called`, and no fix step `started`) omits `input_tokens`,
   `output_tokens` and `model` entirely, and never writes zeros.
   month_to_date is defensive about it (a 0/0 line costs $0 without
   being priced, so it cannot raise UnknownModel and turn the ceiling
   off), but zeros would claim a call that never happened.
   The top-level `cost_usd` is the CLI's reported figure (the fix step's
   `total_cost_usd` plus the scan's `tier1_state.cost_usd`), kept for
   the record, not for the ceiling. `tier1.cost_usd` is a COMPONENT of
   it, kept to attribute the scan's share: any reader that sums
   `cost_usd` sums the top-level figure and never adds `tier1.cost_usd`
   to it.
   Known limit: one top-level `model` prices both calls correctly only
   while both run on the same model (today both run on `models.cheap`).
   If the fix step and the scan ever run on different models, that
   single `model` misprices one of them, and the line will need a token
   record per call (cm-kpz.1, E11-R1: every model call attributed with
   its own tokens). A second known limit: the counts leave out the
   cache tokens (the usage's `cache_creation_input_tokens` and
   `cache_read_input_tokens`), so a call that used the prompt cache is
   under-priced until cm-2ui counts them. curamoir.sweep_job writes these
   top-level fields on each sweep line on which a call started.

   **A cap of 0.** `sweep.max_files_per_pr: 0` is a valid count, and
   means no PR may carry a file. Tier 1 reads it as "no candidate file"
   (no call, no spend); the sweep reads it the same way for the fix step:
   before step 3, with no remedy applied and no fix call made, it records
   a refusal, Reason.FILE_CAP (curamoir.sweep_job). A refusal, as the
   open-PR cap's `max_open_prs: 0` is one: the configuration declines the
   work, and no `found_delta` or snapshot is written (step 2a).
3. Apply the file-level remedies, still before anything else edits it:
   `remedies.apply_file_remedies(sandbox)`, which deletes each file whose
   violation's rule says `remedy: delete_file` and nothing at the base
   refers to (the agent has no deletion tool). Build the prompt from
   `result.for_agent(sandbox.work_list)`, which leaves out what the runner
   handled, and from `base_principles(sandbox)`; report `result.skipped` as
   unfixed, with each reason.
4. Run the fix step in it: `fix_step.run_fix_step(sandbox, prompt)`, which
   snapshots the sandbox (FixStepRun.git_state) before the agent starts,
   so after the remedies; the review still sees their deletions, because
   it diffs against the base commit. A fix step that did not finish (it
   could not start, timed out, exited non-zero, or its payload is missing
   or reports an error) is a failure with no review: a stopped agent may
   have left a file half-written. When the remedies left the agent
   nothing, no call is made, and the review holds the sandbox to a
   snapshot taken there instead (git_guard.snapshot_git_state).
5. Review it: `verify_fix(sandbox, git_state=run.git_state)`. Same sandbox,
   same snapshot, same base: there is nothing else to pass.
6. Treat git_guard.GitGuardError (ScopeCheckError is one) as a refusal
   (with `**tier1_state.ledger_fields()`, step 2c's note): no verdict is
   not a verdict, and an unverified fix is not a fix.
7. A `nothing_changed` verdict is a failure: NOTHING_CHANGED after a fix
   step, or, when no fix step ran and every remedy was skipped,
   NOTHING_REMEDIED with `remedies_skipped` (curamoir.sweep_job).
   On an ok verdict with paths, test it (E3-R4).
   Trim to `sweep.max_files_per_pr` BEFORE this, so the tree tested is the
   PR's, with the lint's paths first (`trimmed = tier1.trim_pr_paths(
   verdict.paths, sandbox.work_list, max_files_per_pr)`: rule fixes take
   precedence over tier-1 fixes; every line from here carries the tier1
   field, step 2c's note): `gate = green_gate.run_test_gate(sandbox,
   verdict, paths=trimmed)`, which runs the base's test command in the
   sandbox and, on red, drops fixed paths until the rest is green. Then,
   by outcome:
   - strategy `green` or `reduced`: compute `fix_fields` (step 8), patch
     and open the PR (step 9), then `run.succeeded(...,
     **gate.ledger_fields(), **fix_fields)`; then the snapshot (step 2a);
   - strategy `abandoned`: no PR; `run.failed(gate.render(),
     **gate.ledger_fields(), found_delta=delta)` (render() carries the
     why; `found_delta` is step 2's); then the snapshot (step 2a);
   - GateError raised: a refusal, as in step 6, `run.refused(...)` with no
     `test_gate` field, since there is no GateResult to record.
8. Record the fixes (E3-R5), still in the sandbox and before the patch:
   `records = fix_counts.record_fixes(sandbox, gate, remedies_result)`
   (with the lint options step 2 used, the same timeout: the re-lint is a
   full lint), which lints the PR's tree again with the base's rules and
   keeps only the violations in `gate.paths_for_pr` it no longer reports;
   then the tier-1 records, `records += tier1.record_tier1_fixes(sandbox,
   gate, fix_run)` (`fix_run` is step 4's FixStepRun, whose reply says
   which findings the agent fixed), then `fix_fields =
   fix_counts.ledger_fields(records, delta)`, beside the tier1 field
   (step 2c's note). They are written with the sweep's success line, once
   the PR is opened, so a sweep whose PR failed to open records no fixes
   (its failure line carries `found_delta=delta` and step 2b's fields, no
   `fixes`). record_fixes' ScopeCheckError, GateError or
   sweep.LintUntrusted is a refusal, as in step 6: a PR whose fixes
   cannot be counted would starve the promotion ladder silently.
9. Take exactly `gate.paths_for_pr` out: `gate.patch(sandbox)`, the
   patch of those paths built from the bytes the green run tested (what
   `sandbox.patch(gate.paths_for_pr)` gives when nothing changed since),
   refused with GateError if a kept path no longer holds them (a refusal
   as in step 6: a process the tests left running rewrote one), and
   published by `publish.publish(repo, base=sandbox.base, patch=...,
   paths=gate.paths_for_pr, branch=..., branch_prefix=..., gh_repo=...,
   message=..., title=..., body=...)` (curamoir.publish, cm-fkd.12): the
   commit is built from the patch in the checkout's object store (never
   its work tree or index), the default-branch guard judges the branch,
   the user's git pushes it (pre-push hooks run), and gh opens the PR on
   `gh_repo`, which is `publish.origin_gh_repo(repo)` resolved once by the
   body (step 1) and also bound into step 2b's listing; the cap's listing
   resolves origin's repository itself when it is called (step 0 (iii)),
   so all three name origin's repository. What to record for
   each outcome, and where the branch is, is publish's "Errors" table:
   PublishRefused is a refusal (nothing pushed); PushFailed,
   PrCreateFailed and PrOutcomeUnknown are failures with no `pr` (a
   PrCreateFailed with `branch_deleted=False` may have left the branch on origin,
   and PrOutcomeUnknown's PR may exist); Published is the success with
   `pr`. List only those paths' fixes in the PR body,
   each with its principle id (E3-R3); the tier-1 ones as
   `tier1.tier1_pr_lines(records, sandbox.work_list.tier1)` gives them. Once
   the PR exists, its line (success, or a failure after it) carries
   `pr=idempotence.pr_field(number, branch, url, sandbox.base,
   gate.paths_for_pr)`: the next sweep's step 2b holds those files. When
   it is empty (strategy `abandoned`, with any cause in
   green_gate.CAUSES), open no PR at all. Never `verdict.paths` once
   the gate has run, never `git add -A`, never the whole tree.
10. Never re-run the fix agent in the user's checkout, not even "to apply
    the same fix": the sandbox is what the verdict is about.

**The rules, and why each is drawn where it is.**

- *Files.* Every changed path must be one of the work list's `paths`. A new
  file is never in scope: the lint ran on the base commit, so a file that did
  not exist there has no violation, and E3-R2 says "only files with
  violations". A deleted file is out of scope too, with one exception below.
  Renames are read with `--no-renames`, so a rename is a deletion and an
  addition, and is refused as both. Paths are compared in Unicode NFC on
  both sides: a macOS file system hands names back in the form they were
  written, and git reports NFC.
- *Deleting a file.* Allowed only when the file carries a file-level
  violation — the lint reports a `target: path` rule's match (a name like
  `handlers_v2.py`) as the empty region at 1:1 (docs/curamoir-lint.md) —
  AND that violation's rule says `remedy: delete_file` (rules.REMEDIES), as
  loaded at the base by lint_sandbox. For such a violation the file's
  existence is the violation, so removing it is the fix rather than a
  refactor. The runner makes that deletion itself (remedies), before the
  agent starts; the agent may delete nothing, and the prompt says so. WHY
  the remedy is required here too, and not only the 1:1 region: a path
  rule without it may want a rename instead, and requiring it keeps the
  exception the runner's even if a later toolset could delete.
- *Dependencies.* Any change to a dependency manifest or lockfile (by name:
  DEPENDENCY_FILES) is refused, whatever else is in it and even when the
  file is on the work list. A section-aware check — "pyproject.toml may
  change, just not its dependency tables" — would be a deny-list over an
  open set (project.dependencies, optional-dependencies, dependency-groups,
  build-system.requires, tool.poetry.group.*, tool.uv.sources, hatch envs,
  …): it passes the first table it forgot. Refusing the whole file costs a
  fix the lint's code rules practically never ask for. Separately, a newly
  imported top-level module (Python `import`, JS/TS `import`/`require`) that
  is not in the standard library, not local to the repository and not
  imported anywhere in it at the base commit is a new dependency in all but
  the manifest, and is refused as one.
- *Refactors.* Every hunk (git's `-U0` hunks: the smallest changed regions)
  must touch, or lie within WINDOW lines of, a violation in the same file.
  The one other hunk allowed is an insertion into the module's header — the
  stretch before its first function or class — of imports and of new
  module-level names bound to a literal or a logger; _only_header_statements
  and _header_value hold the exact rule and the reason for each part of it.
- *Signatures.* For Python, both versions are parsed and every function's
  and class's signature (arguments, defaults, annotations, return
  annotation, async-ness, decorators; a class's bases, keywords and
  decorators) compared by qualified name: one changed, removed or added is a
  signature change. It is allowed only where a principle at the base commit
  says `permits: signature_change` (E2-R1's format, src/curamoir/
  principles.py) and a violation of that principle lies within the
  definition (± WINDOW). For JavaScript and TypeScript there is no parser
  here, so any changed line that looks like a declaration (DECLARATION)
  counts as one — conservative, and blind to a parameter changed on a line
  of its own (see Limits).
- *Encoding.* A Python file is reviewed only if CPython decodes it as UTF-8
  on both sides (tokenize.detect_encoding over its bytes, as the
  interpreter does), and it is parsed from those bytes: the review reads
  text as UTF-8, and `ast.parse` of a str ignores a coding cookie, so a
  `# coding: utf-7` line turns comments the review sees into an import and
  a new definition at run time. Any other source encoding is UNVERIFIABLE,
  the base's included. WHY the base too: a latin-1 base with ASCII content
  reads the same either way, but proving that per file is more code than
  refusing a rare file costs, and refusing loses one fix where guessing
  wrong admits one. And the fix may not add, remove or change a coding
  cookie on line 1 or 2 at all, a UTF-8 one included (ENCODING_DECLARATION):
  no lint fix needs to, and with the declaration held fixed the review and
  CPython cannot come to read different text.

**WINDOW = 3.** A mechanical fix sometimes has to touch a line just above
the violation: chaining a cause (`raise X(...) from error`) needs the
`except` above it to bind `as error`, one to three lines up in ordinary code.
Three lines covers that, and is still small enough that rewriting the rest
of a function to "fix" one line in it is refused.

**Limits, stated rather than hidden.**

- Every path that changed in the sandbox is checked, whoever changed it. The
  fix step runs with `--restricted`, which loads no user hooks or plugins
  (fix_step), so cúramóir's own hooks do not write there: the one it
  loads back, the PreToolUse denylist (`--settings`, cm-60c), only prints
  its decision. Anything else that writes there is refused like the
  agent's own edits (cm-sto).
- JS/TS signatures are matched by line shape, not parsed: a parameter edited
  on its own line inside a multi-line parameter list is not seen.
- New-import detection knows the standard library from the interpreter
  running this (`sys.stdlib_module_names`, 3.10+; on older interpreters every
  import not already in the repository is reported) and Node's built-ins
  from a list. A package that is declared in a manifest but imported nowhere
  yet is reported as new.
- A dynamic import is seen only when its module is a string literal
  (`__import__("x")`, `import_module("x")`); `import_module(name)` with a
  computed name is not. In JS/TS a `require(` or `import(` whose argument
  is not one literal (quoted, or a template with no `${`), or is on
  another line, is NEW_DEPENDENCY: what it loads cannot be known. Lines are
  matched as text, so one in a comment or string counts too (the safe
  direction). Only Python source counts as a local module, and
  a directory holding JS/TS source as a local package; a bare specifier
  that a tsconfig `paths`/`baseUrl` alias resolves locally, and a compiled
  extension module (`.so`, `.pyd`), are reported as new.
- Git. The sandbox's git files are the clone's own, not the owner's, and
  are held to the snapshot as files before any git command runs; git runs
  hardened (git_guard). The base's own committed `.gitattributes` still
  apply to the diff: `-diff` and `binary` are overridden with `--text`, and
  no filter or external driver is configured to run, but an attribute that
  changes how a file is read (`working-tree-encoding`) is the owner's own,
  and a file it makes unreadable as UTF-8 is unverifiable, not passed.
- Python's own semantics only: a change of behaviour inside a function body
  within the window is the violation's fix as far as this module can tell.
  Whether it is a correct fix is E3-R4's question (the tests, green_gate).

Syntax that imports on 3.9, like sweep.py: the sweep reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import ast
import builtins
import collections
import difflib
import io
import json
import os
import re
import sys
import tokenize
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from curamoir.git_guard import (
    GitGuardError,
    git,
    require_sandbox,
    tree_changes,
    verify_git_state,
)
from curamoir.principles import PrincipleError, PrinciplesFile, parse_principles
from curamoir.rules import DELETE_FILE, RuleError, load_rules
from curamoir.sweep import work_list

#: Lines either side of a violation's span that a fix may touch.
WINDOW = 3

#: Breach kinds. Strings, because the ledger line and the PR body record them.
FILE_NOT_IN_WORK_LIST = "file_not_in_work_list"
FILE_ADDED = "file_added"
FILE_DELETED = "file_deleted"
DEPENDENCY_CHANGE = "dependency_change"
NEW_DEPENDENCY = "new_dependency"
OUTSIDE_VIOLATION = "outside_violation"
SIGNATURE_CHANGE = "signature_change"
UNVERIFIABLE = "unverifiable"
GIT_INTERNALS_CHANGED = "git_internals_changed"
#: The sandbox's HEAD no longer names the base commit. The diff is always
#: taken against the base SHA, so this hides nothing from it; it is its own
#: kind because no fix step moves HEAD, and one that did has been doing
#: something other than editing files.
BASE_MOVED = "base_moved"
#: A path changed on disk that git does not report (inside an uninitialised
#: submodule, or hidden from git some other way): a change git cannot see
#: cannot be reviewed, and cannot be carried to a PR either.
HIDDEN_CHANGE = "hidden_change"
#: A Python file's coding cookie (PEP 263, line 1 or 2) was added, removed
#: or changed. Its own kind because it changes how CPython decodes every
#: line the review read (module docstring, *Encoding*).
ENCODING_DECLARATION = "encoding_declaration"

KINDS = (FILE_NOT_IN_WORK_LIST, FILE_ADDED, FILE_DELETED, DEPENDENCY_CHANGE,
         NEW_DEPENDENCY, OUTSIDE_VIOLATION, SIGNATURE_CHANGE, UNVERIFIABLE,
         GIT_INTERNALS_CHANGED, BASE_MOVED, HIDDEN_CHANGE, ENCODING_DECLARATION)

#: The principle permission that lifts SIGNATURE_CHANGE (principles.PERMISSIONS).
SIGNATURE_PERMISSION = "signature_change"

#: Change statuses.
ADDED = "added"
MODIFIED = "modified"
DELETED = "deleted"

#: Dependency manifests and lockfiles, by file name. A change to any of these
#: is DEPENDENCY_CHANGE (the module docstring says why the whole file).
DEPENDENCY_FILES = frozenset({
    # Python
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock",
    "poetry.lock", "uv.lock", "pdm.lock", "environment.yml", "environment.yaml",
    "conda-lock.yml",
    # JavaScript / TypeScript
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lock", "bun.lockb", "deno.json",
    "deno.jsonc", "deno.lock",
    # Others a polyglot repository may hold
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock",
})

#: requirements.txt, requirements-dev.txt, requirements/base.in, constraints.txt…
_REQUIREMENTS = re.compile(r"^(requirements|constraints)([-_.][^/]*)?\.(txt|in)$")

PYTHON_SUFFIXES = (".py", ".pyi")
SCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

#: A JS/TS line that declares something callable or a type: a changed line of
#: this shape is treated as a signature change. Control-flow keywords are
#: excluded from the method form, which would otherwise match `if (x) {`.
DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\b|class\b|interface\b|type\s+\w+\s*(?:<[^=]*>)?\s*=|enum\b)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::[^=]+)?=\s*(?:async\s+)?"
    r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|\w+\s*=>)"
    r"|^\s*(?:(?:public|private|protected|static|readonly|async|override|get|set)\s+)*"
    r"(?!(?:if|for|while|switch|catch|return|function|with)\b)[\w$]+\s*(?:<[^>]*>)?"
    r"\s*\([^)]*\)\s*(?::\s*[^{;]+)?\{?\s*$"
)

#: A JS/TS import specifier written as a literal: quoted, or a template
#: literal with no `$` (so no `${...}` substitution). Two groups, one empty.
_SCRIPT_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|^\s*import\s+)"""
    r"""(?:['"]([^'"]+)['"]|`([^`$]+)`)""")

#: A `require(` or `import(` call (not a method of that name), and the one
#: literal argument it must have for _SCRIPT_IMPORT to know what it loads.
_SCRIPT_LOADER_CALL = re.compile(r"(?<![\w$.])(?:require|import)\s*\(")
_SCRIPT_LITERAL_ARGUMENT = re.compile(r"""\s*(?:'[^'\n]*'|"[^"\n]*"|`[^`$\n]*`)\s*[,)]""")

#: Node's built-in modules; `node:`-prefixed specifiers are built-ins too.
NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net", "os",
    "path", "perf_hooks", "process", "punycode", "querystring", "readline",
    "repl", "stream", "string_decoder", "sys", "test", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads",
    "zlib",
})

_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


@dataclass(frozen=True)
class Hunk:
    """One changed region, in `git diff -U0`'s terms: `old_count` lines from
    `old_start` were replaced by `new_count` lines from `new_start`. A count
    of 0 is a pure insertion or deletion, and then the start is the line
    BEFORE it (0 for the top of the file), as git writes it."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass(frozen=True)
class FileChange:
    """One changed path. `old_text` is None when the file was added and
    `new_text` None when it was deleted. `hunks` None means the core computes
    them (difflib); the git adapter passes git's own. `unverifiable` is set,
    with the reason, when the content cannot be checked (binary, not UTF-8, a
    symlink or submodule, a mode change)."""

    path: str
    status: str
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    hunks: Optional[tuple] = None
    unverifiable: Optional[str] = None


@dataclass(frozen=True)
class Breach:
    """One way the fix went beyond E3-R2's limits. `line` is in the base
    commit's file where it names an old line, else the fixed file's (`detail`
    says which); None for a whole-file breach."""

    kind: str
    path: str
    line: Optional[int]
    detail: str

    def render(self):
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{self.kind} {where}: {self.detail}"


@dataclass(frozen=True)
class ScopeVerdict:
    """The review step's answer. `paths` is every changed path, sorted: what a
    PR made from an accepted fix commits, and nothing else.

    `ok` with no paths (`nothing_changed`) is a fix step that changed
    nothing: in scope, and nothing to open a PR for or count as fixed."""

    breaches: tuple
    paths: tuple

    @property
    def ok(self):
        return not self.breaches

    @property
    def nothing_changed(self):
        return not self.paths

    def kinds(self):
        """The breach kinds present, each once, in KINDS order."""
        present = {breach.kind for breach in self.breaches}
        return tuple(kind for kind in KINDS if kind in present)

    def render(self):
        if self.ok and self.nothing_changed:
            return "in scope: nothing changed, so nothing was fixed"
        if self.ok:
            return f"in scope: {len(self.paths)} file(s) changed, all within E3-R2's limits"
        lines = [f"out of scope: {len(self.breaches)} breach(es) of E3-R2"]
        lines.extend(f"- {breach.render()}" for breach in self.breaches)
        return "\n".join(lines)

    def as_dict(self):
        """For the ledger line and the PR body (cm-fkd.4, cm-fkd.6)."""
        return {
            "ok": self.ok,
            "paths": list(self.paths),
            "breaches": [
                {"kind": b.kind, "path": b.path, "line": b.line, "detail": b.detail}
                for b in self.breaches
            ],
        }


class ScopeCheckError(GitGuardError):
    """No verdict can be given: the review was asked about something it
    cannot check (no snapshot, not a sandbox, no work list, principles that
    do not parse). A GitGuardError, so a runner that refuses on that refuses
    on this too: an unverified fix is not a fix."""


# --- the pure core ---------------------------------------------------------------

def check_scope(changes, violations, permitting=frozenset(), known_modules=frozenset(),
                window=WINDOW, deletable_rules=frozenset()):
    """The verdict on `changes` (FileChanges) against `violations` (anything
    with path, line, col, end_line, end_col, principle_id and rule_id: the
    work list's items). `permitting` is the ids of principles that permit
    signature changes; `known_modules` the top-level module and package names
    the repository already uses or contains, which are not new dependencies;
    `deletable_rules` the ids of rules with `remedy: delete_file` (rules.
    REMEDIES), the only ones whose file-level violation licenses deleting the
    file. Paths are matched in NFC (module docstring)."""
    by_path = {}
    for violation in violations:
        by_path.setdefault(nfc(violation.path), []).append(violation)
    breaches = []
    for change in sorted(changes, key=lambda c: c.path):
        breaches.extend(_check_file(change, by_path.get(nfc(change.path), ()), permitting,
                                    known_modules, window, deletable_rules))
    return ScopeVerdict(breaches=tuple(breaches),
                        paths=tuple(sorted({c.path for c in changes})))


def _check_file(change, violations, permitting, known_modules, window, deletable_rules):
    path = change.path
    found = []
    if is_dependency_file(path):
        found.append(Breach(
            DEPENDENCY_CHANGE, path, None,
            "a dependency manifest or lockfile changed; a sweep fix adds, removes "
            "and re-pins no dependency"))
    if change.status == ADDED:
        found.append(Breach(
            FILE_ADDED, path, None,
            "a new file; the lint ran on the base commit, so no violation is in it"))
        return found
    if not violations:
        found.append(Breach(
            FILE_NOT_IN_WORK_LIST, path, None,
            "changed, but the work list has no violation in this file"))
        return found
    if change.unverifiable:
        found.append(Breach(UNVERIFIABLE, path, None, change.unverifiable))
        return found
    if change.status == DELETED:
        if not any(is_file_level(v) and v.rule_id in deletable_rules
                   for v in violations):
            found.append(Breach(
                FILE_DELETED, path, None,
                "deleted, but none of its violations is about the file itself (a "
                "`target: path` rule's empty region at 1:1) with a rule that says "
                "`remedy: delete_file`"))
        return found

    old, new = change.old_text, change.new_text
    hunks = change.hunks if change.hunks is not None else compute_hunks(old, new)
    old_lines, new_lines = git_lines(old), git_lines(new)
    spans = [violation_span(v, window) for v in violations]
    python = path.endswith(PYTHON_SUFFIXES)
    script = path.endswith(SCRIPT_SUFFIXES)

    old_tree = new_tree = None
    if python:
        breach = _encoding_breach(path, old, new)
        if breach:
            found.append(breach)
            return found
        old_tree, new_tree, problem = _parse_both(old, new)
        if problem:
            found.append(Breach(UNVERIFIABLE, path, None, problem))
            return found

    header_end = _header_end(old_tree, old_lines, python)
    for hunk in hunks:
        if any(overlaps(_hunk_range(hunk), span) for span in spans):
            continue
        added = new_lines[hunk.new_start - 1:hunk.new_start - 1 + hunk.new_count]
        if (hunk.old_count == 0 and hunk.old_start < header_end
                and _only_header_statements(added, python, old_tree, new_tree,
                                            hunk.new_start)):
            continue
        found.append(Breach(
            OUTSIDE_VIOLATION, path, max(hunk.old_start, 1),
            f"{_describe(hunk)} at base line {max(hunk.old_start, 1)}, "
            f"more than {window} line(s) from every violation in this file"))

    # A tier-1 finding (rule_id None, curamoir.tier1) never licenses a
    # signature change, whatever its principle permits: its span is the scan
    # agent's choice, and a licence the agent can draw is no limit.
    permitted_spans = [violation_span(v, window) for v in violations
                       if v.principle_id in permitting and v.rule_id is not None]
    if python:
        found.extend(_python_signature_breaches(path, old_tree, new_tree, permitted_spans,
                                                hunks))
        found.extend(_new_import_breaches(path, _python_imports(old_tree),
                                          _python_imports(new_tree), known_modules,
                                          _STDLIB))
    elif script:
        found.extend(_script_signature_breaches(path, hunks, old_lines, new_lines,
                                                permitted_spans))
        found.extend(_new_import_breaches(path, _script_imports(old_lines),
                                          _script_imports(new_lines), known_modules,
                                          NODE_BUILTINS))
        found.extend(_computed_load_breaches(path, old_lines, new_lines))
    return found


def nfc(path):
    """`path` in Unicode normalisation form C, the form git reports."""
    return unicodedata.normalize("NFC", path)


def is_dependency_file(path):
    """True for a dependency manifest or lockfile (DEPENDENCY_FILES, and the
    requirements/constraints family), by its name wherever it sits."""
    name = PurePosixPath(path).name
    return name in DEPENDENCY_FILES or bool(_REQUIREMENTS.match(name)) or (
        PurePosixPath(path).parent.name == "requirements" and name.endswith((".txt", ".in")))


def git_lines(text):
    """`text`'s lines as git numbers them: split at "\\n" only, without the
    empty string after a final newline, and with a CRLF line's "\\r" dropped.

    WHY not str.splitlines: it also splits at \\f, \\v, \\x1c-\\x1e, \\x85,
    U+2028/U+2029 and a lone \\r, none of which git counts, so one form feed
    above a hunk shifted every slice below it by a line and the check read
    the line above the one inserted. A lone \\r is the one character Python
    *does* count as a line break where git does not; _parse_both refuses
    such a file rather than match parse lines to the wrong hunk lines."""
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [part[:-1] if part.endswith("\r") else part for part in parts]


def compute_hunks(old_text, new_text):
    """`-U0`-style hunks from difflib, for callers without git's. Not always
    git's minimal diff, so the adapter passes git's own."""
    matcher = difflib.SequenceMatcher(None, git_lines(old_text), git_lines(new_text),
                                      autojunk=False)
    hunks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_count, new_count = i2 - i1, j2 - j1
        hunks.append(Hunk(i1 if old_count == 0 else i1 + 1, old_count,
                          j1 if new_count == 0 else j1 + 1, new_count))
    return tuple(hunks)


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text):
    """The hunks of one file's `git diff -U0` output."""
    hunks = []
    # "\n" only, like git_lines: splitlines would also break an added line at
    # a \f or \x85, and the text after it could pass for a hunk header.
    for line in diff_text.split("\n"):
        match = _HUNK_HEADER.match(line)
        if match:
            old_start, old_count, new_start, new_count = match.groups()
            hunks.append(Hunk(int(old_start), 1 if old_count is None else int(old_count),
                              int(new_start), 1 if new_count is None else int(new_count)))
    return tuple(hunks)


def is_file_level(violation):
    """Whether `violation` is about the file itself: the empty region at 1:1
    that the lint reports for a `target: path` rule (docs/curamoir-lint.md)."""
    return (violation.line, violation.col, violation.end_line, violation.end_col) == (1, 1, 1, 1)


def violation_span(violation, window):
    """The violation's base lines, widened by `window`. The end is exclusive at
    a column, so a match ending at column 1 of `end_line` ends on the line
    before it."""
    last = violation.end_line
    if violation.end_col == 1 and violation.end_line > violation.line:
        last -= 1
    return (violation.line - window, max(last, violation.line) + window)


def _hunk_range(hunk):
    """The base lines a hunk touches. A pure insertion after line n sits
    between n and n+1, so it is near whatever either of those is near."""
    if hunk.old_count == 0:
        return (hunk.old_start, hunk.old_start + 1)
    return (hunk.old_start, hunk.old_start + hunk.old_count - 1)


def overlaps(first, second):
    """Whether two inclusive (first, last) line ranges share a line."""
    return first[0] <= second[1] and second[0] <= first[1]


def _describe(hunk):
    if hunk.old_count == 0:
        return f"{hunk.new_count} line(s) inserted"
    if hunk.new_count == 0:
        return f"{hunk.old_count} line(s) deleted"
    return f"{hunk.old_count} line(s) replaced by {hunk.new_count}"


_LONE_CR = re.compile(r"\r(?!\n)")

#: PEP 263's coding cookie, as CPython's tokenizer matches it.
_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def _encoding_breach(path, old, new):
    """A Breach if CPython would not decode `old` or `new` as the UTF-8 the
    review read them as, or if the fix touched a coding cookie; else None
    (module docstring, *Encoding*)."""
    for side, text in (("base", old), ("fixed", new)):
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(text.encode("utf-8")).readline)
        except (SyntaxError, UnicodeError) as error:
            return Breach(UNVERIFIABLE, path, None,
                          f"the {side} version's source encoding cannot be read ({error})")
        if encoding not in ("utf-8", "utf-8-sig"):
            return Breach(UNVERIFIABLE, path, None, (
                f"CPython decodes the {side} version as {encoding} (its coding cookie) "
                "and the review reads UTF-8, so what was checked would not be what runs"))
    old_cookies, new_cookies = _coding_cookies(old), _coding_cookies(new)
    for number in (1, 2):
        if old_cookies.get(number) != new_cookies.get(number):
            return Breach(ENCODING_DECLARATION, path, number, (
                f"a coding cookie on line {number} was added, removed or changed; it "
                "decides how CPython reads every line, and a sweep fix does not touch it"))
    return None


def _coding_cookies(text):
    """Line number (1 or 2) -> the coding cookie line there."""
    return {number: line for number, line in enumerate(text.split("\n", 2)[:2], start=1)
            if _CODING_COOKIE.match(line)}


def _parse_both(old, new):
    """(old tree, new tree, None), or (None, None, why it cannot be checked).
    Parsed from the UTF-8 bytes, as CPython reads the file (a BOM included),
    not from the str, whose parse ignores a coding cookie
    (_encoding_breach has already held both sides to UTF-8)."""
    for side, text in (("base", old), ("fixed", new)):
        if _LONE_CR.search(text):
            return None, None, (f"the {side} version has a carriage return that does not "
                                "end a CRLF line; Python counts it as a line break and git "
                                "does not, so the parse cannot be matched to git's hunks")
    try:
        old_tree = ast.parse(old.encode("utf-8"))
    except (SyntaxError, ValueError) as error:
        return None, None, (f"the base version does not parse ({error.__class__.__name__}: "
                            f"{error}), so its signatures cannot be compared")
    try:
        new_tree = ast.parse(new.encode("utf-8"))
    except (SyntaxError, ValueError) as error:
        return None, None, (f"the fixed version does not parse ({error.__class__.__name__}: "
                            f"{error})")
    return old_tree, new_tree, None


def _header_end(tree, lines, python):
    """The first base line that is past the module header: its first top-level
    function or class (decorators included) for Python, its first line that is
    not blank, a comment or an import for anything else. Insertions before it
    may add imports and module-level assignments."""
    if python:
        starts = [
            min([node.lineno] + [d.lineno for d in node.decorator_list])
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        return min(starts) if starts else len(lines) + 1
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith(("//", "#", "/*", "*", "import ", "'use ",
                                                 '"use ')) and "require(" not in stripped:
            return number
    return len(lines) + 1


def _only_header_statements(added, python, old_tree=None, new_tree=None, first=None):
    """True when the added lines are only imports and simple module-level
    assignments (and blank lines or comments): what a fix may add to a
    module's header.

    For Python, every name they bind must also be new to the module, unless
    the statement is an import the module already has, word for word: an
    inserted `TAX_RATE = 0.3` would otherwise pass as "an assignment in the
    header" while changing what every later use of TAX_RATE means.

    And an assignment's value must be one of _header_value's shapes. WHY:
    header code runs whenever the module is imported — the sweep's own test
    step included — and the window rule exists to keep a fix next to its
    violation. Without this, `_boot = __import__("os").system(...)` is "a
    module-level assignment of a new name" and lands anywhere in the header.
    Imports are not narrowed further here: what they may bring in is the
    new-dependency check's job, and running an existing module's top level is
    what importing it has always meant.

    The statements are read from `new_tree`, the fixed module, as the whole
    top-level statements that exactly cover its lines `first` onwards (the
    hunk's new side): WHY not a parse of the added lines alone — `Y = True,`
    inserted into a multi-line `make(\\n a,\\n)` is a keyword argument in the
    module and an assignment on its own, and `import os` inside a triple-
    quoted string is text. Without `new_tree` (a caller holding only the
    lines) the lines are parsed on their own, as the whole module.

    A new name must also not be one whose meaning it would change for code
    already there (_shadows): a builtin (`print = None`), a dunder other than
    `__all__` (`__getattr__`, `__builtins__`), a name the module already reads
    anywhere, or any name at all after a `from x import *`, whose names this
    module cannot list."""
    if python:
        if new_tree is None:
            try:
                new_tree = ast.parse("\n".join(added))
            except (SyntaxError, ValueError):
                return False
            first = 1
        nodes = _statements_covering(new_tree, first, first + len(added) - 1, added)
        if nodes is None:
            return False
        bound, imports = _module_bindings(old_tree)
        referenced, star = _module_references(old_tree)
        loggers = (_logging_aliases(old_tree.body if old_tree else ())
                   | _logging_aliases(nodes))
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if ast.dump(node) in imports:
                    continue
                names = [(a.asname or a.name).split(".", 1)[0] for a in node.names]
                if any(n in bound or _shadows(n, referenced, star) for n in names):
                    return False
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not all(isinstance(t, ast.Name) and t.id not in bound
                           and not _shadows(t.id, referenced, star) for t in targets):
                    return False
                if node.value is not None and not _header_value(node.value, loggers):
                    return False
                # A module-level annotation is evaluated at import time too
                # (absent `from __future__ import annotations`), so it is held
                # to a type's spelling: `int`, `logging.Logger`.
                if isinstance(node, ast.AnnAssign) and not _dotted_name(node.annotation):
                    return False
            else:
                return False
        return True
    return all(not line.strip() or line.strip().startswith("//")
               or _SCRIPT_HEADER_LINE.match(line.strip()) for line in added)


def _statements_covering(tree, first, last, lines):
    """The top-level statements of `tree` that lie wholly within lines
    `first`..`last` (`lines` being those lines' text), or None when a
    statement crosses either edge — the lines are then part of something
    larger — or a line in the range is neither in a statement nor blank or a
    comment."""
    nodes, covered = [], set()
    for node in tree.body:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])
        end = getattr(node, "end_lineno", None) or node.lineno
        if end < first or start > last:
            continue
        if start < first or end > last:
            return None
        nodes.append(node)
        covered.update(range(start, end + 1))
    for number in range(first, last + 1):
        text = lines[number - first].strip() if number - first < len(lines) else ""
        if number not in covered and text and not text.startswith("#"):
            return None
    return nodes


_BUILTIN_NAMES = frozenset(vars(builtins))


def _shadows(name, referenced, star):
    """Whether binding `name` at module level could change what existing code
    means (see _only_header_statements)."""
    if star or name in _BUILTIN_NAMES or name in referenced:
        return True
    return name.startswith("__") and name.endswith("__") and name != "__all__"


def _module_references(tree):
    """(every name `tree` mentions as a Name or in a `global`/`nonlocal`,
    whether it has a `from x import *` anywhere)."""
    if tree is None:
        return set(), False
    names, star = set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            star = True
    return names, star


#: A JS/TS header line a fix may add: a static `import` (with or without
#: bindings) or `require` of a quoted specifier, with nothing chained after
#: it. Text, not a parse: a multi-line `import {` falls to the window rule,
#: which is the safe direction. The specifier is checked for new dependencies
#: elsewhere (_SCRIPT_IMPORT).
_SCRIPT_HEADER_LINE = re.compile(
    r"""^(?:import\s+(?:type\s+)?(?:[\w$*{}\s,]+?\s+from\s*)?"""
    r"""|(?:(?:const|let|var)\s+(?:[\w$]+|\{[\w$\s,:]*\})\s*=\s*)?require\s*\(\s*)"""
    r"""(['"])[^'"]+\1\s*\)?\s*;?$""")

#: The literal types a header constant may hold.
_HEADER_LITERALS = (str, bytes, int, float, complex, bool, type(None))


def _header_value(node, loggers):
    """Whether `node`, the right-hand side of an assignment inserted into a
    module header, is one of the shapes a header fix needs. The allowlist,
    and the fix each entry exists for:

    - a literal — str, bytes, number (optionally negated), bool, None — or a
      tuple or list of them: a named constant (`TIMEOUT = 30`, `__all__ =
      [...]`) is a header's other ordinary content, and a literal runs
      nothing;
    - `<logging>.getLogger()`, with no argument, `__name__` or one string
      literal, where `<logging>` is a name the module (old or inserted) binds
      to the standard `logging` module: the bundled no-print rule's own fix,
      "logger = logging.getLogger(__name__) at module level"
      (examples/python/no-print.yaml).

    Everything else — any other call, attribute chains, lambdas,
    comprehensions, f-strings, walrus, names — is refused here, so such a
    line is held to the window rule like any other change."""
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_header_literal(element) for element in node.elts)
    if _header_literal(node):
        return True
    if not (isinstance(node, ast.Call) and not node.keywords and len(node.args) <= 1
            and isinstance(node.func, ast.Attribute) and node.func.attr == "getLogger"
            and isinstance(node.func.value, ast.Name) and node.func.value.id in loggers):
        return False
    if not node.args:
        return True
    arg = node.args[0]
    return ((isinstance(arg, ast.Name) and arg.id == "__name__")
            or (isinstance(arg, ast.Constant) and isinstance(arg.value, str)))


def _header_literal(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
        return isinstance(node, ast.Constant) and type(node.value) in (int, float, complex)
    return isinstance(node, ast.Constant) and type(node.value) in _HEADER_LITERALS


def _dotted_name(node):
    """`int`, `logging.Logger`: a name or an attribute of a name, nothing else."""
    if isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _logging_aliases(statements):
    """The names the `import logging [as x]` among `statements` (a module's
    top-level statements) bind to the standard logging module."""
    return {alias.asname or "logging"
            for node in statements if isinstance(node, ast.Import)
            for alias in node.names if alias.name == "logging"}


def _module_bindings(tree):
    """(names bound at the module's top level, ast dumps of its top-level
    imports). Bindings under a top-level `if`/`try` count: they may run."""
    bound, imports = set(), set()
    if tree is None:
        return bound, imports
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.add(ast.dump(node))
            bound.update((a.asname or a.name).split(".", 1)[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                bound.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.If, ast.Try)) or type(node).__name__ == "TryStar":
            pending.extend(node.body)
            pending.extend(getattr(node, "orelse", ()))
            pending.extend(getattr(node, "finalbody", ()))
            for handler in getattr(node, "handlers", ()):
                pending.extend(handler.body)
    return bound, imports


# --- signatures --------------------------------------------------------------------

def _python_definitions(tree):
    """Qualified name -> (signatures, span) for every function and class, at
    any depth, including those under a module-level `if`/`try`. Signatures are
    a tuple because one name may be defined more than once (an `@overload`
    set, a property and its setter); line numbers are not part of them."""
    found = {}

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                signature = (type(node).__name__, ast.dump(node.args),
                             ast.dump(node.returns) if node.returns else None,
                             tuple(ast.dump(d) for d in node.decorator_list))
                _add(found, name, signature, node)
                visit(node.body, name + ".")
            elif isinstance(node, ast.ClassDef):
                name = prefix + node.name
                signature = ("ClassDef", tuple(ast.dump(b) for b in node.bases),
                             tuple(ast.dump(k) for k in node.keywords),
                             tuple(ast.dump(d) for d in node.decorator_list))
                _add(found, name, signature, node)
                visit(node.body, name + ".")
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.AsyncWith,
                                   ast.AsyncFor)):
                visit(node.body, prefix)
                visit(node.orelse if hasattr(node, "orelse") else (), prefix)
            elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
                visit(node.body, prefix)
                for handler in node.handlers:
                    visit(handler.body, prefix)
                visit(node.orelse, prefix)
                visit(node.finalbody, prefix)

    visit(tree.body, "")
    return found


def _add(found, name, signature, node):
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    end = getattr(node, "end_lineno", None) or node.lineno
    signatures, span = found.get(name, ((), (start, end)))
    found[name] = ((*signatures, signature), (min(span[0], start), max(span[1], end)))


def _python_signature_breaches(path, old_tree, new_tree, permitted_spans, hunks=()):
    before, after = _python_definitions(old_tree), _python_definitions(new_tree)
    # An added definition exists only in the fixed file, so its span is in
    # the fixed file's lines; the permitted spans are the base commit's, and
    # are carried through the hunks before the two are compared. WHY: ten
    # inserted header lines otherwise let a definition added ten lines away
    # from a permitting violation pass, and refused one added right at it.
    shifted = [(_to_new_line(start, hunks), _to_new_line(end, hunks, end=True))
               for start, end in permitted_spans]
    found = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is not None and new is not None and old[0] == new[0]:
            continue
        span, allowed_spans = (old[1], permitted_spans) if old else (new[1], shifted)
        if any(overlaps(span, allowed) for allowed in allowed_spans):
            continue
        if new is None:
            what, line, side = "removed", old[1][0], "base"
        elif old is None:
            what, line, side = "added", new[1][0], "fixed"
        else:
            what, line, side = "changed its signature", old[1][0], "base"
        found.append(Breach(
            SIGNATURE_CHANGE, path, line,
            f"`{name}` {what} ({side} line {line}); no principle with a violation "
            f"there permits signature changes"))
    return found


def _to_new_line(line, hunks, end=False):
    """Base line `line`'s number in the fixed file, through `hunks`. A line
    inside a replaced region maps to that region's first line in the fixed
    file, or with `end` to its last. WHY: a span's end is mapped with `end`,
    so a span that reaches into a replaced region covers all of what replaced
    it; mapped to the first line, a six-line replacement of a span's last
    three lines would shrink the span to the replacement's first line."""
    delta = 0
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        if hunk.old_count == 0:
            if line > hunk.old_start:
                delta += hunk.new_count
        elif line > hunk.old_start + hunk.old_count - 1:
            delta += hunk.new_count - hunk.old_count
        elif line >= hunk.old_start:
            return hunk.new_start + (max(hunk.new_count, 1) - 1 if end else 0)
    return line + delta


def _script_signature_breaches(path, hunks, old_lines, new_lines, permitted_spans):
    found = []
    for hunk in hunks:
        if any(overlaps(_hunk_range(hunk), allowed) for allowed in permitted_spans):
            continue
        removed = old_lines[hunk.old_start - 1:hunk.old_start - 1 + hunk.old_count]
        added = new_lines[hunk.new_start - 1:hunk.new_start - 1 + hunk.new_count]
        declared = [line for line in removed + added if DECLARATION.match(line)]
        if declared:
            found.append(Breach(
                SIGNATURE_CHANGE, path, max(hunk.old_start, 1),
                f"a declaration line changed near base line {max(hunk.old_start, 1)} "
                f"({declared[0].strip()[:80]!r}); JS/TS declarations are matched by "
                "line shape, and no principle with a violation there permits it"))
    return found


# --- imports ------------------------------------------------------------------------

def _python_imports(tree):
    """Top-level module name -> first line importing it, for absolute imports
    anywhere in the module (function-local ones too), and for the dynamic
    forms whose module is a string literal: `__import__("x")`,
    `importlib.import_module("x")` and a bare `import_module("x")`. A
    dynamic import of a computed name is not seen (Limits)."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif _dynamic_import(node):
            names = [node.args[0].value]
        else:
            continue
        for name in names:
            top = name.split(".", 1)[0]
            if top not in found or node.lineno < found[top]:
                found[top] = node.lineno
    return found


def _dynamic_import(node):
    """Whether `node` is `__import__`/`import_module` called on an absolute
    module name spelled as a string literal. Any `x.import_module` counts:
    over-matching a look-alike only reports a module the repo does not use."""
    if not (isinstance(node, ast.Call) and node.args):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)
    arg = node.args[0]
    return (name in ("__import__", "import_module") and isinstance(arg, ast.Constant)
            and isinstance(arg.value, str) and bool(arg.value)
            and not arg.value.startswith("."))


def script_package(specifier):
    """The package a JS/TS import specifier names, or None for a relative or
    absolute path. `@scope/name/sub` is `@scope/name`; `node:fs` is `fs`."""
    if specifier.startswith((".", "/")):
        return None
    if specifier.startswith("node:"):
        return specifier[5:].split("/", 1)[0]
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]


def _script_specifiers(line):
    return [quoted or template for quoted, template in _SCRIPT_IMPORT.findall(line)]


def _script_imports(lines):
    found = {}
    for number, line in enumerate(lines, start=1):
        for specifier in _script_specifiers(line):
            package = script_package(specifier)
            if package and package not in found:
                found[package] = number
    return found


def _computed_loads(lines):
    """(line number, stripped line) for each line with a `require(` or
    `import(` whose argument is not one literal, in file order."""
    return [(number, line.strip()) for number, line in enumerate(lines, start=1)
            if any(not _SCRIPT_LITERAL_ARGUMENT.match(line, call.end())
                   for call in _SCRIPT_LOADER_CALL.finditer(line))]


def _computed_load_breaches(path, old_lines, new_lines):
    """A NEW_DEPENDENCY Breach for each computed load the fix added.

    WHY the stripped text is the key: what a computed load names cannot be
    known, so the only safe answer is "this exact line was already in the
    base" — moved or re-indented, not written. WHY counted, not looked up:
    a second copy of an old `require(name)` pasted elsewhere, with `name`
    rebound above it, loads something new while matching an old line. So
    the fixed file may hold each text as often as the base does; each
    occurrence beyond that, in file order, is reported. Editing a line that
    holds a computed load changes its text and is refused too: that fails
    closed, at the cost of a fix that only touched the rest of that line."""
    allowed = collections.Counter(text for _, text in _computed_loads(old_lines))
    found = []
    for number, text in _computed_loads(new_lines):
        if allowed[text]:
            allowed[text] -= 1
            continue
        found.append(Breach(
            NEW_DEPENDENCY, path, number,
            f"loads a module named by something other than one literal (fixed line "
            f"{number}), so whether it is a new dependency cannot be checked"))
    return found


def _new_import_breaches(path, before, after, known_modules, builtins):
    found = []
    for name in sorted(set(after) - set(before)):
        if name in builtins or name in known_modules:
            continue
        found.append(Breach(
            NEW_DEPENDENCY, path, after[name],
            f"imports `{name}` (fixed line {after[name]}), which is not in the "
            "standard library, not part of this repository and not imported "
            "anywhere in it at the base commit"))
    return found


# --- the git adapter ---------------------------------------------------------------
# Every `repo` parameter in this section is a sandbox's path (Sandbox.path),
# never the user's checkout: lint_sandbox and verify_fix, the entry points,
# require a Sandbox and pass its path down.

def lint_sandbox(sandbox, **lint_options):
    """Run the lint (sweep.work_list, with `lint_options`) in `sandbox` while
    its work tree is still exactly its base commit, record the result as
    `sandbox.work_list` for verify_fix, and return it. Record too, as
    `sandbox.rules`, the rules the lint ran (rules.load_rules on the same
    pristine tree): which of them carry a `remedy` is the base's answer for
    remedies.apply_file_remedies and verify_fix, never the edited tree's.

    WHY here and not by the caller: the verdict's line numbers and its "only
    files with violations" are the work list's, so the work list must be the
    base's. A list computed in the user's checkout (another commit, or
    uncommitted edits) would let a fix pass for violations the base never
    had. ScopeCheckError if the tree is not the base (anything modified,
    staged or untracked, ignored or not); sweep.LintUntrusted if the lint's
    report cannot be acted on."""
    require_sandbox(sandbox, ScopeCheckError)
    require_pristine(sandbox)
    result = work_list(sandbox.path, **lint_options)
    try:
        rules = tuple(load_rules(sandbox.path))
    except RuleError as error:
        raise ScopeCheckError(f"the rules at the base commit cannot be loaded: {error}") \
            from error
    require_pristine(sandbox)
    sandbox.work_list = result
    sandbox.rules = rules
    return result


def require_pristine(sandbox):
    """ScopeCheckError unless `sandbox`'s work tree is exactly its base
    commit: nothing modified, staged or untracked, ignored or not."""
    status = git(sandbox.path, "status", "--porcelain", "-z", "--ignored",
                 "--untracked-files=all", "--ignore-submodules=all")
    if status or sandbox.head() != sandbox.base:
        raise ScopeCheckError(
            f"the sandbox at {sandbox.path} is not the base commit {sandbox.base} as "
            "checked out, so a work list computed in it would not be the base's")


def base_principles(sandbox):
    """The principles file at `sandbox`'s base commit (an empty one if it has
    none), read from git's objects. WHY never the work tree: the fix agent
    can edit `.curamoir/principles.md` in the sandbox, and a `permits:
    signature_change` it wrote there would otherwise license its own
    signature change. ScopeCheckError if the base's file does not parse."""
    require_sandbox(sandbox, ScopeCheckError)
    data = sandbox.show(".curamoir/principles.md")
    if data is None:
        return PrinciplesFile()
    try:
        return parse_principles(data.decode("utf-8"))
    except (UnicodeDecodeError, PrincipleError) as error:
        raise ScopeCheckError(
            f"the principles at the base commit cannot be read, so which principles "
            f"permit signature changes is unknown: {error}") from error


def verify_fix(sandbox, *, git_state, window=WINDOW):
    """E3-R2's review step: the verdict on everything that changed between
    `sandbox`'s base commit and its work tree (staged, unstaged and
    untracked, ignored or not), against the work list lint_sandbox recorded
    and the principles at the base commit (base_principles). There is no
    work-list or principles parameter: both are the base's, by construction.

    `git_state` is git_guard.snapshot_git_state(sandbox), taken before the
    fix step ran (fix_step.run_fix_step takes it: FixStepRun.git_state). It
    is compared FIRST, by reading files, and a difference in the sandbox's
    git files or any `.gitattributes` is the whole verdict: WHY — `.git` is
    invisible to `git diff`, and a planted fsmonitor or filter driver would
    run on the very git command that asked what changed. It is required and
    the check fails closed: no snapshot, a snapshot of another sandbox or
    base, `sandbox` not a git_guard.Sandbox, or no work list raises
    ScopeCheckError before any git command runs.

    Then: a HEAD that left the base is BASE_MOVED, and the whole verdict.
    Every path the snapshot's work-tree manifest saw change but git does not
    report is HIDDEN_CHANGE, alongside the core's breaches."""
    if git_state is None:
        raise ScopeCheckError("no git-state snapshot taken before the fix step; refusing "
                              "to run git on the edited tree")
    require_sandbox(sandbox, ScopeCheckError)
    if os.path.realpath(git_state.path) != os.path.realpath(str(sandbox.path)):
        raise ScopeCheckError(f"the git-state snapshot is of {git_state.path}, not "
                              f"{sandbox.path}")
    if git_state.base != sandbox.base:
        raise ScopeCheckError(f"the git-state snapshot was taken for base {git_state.base}, "
                              f"not {sandbox.base}")
    if sandbox.work_list is None or sandbox.rules is None:
        raise ScopeCheckError("no work list or rules were linted in this sandbox at its "
                              "base (fix_scope.lint_sandbox), so there is nothing to hold "
                              "the fix to")
    changed = verify_git_state(git_state)
    if changed:
        return ScopeVerdict(breaches=tuple(
            Breach(GIT_INTERNALS_CHANGED, c.label, None,
                   f"{c.change} while the fix step ran; {c.reason}, so nothing more of "
                   "this tree is read")
            for c in changed), paths=())
    base = sandbox.base
    head = sandbox.head()
    if head != base:
        return ScopeVerdict(breaches=(Breach(
            BASE_MOVED, "HEAD", None,
            f"names {head}, not the base {base} the sandbox was checked out at; the fix "
            "step edits files and never moves HEAD, so nothing more of this tree is "
            "read"),), paths=())
    changes = changes_since(sandbox.path, base)
    seen = {nfc(change.path) for change in changes}
    hidden = sorted(({nfc(path) for path in tree_changes(git_state)}
                     | set(_missing_submodules(sandbox.path, base))) - seen)
    permitting = frozenset(p.id for p in base_principles(sandbox).principles
                           if SIGNATURE_PERMISSION in p.permits)
    # The tier-1 findings the runner accepted (curamoir.tier1) are violations
    # too: each is a whole-line span at the base, validated before any edit.
    violations = (*sandbox.work_list.items, *sandbox.work_list.tier1)
    verdict = check_scope(changes, violations, permitting=permitting,
                          known_modules=base_known_modules(sandbox.path, base), window=window,
                          deletable_rules=rules_licensing_deletion(sandbox.rules))
    if not hidden:
        return verdict
    return ScopeVerdict(
        breaches=verdict.breaches + tuple(Breach(
            HIDDEN_CHANGE, path, None,
            "changed on disk while the fix step ran, but git does not report it (inside "
            "an uninitialised submodule, or hidden another way); a change git cannot see "
            "can be neither reviewed nor carried to a PR") for path in hidden),
        paths=tuple(sorted(set(verdict.paths) | set(hidden))))


def rules_licensing_deletion(rules):
    """The ids of `rules` whose file-level violation licenses deleting the
    file: those with `remedy: delete_file` (rules.REMEDIES)."""
    return frozenset(rule.id for rule in rules if rule.remedy == DELETE_FILE)


def _missing_submodules(repo, base):
    """The base's submodule paths (gitlinks) that are no longer an empty-or-
    not directory in the work tree. WHY: git is told never to look inside a
    submodule (git_guard), so `git diff` no longer reports one removed or
    replaced by a file, and the work-tree manifest records files, not empty
    directories. What is put inside one is the manifest's to see."""
    missing = []
    for entry in git(repo, "ls-tree", "-r", "-z", "--full-tree", base).split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if meta.startswith(b"160000 "):
            path = name.decode("utf-8", "surrogateescape")
            target = Path(repo) / path
            if target.is_symlink() or not target.is_dir():
                missing.append(nfc(path))
    return missing


def changes_since(repo, base):
    """Every path that differs between commit `base` and `repo`'s working
    tree, as FileChanges with git's own `-U0` hunks. Untracked files are
    listed with no exclude rules: in a sandbox, a fresh clone holds no
    ignored file, so any untracked file, ignored or not, is new."""
    repo = Path(repo)
    raw = git(repo, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff",
              "--ignore-submodules=all", base, "--")
    changes = []
    fields = raw.split(b"\0")
    index = 0
    while index + 1 < len(fields) and fields[index].startswith(b":"):
        meta = fields[index][1:].decode("ascii").split()
        path = fields[index + 1].decode("utf-8", "surrogateescape")
        index += 2
        changes.append(_tracked_change(repo, base, path, meta))
    untracked = git(repo, "ls-files", "--others", "-z")
    for entry in untracked.split(b"\0"):
        if entry:
            path = entry.decode("utf-8", "surrogateescape")
            changes.append(FileChange(path, ADDED, None, _read_text(repo / path)[0]))
    return tuple(changes)


def _tracked_change(repo, base, path, meta):
    old_mode, new_mode, status = meta[0], meta[1], meta[4]
    kind = {"A": ADDED, "D": DELETED}.get(status[0], MODIFIED)
    if kind == ADDED:
        return FileChange(path, ADDED, None, _read_text(repo / path)[0])
    if kind == DELETED:
        return FileChange(path, DELETED, _blob_text(repo, base, path)[0], None)
    unusual = {"120000": "a symlink", "160000": "a submodule"}
    for mode in (old_mode, new_mode):
        if mode in unusual:
            return FileChange(path, MODIFIED, unverifiable=(
                f"{unusual[mode]} changed; only regular text files can be checked"))
    if old_mode != new_mode:
        return FileChange(path, MODIFIED, unverifiable=(
            f"its mode changed ({old_mode} to {new_mode}); a fix edits text, not modes"))
    old, old_problem = _blob_text(repo, base, path)
    new, new_problem = _read_text(repo / path)
    if old_problem or new_problem:
        return FileChange(path, MODIFIED, unverifiable=old_problem or new_problem)
    # --text: a committed `-diff` or `binary` attribute would otherwise print
    # "Binary files differ" and no hunks, and no hunks pass the window rule.
    # The content is UTF-8 without NULs, checked just above.
    diff = git(repo, "diff", "-U0", "--text", "--no-color", "--no-ext-diff", "--no-textconv",
               "--no-renames", base, "--", f":(literal){path}")
    return FileChange(path, MODIFIED, old, new,
                      hunks=parse_hunks(diff.decode("utf-8", "surrogateescape")))


def _decode(data, where):
    if b"\0" in data:
        return None, f"{where} is binary; only text files can be checked"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as error:
        return None, f"{where} is not UTF-8 ({error}); only text files can be checked"


def _blob_text(repo, base, path):
    return _decode(git(repo, "cat-file", "blob", f"{base}:{path}"), "the base version")


def _read_text(path):
    try:
        return _decode(Path(path).read_bytes(), "the fixed version")
    except OSError as error:
        return None, f"the fixed version cannot be read: {error}"


_PY_IMPORT_LINE = re.compile(r"^\s*(?:import\s+([\w., ]+)|from\s+(\w[\w.]*)\s+import\b)")


def base_known_modules(repo, base):
    """Top-level module and package names that are not a new dependency at
    commit `base`: every Python file's module name and every directory
    holding Python or JS/TS source (a local module or package, wherever it
    sits), every module a Python file imports, every package a JS/TS file
    imports, and package.json's declared packages.

    Only source counts as local: WHY — `docs/pandas.md` is not a module named
    pandas, and counting every file stem let `import pandas` pass as local.
    The grep patterns use POSIX classes (`[[:space:]]`), not `\\s`: Apple's
    git 2.39 does not know `\\s` in `-E`, matched nothing, and every import
    in the repository looked new on macOS."""
    known = set()
    listing = git(repo, "ls-tree", "-r", "-z", "--name-only", base)
    for entry in listing.split(b"\0"):
        parts = [p for p in entry.decode("utf-8", "surrogateescape").split("/") if p]
        if not parts or not parts[-1].endswith(PYTHON_SUFFIXES + SCRIPT_SUFFIXES):
            continue
        if parts[-1].endswith(PYTHON_SUFFIXES):
            known.add(parts[-1].split(".", 1)[0])
        known.update(parts[:-1])
    python = git(repo, "grep", "-h", "-I", "-E", r"^[[:space:]]*(import|from)[[:space:]]",
                 base, "--", "*.py", "*.pyi", ok=(0, 1))
    for line in python.decode("utf-8", "surrogateescape").splitlines():
        match = _PY_IMPORT_LINE.match(line)
        if not match:
            continue
        names = match.group(1).split(",") if match.group(1) else [match.group(2)]
        for name in names:
            name = name.strip().split(" ", 1)[0]
            if name:
                known.add(name.split(".", 1)[0])
    scripts = git(repo, "grep", "-h", "-I", "-E",
                  r"(from|import|require)[[:space:]]*\(?[[:space:]]*['\"`]",
                  base, "--", *(f"*{suffix}" for suffix in SCRIPT_SUFFIXES), ok=(0, 1))
    for line in scripts.decode("utf-8", "surrogateescape").splitlines():
        for specifier in _script_specifiers(line):
            package = script_package(specifier)
            if package:
                known.add(package)
    known.update(_declared_packages(repo, base))
    return frozenset(known)


def _declared_packages(repo, base):
    raw = git(repo, "cat-file", "-p", f"{base}:package.json", ok=(0, 128))
    try:
        manifest = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return ()
    if not isinstance(manifest, dict):
        return ()
    names = []
    for table in ("dependencies", "devDependencies", "peerDependencies",
                  "optionalDependencies"):
        if isinstance(manifest.get(table), dict):
            names.extend(manifest[table])
    return names
