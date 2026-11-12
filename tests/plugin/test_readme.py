"""cm-2qp.7 (E14.7, docs/direction-platform-first.md §3, §5 R8 and §8 (5)):
the README's "Current status" and "Guardrails" are written against the
runner-kinds table.

What is pinned is what the bead and the user's instruction name: the
sentence the direction retires ("nothing is scheduled for you yet") and the
removed command (`/curamoir:schedule`, R1) stay out; the README links the
direction doc; the boundary table has a column per runner kind and every
row; and the "Local adapter" subsection says plainly that the local agent's
own `claude -p` is not OS-sandboxed (user decision 2026-10-29, cm-xy9.8
parked). The three facts the correctness review fixed (a Routine's own
session loads the repository's configuration, the runner check reads
markers, which platform gets loopback) are pinned by their key phrases,
not by whole sentences, so the prose around them can be reworded. The
command table's rows are test_routine_command.py's.
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
    at about 78 columns, and "Current status" is a blockquote. Every
    leading `>` goes, so a nested blockquote (`>>`, `> >`) flattens too."""
    return " ".join(re.sub(r"^(?:>\s?)+", "", line) for line in text.splitlines()).split()


def section(heading, text=None):
    """The body of the `## `/`### ` section titled `heading` in `text` (the
    README by default), up to the next heading of the same or a higher
    level. The heading must occur exactly once, at any level, so a repeated
    heading fails loudly rather than cutting the wrong section; a `#` line
    inside a fenced code block is not a heading."""
    text = readme() if text is None else text
    headings = []  # (level, start of the heading line, end of it)
    fenced = False
    offset = 0
    for line in text.splitlines(keepends=True):
        if re.match(r"\s*(```|~~~)", line):
            fenced = not fenced
        elif not fenced and (match := re.match(r"(#+) (.*?)\n?$", line)):
            headings.append((len(match.group(1)), match.group(2), offset, offset + len(line)))
        offset += len(line)
    found = [entry for entry in headings if entry[1] == heading]
    assert len(found) == 1, f"the text has {len(found)} {heading!r} headings, not one"
    level, _, _, body_start = found[0]
    end = next((start for other, _, start, _ in headings
                if start >= body_start and other <= level), len(text))
    return text[body_start:end]


def test_flat_strips_nested_blockquote_markers():
    assert flat("> a\n>> b\n> > c\nd > e") == ["a", "b", "c", "d", ">", "e"]


def test_section_ignores_a_hash_line_inside_a_fenced_block():
    text = ("## Known limits\n\nok\n\n```bash\n# set it\nexport X=1  # stale\n```\n\n"
            "after the fence\n\n### Sub\n\nsub text\n\n## Next\n\nnot in it\n")
    body = section("Known limits", text)

    assert "after the fence" in body and "sub text" in body
    assert "not in it" not in body


def test_section_refuses_a_repeated_or_missing_heading():
    for text in ("## Known limits\n\na\n\n### Known limits\n\nb\n", "## Other\n\nc\n"):
        try:
            section("Known limits", text)
        except AssertionError:
            continue
        raise AssertionError(f"section() accepted {text!r}")


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
    rows = {cells[0]: cells for line in boundary.splitlines() if line.startswith("| ")
            for cells in [[cell.strip() for cell in line.strip().strip("|").split("|")]]}
    for row in ("Process boundary", "Your checkout", "Credentials at risk",
                "Agents' tools", "Your tests", "One sweep at a time",
                "Kill switch", "Spend", "Memory between runs"):
        assert row in rows, row
        cells = rows[row]
        assert len(cells) == 3 and all(cells), (row, cells)


def test_the_hosted_kill_switch_names_the_default_branch():
    boundary = section("Where the security boundary is")
    kill_switch = next(line for line in boundary.splitlines() if line.startswith("| Kill switch |"))

    assert "`.curamoir/PAUSE` committed to the default branch" in kill_switch


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
    assert "loads the default branch's" in limits
    assert "a hook still runs" in limits


def test_the_runner_declaration_is_said_to_check_markers_not_prove_the_runner():
    """config.resolve_runner accepts `hosted` on the runners' environment
    markers alone, which a laptop could set."""
    boundary = " ".join(flat(section("Where the security boundary is")))

    assert "refused anywhere else" not in boundary
    assert "environment markers" in boundary
    assert "it does not prove where the run is" in boundary


def test_the_local_test_network_row_says_which_platform_gets_loopback():
    """os_sandbox: bwrap's --unshare-net always gives a private loopback;
    `sweep.test_loopback` gates loopback on macOS only. The table row and
    the Local adapter's bullet both say so."""
    for heading in ("Where the security boundary is", "Local adapter"):
        text = " ".join(flat(section(heading)))

        assert "`sweep.test_loopback: true`" in text, heading
        assert "private loopback" in text, heading


def test_the_local_adapter_is_said_to_count_as_a_runner():
    """cm-2qp.7 redo, edge cases: a laptop cron sweep alongside a hosted
    runner overlaps it (the local lock is in the checkout, a hosted run's on
    its VM) and its ceiling misses hosted spend, so both "one runner per
    repository" and the Local adapter say the local adapter counts."""
    status = " ".join(flat(section("Current status")))
    local = " ".join(flat(section("Local adapter")))

    assert "The local adapter counts as a runner" in status
    assert "remove any cron or launchd entry from an earlier setup" in status
    assert "The local adapter counts as a runner" in local
    assert "remove any cron or launchd entry from an earlier setup" in local
    assert "only by hand while no hosted run is going" in local
    assert "the local ledger never sees hosted spend" in local


def test_install_says_github_hosted_runners_only():
    """config.resolve_runner refuses `hosted` on a GitHub runner whose
    RUNNER_ENVIRONMENT is not `github-hosted` (Reason.RUNNER, exit 3), and
    the workflow shows exit 3 as a green job with a warning."""
    install = " ".join(flat(section("Install")))

    assert "GitHub-hosted runners only" in install
    assert "self-hosted runner" in install and "GitHub Enterprise Server" in install
    assert "refused with reason `runner`" in install
    assert "still shows green, with a warning" in install
