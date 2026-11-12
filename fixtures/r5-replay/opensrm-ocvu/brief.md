# opensrm-ocvu: nthlayer-common: v1 and v2 parsers disagree on SLO target units

Found during opensrm-fxln, surfaced by nthlayer-common's own
TargetConventionWarning firing on a perfectly ordinary v2 manifest.

The SAME SLO, meaning the same thing, parses to different units
depending on the manifest format:

  v1 (spec.slos)       availability target = 99.9
  v2 (OpenSLO doc)     availability target = 0.999

Reproduced with two minimal manifests differing only in format.

nthlayer-common CLAUDE.md hard rule 1 is unambiguous about which is
canonical:

  "SLO target convention is 0-100 percentage. SLODefinition.target uses
   0-100 percentage across every NthLayer-internal consumer (classical
   AND judgment SLOs). The OpenSLO surface (slo_models.SLO) uses 0.0-1.0
   ratio. Conversion happens at the boundary."

That names the OUTBOUND boundary (nthlayer-generate's
slos.pipeline._build_slo_from_manifest divides by 100). The INBOUND
boundary — parser/v2 reading an OpenSLO document's objectives[].target —
does not multiply by 100, so a ratio reaches SLODefinition.target
unconverted.

nthlayer-common already KNOWS this is wrong. manifest/target_validation.py
raises TargetConventionWarning for targets in (0,1), and it fires on
every v2 manifest carrying a normal OpenSLO objective:

  "Service 'checkout' SLO 'availability' has target=0.999 which looks
   like a ratio (0.0-1.0). This codebase uses 0-100 percentage canonical
   for SLO targets"

So the validator flags the parser's own output. The warning is correct
and the parser is wrong.

WHY THIS IS P1
Every consumer comparing a target against a measured value gets a
silently different answer depending on which format the manifest happens
to be in. opensrm-fxln has just fixed measure's judgment breach check to
scale correctly against a 0-100 target; that fix is right for v1 and
wrong for v2 through no fault of its own. The same applies to
observe/slo/collector, to bench, and to anything reading
manifest.slos[].target.

This is the wrong-side mistake hard rule 1 exists to prevent, and it
sits in the parser rather than in a consumer.

CHECK BEFORE FIXING
- Whether spec/v2's own archetypes are affected: api.yaml carries
  `target: 0.9995` in an inline OpenSLO doc. Note the archetypes cannot
  currently be parsed at all (opensrm-47tt), so this has been invisible.
- Whether anything DEPENDS on the ratio arriving unconverted — grep for
  consumers doing their own *100 or /100 on manifest.slos targets. The
  measure adapter used to do exactly that for availability only; that
  local normalisation was deleted in opensrm-fxln.
- Judgment SLOs on the v2 path: spec.judgment_slo targets are typed by
  judgment_type (maximum_reversal_rate etc.), which are naturally rates.
  Decide whether those follow the same rule or are a documented
  exception — v1_compat._v1_slo_to_judgment carries v1's percentage
  through unchanged, so the two paths may disagree there too.

ACCEPTANCE
- A v1 and a v2 manifest declaring the same SLO produce the same
  SLODefinition.target.
- TargetConventionWarning does not fire on a valid v2 manifest.
- A test asserts the equivalence directly, parametrised over both
  formats — the boundary no test currently crosses.

RELATED: opensrm-fxln (measure adapter), opensrm-5fff (the convention
decision), opensrm-47tt (why the v2 archetypes could not reveal this).

LABELS: v1.5
