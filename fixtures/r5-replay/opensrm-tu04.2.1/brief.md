# opensrm-tu04.2.1: tu04.2 follow-up: wire deploying.md to CI (drift gate)

Drift-check the deploying.md reference subsections against source.

- Env-var table rows match os.environ.get(...) calls in server.py.
- CLI subsection matches the argparse surface in cli.py.
- Manifests directory layout claim (no recursion; *.yaml + *.yml) matches catalogue.py.

Optional stretch: shell harness that runs the Tutorial section in a tmpdir
on CI to verify install + serve + verdict round-trip still work.

Deferred from tu04.2 design because the parity check should target a stabilised
doc, not one whose reference table is still churning. tu04.2 closed with all
4 R5 passes clean — doc is stable enough now to anchor a parity test.
