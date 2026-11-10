# opensrm-3470: nthlayer-workers: observe spec_loader silently skips unparseable manifests

Same failure shape as opensrm-oh27, different consumer — found by that
bead's in-scope audit of nthlayer-workers.

src/nthlayer_workers/observe/slo/spec_loader.py:29-35

    for path in sorted(specs_path.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue
        try:
            manifest = load_manifest(path, suppress_deprecation_warning=True)
        except (ManifestLoadError, FileNotFoundError, ValueError, OSError):
            continue

No log, no counter, no re-raise. `load_specs` returns a flat list of
ServiceSLO, so a service whose manifest fails to parse contributes zero
SLOs and is indistinguishable from a service that declares none.

WHY IT MATTERS
oh27's case was a financial figure computed over a subset. This one is
the observe path measuring a subset: the SLOs that would have breached
are simply not evaluated, and nothing anywhere says so. The docstring
says "skips non-manifest files silently" — which is a reasonable
contract for a kustomization.yaml sharing the directory, and NOT a
reasonable one for a manifest that was meant to load and did not.

Note the handler is broader than oh27's was: it also swallows
FileNotFoundError, ValueError and OSError.

SUGGESTED FIX
Mirror what oh27 landed in learn/retrospective.py:
- log the skip with the file path and the error
- distinguish "foreign YAML sharing the directory" from "a manifest that
  was aiming to load and failed" — oh27's `_foreign_yaml_reason` is the
  worked version of that gate, including its stated limit
- surface a count to the caller so load_specs' consumers can tell a
  partial view from a complete one

Do NOT copy the gate by duplication. If both call sites want it, promote
it — see the sibling bead on exporting the data-level format predicates
from nthlayer_common.manifest.

DISCOVERED DURING: opensrm-oh27 (PR rsionnach/nthlayer-workers#9)
LABELS: v1.5
