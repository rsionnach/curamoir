# Platform-first runners: Claude Code + GitHub Actions now, other providers later

**Status:** accepted, 2026-10-29. **Supersedes:** the local-scheduler assumption in README "Current status" and the `/curamoir:schedule` row. **Epic:** E14 (`cm-2qp`). Linked from README "Current status" (E14.7).

## 1. Decision in one paragraph

cúramóir's scheduled jobs run on a hosted, disposable runner by default: a Claude Code Routine or a GitHub Actions workflow. On those runners the virtual machine is the security boundary, so the OS sandbox (`os_sandbox.py`, `os_sandbox.sb`) and the laptop-sharing machinery around it are scoped to one adapter, `local`, and stop being hardened. Nothing in the boundary layer is deleted: the local adapter is the documented fallback for a developer without a hosted runner, and the hardened git wrapper matters more on a hosted runner than on a laptop (§4). Other providers (Codex, Cursor) are reached through two seams that exist today and get named, documented and tested, not through adapters built now.

## 2. What changed

- Claude Code Routines (April 2026, research preview) run each job in a fresh Anthropic cloud VM: clone from the default branch, `CLAUDE.md`, skills, agents and hooks loaded, cron at one hour or longer, GitHub-event and API triggers, PRs to a `claude/*` branch, no local filesystem, no memory between runs, subscription quota.
- Cursor Automations run in a Cursor cloud VM with cron, SCM, Slack, webhook, Linear, Sentry and PagerDuty triggers, never pause for approval, and bill per run. Codex Automations run locally through the Codex app and need the laptop awake.
- cúramóir's runner was designed for exactly this: `curamoir run <job>` is "the one entry point schedulers call" (`runner.py`), and a CI workflow was always a named caller. What the last three days built (`cm-p2f` rounds 2–7, command-guard rounds 10–13, command-policy rounds 2–5) is a boundary for the one runner the platform no longer requires: the developer's own machine.

Of 164 commits, 60 are guard, policy or sandbox work; of the 53 since 27 October, 37 are. The boundary modules are about 10.4k lines against 11.6k for everything the plugin is for, and the features the README calls "most of the reason to want this" (review, grades, promote) are still planned.

## 3. Runner kinds and where the boundary is

| | `hosted` (Routine, GitHub Actions) | `local` (cron, launchd, by hand) |
|---|---|---|
| Process boundary | the VM; discarded after the run | `os_sandbox` (`sandbox-exec` / `bwrap`), self-tested, fails closed |
| Developer's checkout | unreachable | protected by the throwaway clone + `fix_scope` |
| Credentials at risk | the run's token (`GITHUB_TOKEN`, the Claude GitHub App token) | the developer's whole home directory |
| Hostile repo content | `git_guard` (no hooks, filters, diff drivers; planted `.git`/`.gitattributes` refuses) | same |
| Agent tools | Read, Grep, Glob, Edit; Bash permitted (D4) | Read, Grep, Glob, Edit; no shell (`cm-xq9` stands) |
| Supervisor lock | taken and never stale (fresh VM) | E0-R2 as built |
| Kill switch | `curamoir:paused` label (unchanged) | label or `.curamoir/PAUSE` |
| Spend | subscription quota / per-run billing; ledger records tokens | ledger prices tokens; monthly ceiling |
| Memory between runs | none: the in-repo handoff and ledger are the only continuity | the checkout |

The runner kind is declared, never inferred: `CURAMOIR_RUNNER=hosted` is set by the workflow template and the Routine prompt and by nothing else. Absent, the runner is `local` and every guard runs as today. `hosted` is accepted only with a hosted marker: `CLAUDE_CODE_REMOTE=true` (set in every Claude Code cloud session, which is where a Routine runs), or `GITHUB_ACTIONS=true` together with `RUNNER_ENVIRONMENT=github-hosted` (a self-hosted runner or `act` is not a disposable VM, so it runs as `local`). A `hosted` claim on a machine that is not one, or any other value, is refused, not downgraded, and the refusal never echoes the value.

## 4. What is not rolled back, and why

- **`git_guard`.** A planted hook in the clone runs with the run's credentials. On a laptop that is the developer's keychain; on a GitHub runner it is `GITHUB_TOKEN` with write access to the repo. The hosted runner raises the stakes. Keep as built; no change.
- **`fix_scope` and the throwaway clone (E3-R2).** "Only the reviewed patch of the files in scope leaves the clone" is the sweep's semantics, not a laptop concern. The 28 October dogfood refusal was this rule working. Keep.
- **`job_guard`'s order** (kill switch, lock, budget, PR cap, sync seam, job, ledger line, release). Platform-neutral. Keep; the lock becomes trivially satisfied on `hosted`.
- **The PreToolUse command guard** (`hooks/policy.py`, `hooks/destructive.py`). It runs in interactive sessions too and catches honest mistakes. Keep; freeze (§6).
- **`os_sandbox`.** Keep, scoped to `local`. It is the fallback if Routines stay preview or are withdrawn, and the only honest answer for a developer on a laptop. Not hardened further.

Deleting tested boundary code saves nothing; the cost of this code is the hardening cadence, not its existence. What is removed is the plan to keep hardening it, and one planned feature (§5, R1).

## 5. Short-term scope: ship (E14)

| R | Requirement | Touches |
|---|---|---|
| R1 | `/curamoir:schedule` (local cron/launchd writer) is removed from the roadmap. The row leaves the README; no bead builds it. | README |
| R2 | `CURAMOIR_RUNNER` is read by `config`/`job_guard`; `hosted` is accepted only with a hosted marker present (`CLAUDE_CODE_REMOTE=true`, or `GITHUB_ACTIONS=true` with `RUNNER_ENVIRONMENT=github-hosted`); otherwise, or for any other value, refused with the reason on the job line, never echoing the value. | `config.py`, `job_guard.py`, tests |
| R3 | `green_gate` on `hosted` runs the test command directly in the clone; `os_sandbox.wrap` and `self_test` are bypassed only on `hosted`. On `local`, unchanged: no unsandboxed fallback. | `green_gate.py` (the `OS_SANDBOX` seam), tests |
| R4 | `fix_step` on `hosted` permits Bash in the tool list, approved only for the base's test command, with git denied and the run's GitHub/Actions tokens stripped from the fix step's environment (`cm-2qp.12`); `--restricted` and the `--settings` carrying the PreToolUse denylist are kept on both runners (user decisions 2026-10-29: the clone's own project settings and hooks never load; `cm-60c` showed `rm -rf` runs without the denylist). The tier-1 scan stays read-only, its command line the same on both. There is no fresh `HOME` to drop: the fix step never had one. `cm-xq9` ("no shell without an OS sandbox or a hosted runner") is satisfied by the VM. | `fix_step.py`, tests |
| R5 | `templates/github/curamoir-sweep.yml`: a workflow with `schedule:` (cron) and `workflow_dispatch`, checkout, `uv`, `curamoir run sweep`, `CURAMOIR_RUNNER=hosted`, secrets documented (`ANTHROPIC_API_KEY` or the subscription OAuth token where the terms permit unattended use). `/curamoir:init` offers to write it. | `templates/`, `scaffold.py`, `commands/init.md` |
| R6 | `/curamoir:routine`: prints the Routine prompt for `sweep` (and later `brief`, `review`) and the `/schedule` steps, in place of the removed `/curamoir:schedule`. The prompt sets `CURAMOIR_RUNNER=hosted` and runs `curamoir run sweep`. | `commands/routine.md` |
| R7 | **Dogfood acceptance:** the sweep opens a PR on `rsionnach/curamoir` from (a) a GitHub Actions run and (b) a Routine, with ledger lines committed by the run. Until both have happened, E14 is not done. The constraints this will surface are recorded as beads, not fixed in the spec: `bd` absent in the VM (GitHub-only filing, already designed), the default environment's package allowlist blocking network-using tests, hooks loading only for single-repo Routines. | `.curamoir/handoff/`, beads |
| R8 | README "Current status" and "Guardrails" are rewritten against §3: the boundary table replaces the "Known limits" prose that describes laptop-only problems (stale locks, `CURAMOIR_JOB` leaking into the developer's sessions). | README |

Out of scope for E14: review (Epic 5, `cm-8ap`), grades (`cm-16x.1`), promote/demote. They follow E14 and are the next three epics; nothing in E14 may grow to include them.

## 6. What stops

- `cm-p2f` (grant walk, round 7 in flight): close at the current commit as `local`-only; no further rounds.
- `cm-uxl` (signals leave stale locks/orphans), `cm-6xd` (test suite leaks sandboxes): park, tagged `local-adapter`. Neither exists on `hosted`.
- Command-guard and command-policy review rounds: no further rounds unless a dogfood run shows a mistake the guard missed. Eighteen rounds on a component the README calls "deliberately not the security boundary" is the regenerating review queue, and it stops here.
- Dollar-precise pricing (`cm-1et`, `cm-2ui`): close `cm-1et` with the finding (1-hour cache writes reconcile at 2×). The ledger keeps recording tokens; the monthly ceiling stays for `local`; no further pricing work.

## 7. Later: other providers

Two seams, named now, built on by contributors later. Neither is E14 work.

**Seam A, the scheduler.** Already exists: `curamoir run <job>`. Any scheduler that can run a command in a clone with `CURAMOIR_RUNNER=hosted` is supported. A Cursor Automation is a shell step; a Codex Automation (local, laptop awake) is the `local` adapter. Documented in one page, `docs/runners.md`, with the GitHub Actions workflow as the worked example.

**Seam B, the agent runner.** `fix_step.run_fix_step(sandbox, prompt, model, …) -> FixStepRun` is the only model-specific call in the sweep (the r5 review will be the second). Its contract: given the clone, a prompt and a work list, return a diff and token counts; scope (`fix_scope`), the green gate and publish stay cúramóir's. A `codex exec` or Cursor CLI runner implements the same contract. Not built now; the contract is written into `fix_step`'s docstring and one test pins it.

**Content portability** needs no work: `principles.md`, the rules and `curamoir-lint` are plain files; `AGENTS.md` and skills are shared by Codex; only the PreToolUse hooks are Claude Code-specific, and Cursor has hooks for a port when someone wants one.

**Billing note.** A GitHub Actions run needs an API key (per-token) unless the Claude Code GitHub Action's subscription OAuth token is permitted for unattended use under current terms. Check before documenting it as the default; otherwise document the key and its cost.

## 8. Acceptance for E14

1. `CURAMOIR_RUNNER=hosted curamoir run sweep` in a GitHub Actions run on `rsionnach/curamoir` opens one PR, or records a refusal with a reason, and commits its ledger line.
2. The same from a Routine.
3. `curamoir run sweep` with no runner variable on a laptop behaves exactly as at commit `3f50a3f` (sandbox required, no shell).
4. `CURAMOIR_RUNNER=hosted` on a machine with no hosted marker is refused.
5. README "Current status" no longer says "nothing is scheduled for you yet".

---

## Appendix A. Bead review (proposed; reconcile against `bd list` before applying)

The live beads are in Dolt, not in git, so this table is built from the IDs the source, handoffs and commits reference. Anything in `bd list` not here is unaffected.

| Bead | What it is | Action | Why |
|---|---|---|---|
| `cm-p2f` | OS sandbox grant walk (rounds 2–7) | **close** at `3f50a3f`, label `local-adapter` | boundary on `hosted` is the VM |
| `cm-uxl` | signals leave stale locks/orphans | **park**, label `local-adapter` | no stale lock on a fresh VM |
| `cm-6xd` | test suite leaks sandboxes | **park**, label `local-adapter` | same |
| `cm-xq9` | no job agent gets a shell without an OS sandbox | **rescope**: "…without an OS sandbox or a hosted runner" | R4 |
| `cm-3b2` | prove the boundary: nothing planted in the clone runs | **keep closed**; cite in §4 | holds on both runners |
| `cm-9ab` | drop and re-gate on scope breach | **keep**; done | platform-neutral semantics |
| `cm-t3a` | `sweep.exclude` | **keep**; done | neutral |
| `cm-1et` | cache-write pricing 2× | **close** with the finding; no further pricing work | §6 |
| `cm-2ui` | cache tokens priced in the ceiling | **keep closed**; no follow-ups | §6 |
| `cm-0ik` | package installs naming a package (command policy) | **park** unless a dogfood run hits it | guard freeze |
| `cm-mox` | waits on the dogfood sweep | **re-point** at R7 | dogfood moves to hosted |
| `cm-hve`, `cm-z0k`, `cm-f41`, `cm-hhr` | command-policy hardening rounds | **keep closed**; **no new children** | guard freeze |
| `cm-60c`, `cm-854`, `cm-kry` | `claude -p` isolation: `--restricted`, hidden `HOME`, PyYAML interpreter | **keep**; becomes the `local` path of R4 | hosted keeps `--restricted` and the denylist `--settings` (user decisions 2026-10-29); there is no hidden HOME on either |
| `cm-x5a.1` | runner: review/smoke/sync/grade jobs registered | **keep**; after E14 | — |
| `cm-8ap` (Epic 5) | r5 review job | **keep**; first epic after E14 | the product |
| `cm-16x.1` | grades | **keep**; after E14 | the product |
| `cm-eqp.3` | sync seam (beads ↔ GitHub) | **keep**; note `bd` absent on hosted, GitHub-only fallback is the hosted default | R7 finding |
| `cm-5f3.1` | smoke job | **keep**; low | — |
| `cm-ydl` | brief reads sweep PRs | **keep** | neutral |
| *(new)* | `/curamoir:schedule` | **do not file**; row removed | R1 |
| *(new)* E14.1 | `CURAMOIR_RUNNER` + hosted marker check | **file** | R2 |
| *(new)* E14.2 | `green_gate` hosted path | **file** | R3 |
| *(new)* E14.3 | `fix_step` hosted path; Bash on hosted | **file** | R4 |
| *(new)* E14.4 | GitHub Actions workflow template + init | **file** | R5 |
| *(new)* E14.5 | `/curamoir:routine` | **file** | R6 |
| *(new)* E14.6 | dogfood: PR from Actions and from a Routine | **file**, blocks E14 close | R7 |
| *(new)* E14.7 | README rewrite against §3 | **file** | R8 |
| *(new)* E14.8 | `docs/runners.md` + `run_fix_step` contract pinned | **file**, low | §7 |

Labels to create: `local-adapter` (parked, local-only), `guard-freeze` (closed, no children without a dogfood finding).
