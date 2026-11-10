# opensrm-z3ab: nthlayer-generate: duplicate service-type rule now disagrees with nthlayer-common

Follow-up to opensrm-ih0v, sharpened by its R5 correctness pass.

nthlayer-generate maintains its OWN copy of the service-type rule, in at
least three places:

  src/nthlayer_generate/specs/manifest.py:37   VALID_SERVICE_TYPES
  src/nthlayer_generate/specs/models.py:143    VALID_SERVICE_TYPES
  src/nthlayer_generate/specs/validator.py:212

Confirmed at runtime after ih0v landed:

  nthlayer_generate.specs.manifest.VALID_SERVICE_TYPES
    = {api, web, worker, stream, ai-gate, batch, database}
  nthlayer_generate.specs.manifest.SERVICE_TYPE_ALIASES
    = {background-job: worker, pipeline: batch}

So generate accepts `web` (which schema.json REJECTS) and rejects `x-web`
(which schema.json ACCEPTS, and which is exactly what nthlayer-common's
alias map now produces from a `web` input).

THIS IS A LIVE INCOMPATIBILITY, NOT COSMETIC DRIFT
A manifest declaring `type: web` is normalised by nthlayer-common to
`x-web`, and generate's validator then rejects it. Before ih0v both repos
said `web` and agreed. The decision to route web through the spec's
extension branch (rather than collapsing it into api) was deliberate --
generate distinguishes web from api in dashboard templates
(dashboards/templates/__init__.py:74), CLI init choices (cli/init.py:24,
34, 339), docs generation (generators/docs.py:455) and dashboard
validation (dashboards/validator.py:193) -- but generate's *validation*
constants were left behind.

CURRENTLY LATENT: a grep across the whole ecosystem finds NO manifest
declaring `type: web`, so nothing exercises the path today and every
suite is green. It bites the first web manifest anyone writes.

generate's models.py additionally lists `background-job` and `pipeline`
as VALID service types, not merely as aliases -- so generate would accept
a manifest storing an alias, which nthlayer-common's design explicitly
prevents (aliases resolve in __post_init__ before validation and are
never stored).

WORK
1. Decide the architecture first: should generate import
   VALID_SERVICE_TYPES / SERVICE_TYPE_ALIASES / is_valid_service_type
   from nthlayer_common.manifest.models rather than redeclaring them?
   generate already depends on nthlayer-common. A single source of truth
   is the point -- ih0v's own correctness pass flagged the second and
   third copies inside nthlayer-common and they were collapsed into one
   predicate for exactly this reason.
2. Whichever way, make generate accept `x-web` and stop accepting bare
   `web`, `background-job` and `pipeline` as stored types.
3. Update the ~6 non-validation sites that branch on `web` to `x-web`:
   dashboards/templates/__init__.py:74, cli/init.py:24/34/339,
   generators/docs.py:455, dashboards/validator.py:193,
   specs/templates.py:33, metrics/templates/registry.py:30.
4. tests/test_opensrm.py:450 asserts `"web" in VALID_SERVICE_TYPES` and
   labels it an NthLayer extension -- update to x-web.
5. Add a cross-repo test that a manifest round-tripping
   nthlayer-common -> nthlayer-generate keeps its type. No test covers
   that boundary today, which is why this went unnoticed.

RELATED: opensrm-8sa6 (registry.py has no ai-gate or stream template) is
in the same file family and could be done in the same pass.

LABELS: v1.5
