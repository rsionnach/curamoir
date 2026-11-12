# opensrm-jmy.18: verdict-binding path for overrides: sidecar→core direct POST (jmy.7 follow-up)

Reframed (post-challenge-the-premise, 2026-05-19): direct sidecar→core POST path replaces the OTel-consumer-in-worker design. Conflating observability transport with state mutation creates unnecessary coupling between the two concerns.

Scope:
- Core gains POST /verdicts/{id}/outcome endpoint (or equivalent) accepting override binding
- Sidecar (nthlayer-override-adapter) gains direct-POST path alongside existing OTel emission
- Both paths use same redacted payload (privacy applied uniformly at sidecar entry; per-target privacy config deferred)
- Idempotency: replaying same override does not double-update

Acceptance:
- Override emitted via sidecar reaches core via direct POST
- Verdict outcome.status correctly updated to "overridden"
- OTel path continues working independently for reversal-rate metrics
- Retrospectives can render "verdict X was overridden by operator Y"

Counter-considerations resolved:
- Worker placement / decoupling: not a blocker for MVP. Override acceptance is already coupled to OTel collector availability; adding core API as a second target doesn't fundamentally change the coupling profile. If reliability requirements emerge later, the sidecar gets a local buffer with retry logic; small addition when needed.
- Privacy config locus: privacy applies uniformly at sidecar entry; both OTel emission and direct POST receive the same redacted payload. Per-target privacy configuration deferred to v2 if audit trail completeness becomes a real requirement.
- Spec § 4 wording: "look up the verdict by decision_id; update outcome.status" is a state-mutation contract, which the reframe matches more literally than the OTel-consumer interpretation.

Effort: 1-2 sessions including R5
Priority: Required for retrospective completeness; reversal-rate path works without it
Discovery: jmy.7 verification + reframe via challenge-the-premise skill (2026-05-19)

Original framing (superseded): OTel consumer in nthlayer-workers/measure subscribes to gen_ai.override events and calls apply_override_to_verdict.
