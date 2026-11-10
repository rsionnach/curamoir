# opensrm-ih0v: nthlayer-common: read spec.service.type, delete _infer_service_type

Follow-up to opensrm-6w9d (v2 service type discriminator). Blocked on the
spec change landing in opensrm.

Design doc: opensrm/docs/superpowers/specs/2026-08-17-v2-service-type-discriminator-design.md

opensrm-6w9d makes `spec.service.type` a required first-class field on the
v2 ServiceIdentity, with enum {api, worker, stream, ai-gate, batch,
database} plus an `^x-[a-z][a-z0-9-]*$` extension escape hatch. This bead
makes nthlayer-common read it.

WORK
1. Delete `_infer_service_type()` (src/nthlayer_common/manifest/parser/v2.py:215-244)
   outright. Do not retain it as a fallback -- the field is required, so
   inference has no legitimate case left. Its `judgment_slo` -> ai-gate
   heuristic (:233-235) inverts v1's permission rule and silently
   reclassifies a worker that wrongly declares judgment SLOs as an ai-gate,
   routing it into ai-gate-only codepaths.
2. Read `spec["service"]["type"]` in parser/v2.py (call site :119-121, value
   consumed at :163) and surface a clear error when absent, naming the field
   path rather than instructing authors to set metadata.labels.type as the
   current message does (:242-244).
3. Update `v1_compat.py:222-226` to write v1's `spec.type` to
   `spec.service.type` on upconversion, not to `metadata.labels.type`.
   Check whether any consumer reads labels.type and needs a transition.
4. Resolve `web`: VALID_SERVICE_TYPES (models.py:45-53) carries seven values
   including `web`, which nthlayer-generate/tests/test_opensrm.py:450
   explicitly labels an NthLayer extension. Either rename to `x-web` per the
   spec's extension rule, or remap web -> api. Decide and document. Also
   decide whether SERVICE_TYPE_ALIASES (models.py:55-58,
   background-job -> worker, pipeline -> batch) stay as implementation
   conveniences -- the spec does not define aliases.
5. Accept `x-` prefixed types in the validation at models.py:834 so an
   extension type is not rejected as invalid.

ACCEPTANCE
- spec/v2/examples/services/{api,worker,stream}.yaml parse successfully.
  They fail TODAY at parser/v2.py:242 while validate.sh is green on all
  four -- the seam this bead closes.
- A regression test asserts each of the four shipped archetype examples
  both validates against schema.json AND parses to a ReliabilityManifest.
  No test covers that seam currently, which is why the divergence survived.
- A worker manifest declaring judgment_slo is REJECTED, not reclassified.
- Full test suite green.

NOTE: `spec.service` is polysemous in v2 -- a ServiceIdentity object in a
ServiceManifest, but a plain string naming the service in a standalone
`kind: JudgmentSLO` document (JudgmentSLOSpec). Do not conflate them.
