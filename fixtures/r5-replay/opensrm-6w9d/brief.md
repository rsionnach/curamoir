# opensrm-6w9d: Decide whether OpenSRM v2 should reintroduce a service type discriminator

Split out of opensrm-tu04.3 (v2 manifest authoring guide), 2026-08-15.

v1 had `spec.type` in {api, worker, stream, ai-gate}, which drove type-specific validation (spec/v1/specification.md sec 3 — e.g. judgment SLOs only valid for ai-gate). v2 DROPPED it: ServiceIdentity in spec/v2/schema.json carries only name + description, and OPENSRM-CORE-v2.md never mentions service types.

The tu04.3 guide works around this by treating the four archetypes as an informal documentation-organising axis only (Rob's call, 2026-08-15) — no field reintroduced, schema.json unchanged. Each archetype is distinguished by which blocks it uses, not by a discriminator.

DEFERRED QUESTION: is the omission deliberate (v2 leans on Backstage entity metadata / labels for categorisation) or an oversight? If oversight, v2 loses v1's type-conditional validation, e.g. nothing stops a 'worker' declaring judgment SLOs.

Decide, then either (a) document the omission as intentional in OPENSRM-CORE-v2.md, or (b) add an optional service.type enum to the spec + schema and restore the conditional validation rules. Either way update spec/v2/AUTHORING.md sec 3.

Spec change — do not fold back into the guide bead.
