"""cm-kry: scripts/curamoir runs the CLI with an interpreter that has PyYAML.

Found by the first real dogfood sweep (2026-10-28): on a machine where
Homebrew's python3.14 exists without PyYAML and an older python3 has it, the
wrapper took the first interpreter that EXISTS, and every `curamoir run`
refused because curamoir.yaml could not be read. scripts/curamoir-lint already
takes the first that can `import yaml`, falling back to the first that exists;
the CLI wrapper now does the same.

Each test puts stand-in interpreters on a PATH of their own. A stand-in runs
the interpreter running these tests (which has PyYAML, a project dependency),
the "bare" one with `-S` so no site-packages, and so no PyYAML, is importable.
Each logs how it was called, so the test can tell which one ran the CLI.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "curamoir"


def stand_in(directory, name, log, bare):
    script = directory / name
    flags = "-S " if bare else ""
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "{name} $*" >> "{log}"\n'
        f'exec "{sys.executable}" {flags}"$@"\n',
        encoding="utf-8")
    script.chmod(0o755)


def run_wrapper(tmp_path, interpreters):
    """Run `scripts/curamoir --help` with only `interpreters` ({name: bare})
    and the system's own directories on PATH. Returns (result, log lines)."""
    shims = tmp_path / "bin"
    shims.mkdir()
    log = tmp_path / "calls.log"
    for name, bare in interpreters.items():
        stand_in(shims, name, log, bare)
    environment = {"PATH": os.pathsep.join([str(shims), "/usr/bin", "/bin"]),
                   "HOME": str(tmp_path)}
    result = subprocess.run([str(WRAPPER), "--help"], capture_output=True, text=True,
                            env=environment, timeout=60)
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, lines


def ran_the_cli(lines):
    return [line.split()[0] for line in lines if "-m curamoir.cli" in line]


def test_the_cli_runs_with_the_first_interpreter_that_has_pyyaml(tmp_path):
    result, lines = run_wrapper(tmp_path, {"python3.14": True, "python3": False})

    assert result.returncode == 0, result.stderr
    assert ran_the_cli(lines) == ["python3"], lines


def test_with_no_interpreter_that_has_pyyaml_the_first_that_exists_still_runs(tmp_path):
    """So the CLI can say what is missing (doctor does), rather than the
    wrapper failing with nothing to say."""
    result, lines = run_wrapper(tmp_path, {"python3.14": True, "python3": True})

    assert result.returncode == 0, result.stderr
    assert ran_the_cli(lines) == ["python3.14"], lines
