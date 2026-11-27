"""doctor's own philosophy, applied to the three checks that did not follow it.

The module's stated principle is behavioural: "the file existing proves nothing
about whether a hook actually runs". Three checks were presence-only or
shape-only, and each produced a confident "ok" for a setup that does not work.
"""

import os
import stat
from pathlib import Path

from curamoir.doctor import diagnose
from curamoir.scaffold import init_repo


def finding(findings, name):
    return next(f for f in findings if f.check == name)


def test_a_missing_state_directory_is_not_reported_as_writable(tmp_path):
    """The filter was `path.is_dir() and not writable`, so a directory that does
    not exist contributed nothing and an EMPTY .curamoir/ reported "handoff,
    state and briefs are writable". Reachable whenever a CLI command created
    .curamoir/ before init did."""
    (tmp_path / ".curamoir").mkdir()

    result = finding(diagnose(tmp_path), "writable state")

    assert result.level != "ok", f"claimed ok with no directories: {result.detail}"


def test_a_bd_that_cannot_reach_its_database_is_reported(tmp_path, monkeypatch):
    """`bd` is the one prerequisite checked by mere presence, and its documented
    failure mode is "installed, but the Dolt server is down". Reporting
    "available" then sends the handoff and the brief to a stale export while
    doctor says the setup is fine."""
    directory = tmp_path / "fakebin"
    directory.mkdir()
    script = directory / "bd"
    script.write_text(
        "#!/bin/sh\necho 'error: dial tcp 127.0.0.1:3306: connection refused' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    # A repo that USES beads: the probe is deliberately skipped for one that
    # does not, because `filing.beads: auto` supports that and `bd ready` exits
    # non-zero there for a reason doctor must not call a fault.
    (tmp_path / ".beads").mkdir()

    result = finding(diagnose(tmp_path), "bd")

    assert result.level != "ok", f"a broken bd reported as {result.detail!r}"


def test_a_working_bd_is_still_reported_as_available(tmp_path, monkeypatch):
    """The probe must not fail a healthy install."""
    directory = tmp_path / "fakebin"
    directory.mkdir()
    script = directory / "bd"
    script.write_text("#!/bin/sh\necho '[]'\nexit 0\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    (tmp_path / ".beads").mkdir()

    assert finding(diagnose(tmp_path), "bd").level == "ok"


def test_a_python_below_the_floor_is_reported(tmp_path, monkeypatch):
    """The fix text says "Install Python 3.12 or later" while the check only
    asked whether python3 resolves. On this machine `python3` is 3.9, which is
    what the hook wrappers would use."""
    directory = tmp_path / "fakebin"
    directory.mkdir()
    script = directory / "python3"
    script.write_text("#!/bin/sh\necho '3.8'\nexit 0\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(directory))
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)

    result = finding(diagnose(tmp_path), "python3")

    assert result.level != "ok", f"3.8 reported as {result.detail!r}"
    assert "3.12" in result.detail or "3.12" in (result.fix or "")


def test_a_repo_without_beads_is_not_reported_as_a_broken_bd(tmp_path, monkeypatch):
    """`filing.beads: auto` supports a repo that does not use beads, and there
    `bd ready` exits non-zero with "no beads database found". Reported as a
    fault — with Dolt advice that cannot help — it is exactly what doctor's own
    docstring warns against: it teaches the developer to ignore the output."""
    directory = tmp_path / "fakebin"
    directory.mkdir()
    script = directory / "bd"
    script.write_text(
        "#!/bin/sh\necho 'Error: no beads database found' >&2\nexit 1\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)

    assert finding(diagnose(tmp_path), "bd").level == "ok"


def test_a_leftover_write_probe_does_not_wedge_doctor_into_a_false_error(tmp_path):
    """With a fixed probe name, a leftover file — from a killed doctor, or from
    `git pull`, since `.curamoir/` is committed — made FileExistsError read as
    "not writable" permanently, with a fix the developer cannot act on because
    the permissions are fine."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    (tmp_path / ".curamoir" / "handoff" / ".curamoir-write-probe").write_text(
        "leftover\n", encoding="utf-8"
    )

    assert finding(diagnose(tmp_path), "writable state").level == "ok"


def test_a_curamoir_predating_the_gitignore_is_reported(tmp_path):
    """`init_repo` refuses an existing `.curamoir/`, so no repo initialised
    before the .gitignore existed will ever receive one — and the journal holds
    verbatim session text. doctor is the only thing that sees those repos."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    (tmp_path / ".curamoir" / ".gitignore").unlink()

    result = finding(diagnose(tmp_path), "gitignore")

    assert result.level == "warning"
    assert "journal" in result.detail


def test_a_freshly_initialised_repo_passes_the_gitignore_check(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)

    assert finding(diagnose(tmp_path), "gitignore").level == "ok"


def test_doctor_probes_the_interpreter_the_wrappers_would_actually_use():
    """doctor resolved `python3` while the wrappers try the versioned binaries
    first, so a machine with 3.12 installed beside an older default got an
    error for hooks that work. The two lists are duplicated — doctor must not
    parse shell — so this asserts they agree."""
    import re

    from curamoir.doctor import WRAPPER_INTERPRETERS

    text = Path("src/curamoir/launcher.sh").read_text(encoding="utf-8")
    match = re.search(r'^CURAMOIR_PYTHON_CANDIDATES="([^"]+)"$', text, re.MULTILINE)
    assert match, "src/curamoir/launcher.sh no longer names its candidates"
    assert tuple(match.group(1).split()) == WRAPPER_INTERPRETERS, (
        "the launchers try a different order than doctor probes")


def test_doctors_probe_and_versioned_names_agree_with_the_launchers_and_pyproject():
    """doctor copies the launchers' probe and their unprobed names rather than
    parse shell: a copy that drifted would report "ok" for an interpreter
    every launcher skips, hooks that fail open while looking installed. Both
    floors, and the versioned names, are pinned to launcher.sh and to
    pyproject's requires-python (cm-6f0, provenance)."""
    import re
    import tomllib

    from curamoir.doctor import _QUALIFIES, _VERSIONED_INTERPRETERS, MINIMUM_PYTHON

    requires = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["requires-python"]
    floor = tuple(int(part) for part in re.fullmatch(r">=(\d+)\.(\d+)", requires).groups())
    text = Path("src/curamoir/launcher.sh").read_text(encoding="utf-8")

    probe = re.search(r"""^\s*"\$1" (-P -E -c) '([^']+)'""", text, re.MULTILINE)
    assert probe, "src/curamoir/launcher.sh no longer probes unversioned names"
    assert (*probe.group(1).split(), probe.group(2)) == _QUALIFIES
    assert _QUALIFIES[-1] == f"import sys; raise SystemExit(sys.version_info < {floor})"
    assert floor == MINIMUM_PYTHON
    shown = re.search(r'^CURAMOIR_PYTHON_FLOOR="([^"]+)"$', text, re.MULTILINE).group(1)
    assert shown == ".".join(map(str, floor))

    unprobed = re.search(r"^\s*(python[0-9.]+(?: \| python[0-9.]+)*)\) return 0 ;;$",
                         text, re.MULTILINE)
    assert unprobed, "src/curamoir/launcher.sh no longer names its unprobed interpreters"
    assert tuple(unprobed.group(1).split(" | ")) == _VERSIONED_INTERPRETERS
    assert all(tuple(map(int, name.removeprefix("python").split("."))) >= floor
               for name in _VERSIONED_INTERPRETERS)


def test_doctor_reports_a_set_pythonpath_through_diagnose(tmp_path, monkeypatch):
    """The ruling is that doctor reports it, not that a helper can: the check
    must be one diagnose runs (cm-6f0, provenance)."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "vendor")

    result = finding(diagnose(tmp_path), "PYTHONPATH")

    assert result.level == "warning" and "ignored" in result.detail
    monkeypatch.delenv("PYTHONPATH")
    assert not [f for f in diagnose(tmp_path) if f.check == "PYTHONPATH"]
