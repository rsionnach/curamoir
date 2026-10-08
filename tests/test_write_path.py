"""The two artefacts the plugin exists to produce had no safe write path.

`write_artefact` is the single write path E0-R7 calls for, so every property it
lacks is lacked by the handoff, the brief, issue bodies and PR bodies at once.
It lacked two:

- **atomicity.** `write_text` truncates and then writes, so a write that fails
  part way — a full disk, a quota — leaves a fragment where a complete artefact
  was. The previous handoff is already gone at that point, and SessionStart will
  inject the fragment next session as "state from the previous session".
- **a unique archive name.** The name came from `latest.md`'s mtime at SECOND
  resolution and was written with `write_text`, so two handoffs in the same
  second left one archive entry. The spec calls the archive append-only.

Neither needs an unusual machine to reach: PreCompact followed by SessionEnd,
or `/curamoir:handoff` followed by exit, land in the same second routinely.
"""

import os
import resource
import signal

import pytest

from curamoir.handoff import write_handoff
from curamoir.redaction import write_artefact
from curamoir.scaffold import init_repo


def initialised(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


def test_three_handoffs_in_one_second_leave_three_archive_entries(tmp_path):
    """At second resolution the second write clobbered the first, so the oldest
    handoff was gone from BOTH latest.md and the archive that exists to keep it."""
    repo = initialised(tmp_path)

    for note in ("handoff FIRST", "handoff SECOND", "handoff THIRD"):
        write_handoff(repo, note=note)

    history = repo / ".curamoir" / "handoff" / "history"
    archived = sorted(path.name for path in history.iterdir())

    assert len(archived) == 2, f"two writes should have been archived, got {archived}"
    bodies = "\n".join(
        (history / name).read_text(encoding="utf-8") for name in archived
    )
    assert "handoff FIRST" in bodies, "the first handoff was overwritten in the archive"
    assert "handoff SECOND" in bodies


def test_a_failed_write_leaves_the_previous_artefact_intact(tmp_path):
    """Either the old complete artefact or the new one, never a fragment.

    RLIMIT_FSIZE reproduces ENOSPC faithfully — the same OSError at the same
    point, with the same partial write behind it.
    """
    target = tmp_path / "latest.md"
    good = "# the previous handoff\n\n" + "- **branch:** main\n" * 20
    write_artefact(target, good)

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (400, hard))
    try:
        with pytest.raises(OSError):
            write_artefact(target, "# the replacement\n\n" + "- **note:** x\n" * 200)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))

    assert target.read_text(encoding="utf-8") == good, (
        "a failed write replaced a complete handoff with a fragment"
    )


def test_a_failed_write_leaves_no_temporary_file_behind(tmp_path):
    """An atomic write that litters turns one failure into a growing directory."""
    target = tmp_path / "latest.md"
    write_artefact(target, "# first\n")

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (100, hard))
    try:
        with pytest.raises(OSError):
            write_artefact(target, "x" * 5_000)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["latest.md"]


def test_write_handoff_keeps_its_never_raises_contract(tmp_path):
    """The docstring says it never raises, and the hooks rely on that. The build
    was wrapped and the archive was wrapped; the final write was not, so a
    read-only handoff directory — which doctor has a whole check for — reached
    `/curamoir:handoff` as a traceback."""
    repo = initialised(tmp_path)
    handoff_dir = repo / ".curamoir" / "handoff"
    write_handoff(repo, note="first")
    os.chmod(handoff_dir, 0o500)
    try:
        result = write_handoff(repo, note="second")
    finally:
        os.chmod(handoff_dir, 0o700)

    assert result is None, "an unwritable target should report failure, not a path"


# --- the CLI's own preconditions and failure reporting ---------------------

def test_brief_and_handoff_refuse_an_uninitialised_repo(tmp_path):
    """`status` requires .curamoir/ and the HOOKS check for it, but the CLI's
    brief and handoff did not: running either first created a half-built
    .curamoir/ holding only their own output, after which `init` refused
    forever and the repo never got curamoir.yaml, principles.md or state/."""
    from curamoir.cli import main

    for command in ("brief", "handoff"):
        assert main([command, "--repo", str(tmp_path)]) == 1, command
        assert not (tmp_path / ".curamoir").exists(), (
            f"{command} created a partial .curamoir/ that init will refuse"
        )

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert main(["init", "--repo", str(tmp_path)]) == 0


def test_the_cli_reports_an_unwritable_handoff_rather_than_a_traceback(tmp_path, capsys):
    """A slash command relays this to the developer, so a stack trace is the
    one thing it must not print."""
    from curamoir.cli import main

    repo = initialised(tmp_path)
    handoff_dir = repo / ".curamoir" / "handoff"
    os.chmod(handoff_dir, 0o500)
    try:
        code = main(["handoff", "--repo", str(repo)])
    finally:
        os.chmod(handoff_dir, 0o700)

    assert code == 1
    assert "Traceback" not in capsys.readouterr().out


def test_a_damaged_install_is_reported_in_one_sentence(tmp_path, capsys):
    """`init` already turns a missing bundled template into a sentence via
    TemplateDrift; every other OSError reached the developer as a traceback."""
    from curamoir.cli import main

    not_a_directory = tmp_path / ".curamoir"
    not_a_directory.write_text("this is a file, not a directory\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    code = main(["init", "--repo", str(tmp_path)])

    assert code == 1
    assert "Traceback" not in capsys.readouterr().out
