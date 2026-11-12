"""cm-2qp.7 (E14.7, docs/direction-platform-first.md §3, §5 R8 and §8 (5)):
the README's "Current status" and "Guardrails" are written against the
runner-kinds table.

What is pinned is what the bead and the user's instruction name, not the
prose: the sentence the direction retires ("nothing is scheduled for you
yet") and the removed command (`/curamoir:schedule`, R1) stay out; the
README links the direction doc; the boundary table has a column per runner
kind; and the "Local adapter" subsection says plainly that the local
agent's own `claude -p` is not OS-sandboxed (user decision 2026-10-29,
cm-xy9.8 parked). The command table's rows are test_routine_command.py's.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DIRECTION = "docs/direction-platform-first.md"


def readme():
    return README.read_text(encoding="utf-8")


def flat(text):
    """`text` as one line, without the blockquote markers: the README wraps
    at about 78 columns, and "Current status" is a blockquote."""
    return " ".join(re.sub(r"^>\s?", "", line) for line in text.splitlines()).split()


def section(heading):
    """The body of the `## `/`### ` section titled `heading`, up to the next
    heading of the same or a higher level."""
    text = readme()
    match = re.search(rf"^(#+) {re.escape(heading)}\n", text, re.MULTILINE)
    assert match, f"the README has no {heading!r} heading"
    level = len(match.group(1))
    rest = text[match.end():]
    end = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return rest[:end.start()] if end else rest


def test_the_readme_no_longer_says_nothing_is_scheduled():
    assert "nothing is scheduled for you yet" not in " ".join(flat(readme())).lower()


def test_the_readme_never_names_the_removed_schedule_command():
    assert "/curamoir:schedule" not in readme()


def test_current_status_links_the_direction_doc():
    assert f"]({DIRECTION})" in section("Current status")
    assert (ROOT / DIRECTION).is_file()


def test_current_status_names_both_hosted_runners_and_one_per_repository():
    status = " ".join(flat(section("Current status")))

    assert "/curamoir:init" in status and "/curamoir:routine" in status
    assert "one runner per repository" in status


def test_the_boundary_table_has_a_column_per_runner():
    boundary = section("Where the security boundary is")
    header = next(line for line in boundary.splitlines() if line.startswith("| |"))

    assert "Hosted" in header and "Local" in header
    for row in ("Process boundary", "Credentials at risk", "Agents' tools",
                "Your tests", "One sweep at a time", "Kill switch",
                "Memory between runs"):
        assert f"| {row} |" in boundary, row


def test_the_local_adapter_says_its_agent_is_not_os_sandboxed():
    local = " ".join(flat(section("Local adapter")))

    assert "`claude -p` is not OS-sandboxed" in local


def test_known_limits_no_longer_carries_the_laptop_only_prose():
    """R8: the stale-lock and CURAMOIR_JOB prose moved to "Local adapter"."""
    limits = section("Known limits")

    assert "CURAMOIR_JOB" not in limits
    assert "stale" not in limits


def test_a_routines_own_session_is_said_to_load_the_repositorys_configuration():
    """cm-2qp.7 redo, correctness: restricted mode covers the scan and the
    fix agent only. A Routine's own session is a Claude Code session in a
    clone of the repository and loads its configuration and hooks, so the
    README says so, not that the configuration never loads."""
    guardrails = " ".join(flat(section("Guardrails")))
    limits = " ".join(flat(section("Hosted runner limits")))

    assert "never loads" not in guardrails
    assert "never reaches the scan or the fix agent" in guardrails
    assert ("loads the default branch's `CLAUDE.md`, settings and hooks, and they run with "
            "the session's GitHub access and Claude credentials") in limits
    assert "that is a prompt, not a fence: a hook still runs" in limits


def test_the_runner_declaration_is_said_to_check_markers_not_prove_the_runner():
    """config.resolve_runner accepts `hosted` on the runners' environment
    markers alone, which a laptop could set."""
    boundary = " ".join(flat(section("Where the security boundary is")))

    assert "refused anywhere else" not in boundary
    assert "own environment markers are set, and refused where they are absent" in boundary
    assert "it does not prove where the run is" in boundary


def test_the_local_test_network_row_says_which_platform_gets_loopback():
    """os_sandbox: bwrap's --unshare-net always gives a private loopback;
    `sweep.test_loopback` gates loopback on macOS only."""
    boundary = " ".join(flat(section("Where the security boundary is")))

    assert ("on macOS not even loopback unless `sweep.test_loopback: true`, on Linux a "
            "private loopback only") in boundary
