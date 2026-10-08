"""doctor's own philosophy, applied to the three checks that did not follow it.

The module's stated principle is behavioural: "the file existing proves nothing
about whether a hook actually runs". Three checks were presence-only or
shape-only, and each produced a confident "ok" for a setup that does not work.
"""

import os
import stat

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
