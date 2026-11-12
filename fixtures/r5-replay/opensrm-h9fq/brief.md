# opensrm-h9fq: nthlayer-generate: setup wizard interpolates team/name/tier raw into YAML

Found by the R5 correctness pass on opensrm-t4rd (iterations 2 and 3). Deferred
from that bead because setup.py is outside its diff. TWO defects in the same
file.

## 1. team/name/tier/type interpolated raw into YAML

cli/setup.py:456 (_generate_service_yaml) builds the manifest by f-string:

    service:
      name: {name}
      team: {team}
      tier: {tier}
      type: {service_type}

team comes from a prompt at cli/setup.py:396 (`_prompt("Team name",
default="platform")`), so it is user-supplied text with no validation and no
quoting. Typing `Platform: Core` reproduces t4rd defect 2 exactly: either the
manifest fails to parse, or a typed newline injects a second field that the
generated one shadows while the document still validates.

Lower severity than t4rd because it is prompt-sourced rather than scriptable via
a flag, so it is not reachable from CI.

t4rd built the machinery in cli/init.py: `_yaml_scalar` (plain when it provably
round-trips, otherwise pyyaml-quoted; accepts non-str), `_emits_as_same_string`
(round-trip test, catches YAMLError + ValueError), `_quoted_yaml_scalar`,
`_is_valid_team`. Route all four values through `_yaml_scalar` and gate team
with `_is_valid_team`. Quoting must stay CONDITIONAL: unconditional quoting
breaks test_block_is_byte_identical_to_real_output, the docs-vs-reality guard
from opensrm-noc6.

## 2. A THIRD, divergent copy of the service-name guard

cli/setup.py:503 defines its own `_is_valid_service_name` looping over
`char.islower() or char.isdigit() or char == '-'` — the UNICODE-predicate shape
t4rd removed from cli/init.py. Measured against the real function, it accepts
`123`, `café` and `1-svc`, all of which the shared rule in specs/manifest.py
rejects. It DOES reject `svc-`, `-svc` and ``, so the divergence is partial.

This is the same soft failure t4rd closed for init, one command over: setup
accepts a name, writes the manifest, and `nthlayer validate` then refuses it.

specs/manifest.py now carries a caveat saying its rule is NOT exhaustive for
this reason, and test_init.py::TestSetupGuardStillDiverges asserts the
divergence so the comment cannot rot. That test FAILS when this bead is fixed,
which is deliberate — it forces the comment, the test and the bead to close
together.

Acceptance:
- setup.py delegates to `is_valid_service_name` from specs/manifest; no fourth copy.
- A colon and a newline typed at the team prompt are both covered by tests.
- Non-BMP input round-trips (t4rd's surrogate-pair regression must not return).
- A test loads the wizard's output through validate_service_file, not substrings.
- TestSetupGuardStillDiverges and the specs/manifest.py caveat are both removed.
- Fixtures measured against the real functions, not reimplementations of them.
