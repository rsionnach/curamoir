# opensrm-t4rd: nthlayer-generate: init writes invalid or injected manifests at exit 0

Found by the R5 edge-case pass during opensrm-noc6. All three PREDATE that bead
and are reachable without --no-interactive; verified against the pre-existing
--team flag and the unconditional name guard at cli/init.py:101. Filed
separately rather than folded into a docs fix.

Three soft failures where init exits 0 having written a manifest that is wrong.
The exit code is what a CI gate keys on.

1. The CLI name guard is looser than the manifest schema.
   _is_valid_service_name (cli/init.py:235) accepts any char where
   str.islower() or str.isdigit() is true, and allows a leading digit. The
   schema requires [a-z][a-z0-9-]*. These all exit 0 and then fail validate:
     nthlayer init 1-svc --team ops   -> validate: must start with lowercase
     nthlayer init cafe-with-accent   -> writes the file, validate rejects
     fullwidth and non-ASCII lowercase letters likewise pass the guard
   Path traversal is NOT reachable: / and . are correctly rejected.

2. --team is interpolated raw into YAML with only a truthiness check
   (cli/init.py:111, interpolated at :294). Verified, all exit 0:
     --team 'Platform: Core'            -> YAML fails to parse
     --team with an embedded newline
       plus "name: hijacked"            -> injects a second service.name
     --team with an embedded newline
       plus "tier: critical"            -> manifest VALIDATES CLEAN, carrying a
                                           bogus tier line shadowed by the
                                           generated one
   A colon in a team name is ordinary. This is also hard rule 5 (raw string
   construction for generated output) at a scriptable, unvalidated input.

3. Partial failure reports success. If .nthlayer exists as a regular FILE,
   mkdir raises FileExistsError and the config write raises NotADirectoryError;
   both are downgraded to warnings. Then `if nthlayer_dir.exists()` is true
   because it is a file, so init prints a success line for a directory it did
   not create, and returns 0. cli/init.py:197-216; the check at :215 should be
   is_dir().

The genuinely fatal cases are all handled well: missing name, missing team,
invalid name, path traversal, unknown template, existing target file and an
unwritable cwd all give a clear message and exit 1.

ACCEPTANCE
- The CLI name guard and the manifest validator agree. Derive the test table
  from what the schema requires, not from what the guard accepts (ecosystem
  CLAUDE.md fixture-provenance rule).
- team is validated at the boundary, or emitted as a quoted scalar. A colon and
  a newline in --team are both covered by tests.
- A run that fails to create .nthlayer does not print a success line for it;
  decide and document whether it is fatal under --no-interactive.
- A test runs init and then loads the result through the real manifest parser
  rather than asserting on substrings. That single test catches 1 and 2.
