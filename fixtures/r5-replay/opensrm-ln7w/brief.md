PASS 1 (CORRECTNESS) re-run, fix iteration 1, sequential R5 review on bead opensrm-ln7w (OpenSRM v2 Addendum A1 revision — a SPECIFICATION change). Read-only: do not modify any files.

Template: read and follow ~/.claude/skills/rule-of-five-review/correctness-reviewer.md. For a spec, correctness = wrong or self-contradictory normative statements, contradictions with core spec/schema, broken cross-references (§, rule ids, erratum numbers), errata whose "Current:" text doesn't match the base file or that are described but not applied as described, and examples violating rules the change introduces.

Scope is THE DIFF: git -C <workspace>/opensrm-ln7w diff 859424a..d0048a5
(The new addendum file is entirely in scope.) Pre-change: `git -C <path> show 859424a:<file>`.

Rulings by Rob — implement, don't relitigate: M1 metric prefix nthlayer_; M2 calibration from authoritative outcomes only; M3 A2's not-evaluable rule lives in core §5.5.6 E3, A1 cites it; M4 §9 errata applied in this bead.

The previous iteration found 4 IMPORTANT + 4 MINOR, fixed in commit d0048a5 (see `git show d0048a5`):
 1. A2 "computable only from authoritative records" contradicted grader admissibility for calibration_sample / audit_sampling → A2 and the Authoritative row now say denominators are decision records only and graders feed a numerator only where the Grader row admits them.
 2. §4.4 nthlayer.decision.id Required vs conditional; Claude profile's outcome_evaluation can't carry it → now Conditionally required (always for opensrm.audit); profile says how to set it.
 3. §1 scope excluded applications, so §10.2's MUST didn't bind them → scope now names applications (§4, §5, §10.2).
 4. §4.5 HCF row claimed rename-only but redefines the series; the HCF PromQL in ARCHITECTURE/SitRep divided reversals by decisions → redefinition stated; queries use nthlayer_high_confidence_failures_total.
 Minors: "five things"; amends list adds Delta Summary; gen_ai_reversals_total in mapping; confidence condition covers segments metric high_confidence_failure.

Verify each fix is correct and introduces no new contradiction, then re-check the whole diff for remaining correctness issues. Validate with `bash spec/v2/validate.sh` from the repo path.

Report as bullet list of CRITICAL / IMPORTANT / MINOR with file:line, under 350 words, with verdict. "No correctness issues found" is valid.