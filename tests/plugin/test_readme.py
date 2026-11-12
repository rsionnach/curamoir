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

The provenance review (cm-2qp.7, round 1) added a key phrase per claim
that a one-word edit could invert or drop: every cell of the boundary
table, in both columns; the Guardrails bullets and their per-runner
caveats; the runner check's refusal; the Hosted runner limits disclosure;
the Local adapter's sandbox, lock and CURAMOIR_JOB prose (R8, moved there
from Known limits); the Current status and Install caveats; and Cost.
Where a README fact has a source in the code, the expected text is built
from it, so a change there fails here until the README follows:

- the withheld token names: fix_step.HOSTED_RUN_TOKEN_VARIABLES, as a set;
- the agents' tools: tier1.SCAN_TOOLS, fix_step.FIX_TOOLS and
  HOSTED_FIX_TOOLS, and "git denied" from fix_step.HOSTED_BASH_DENY_RULES;
- the workflow's state branch: state_branch.DEFAULT_BRANCH (and the
  workflow template's default); the Routine's: the `state push --branch`
  line of templates/routine/sweep-prompt.md, which is where that name
  lives;
- the refusal of an unmarked `hosted`: config.resolve_runner, called;
- the CURAMOIR_JOB name: job_guard.JOB_VARIABLE;
- the "$15" ceiling: config.DEFAULTS' budget.monthly_ceiling_usd.

The "$4\N{EN DASH}5 a month" figure is an estimate with no source in the code; it is
pinned against itself.
"""

import re
from pathlib import Path

import pytest

from curamoir import config, fix_step, job_guard, state_branch, tier1

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


# --- cm-2qp.7 provenance round 1: a key phrase per claim --------------------

TEMPLATES = ROOT / "templates"


def boundary_rows():
    """The boundary table's rows, `{row name: [name, hosted, local]}`."""
    boundary = section("Where the security boundary is")
    return {cells[0]: cells for line in boundary.splitlines() if line.startswith("| ")
            for cells in [[cell.strip() for cell in line.strip().strip("|").split("|")]]}


def routine_state_branch():
    """The Routine's state branch, as the Routine prompt pushes to it: the
    name has no Python constant, and the prompt is where it lives."""
    prompt = (TEMPLATES / "routine" / "sweep-prompt.md").read_text(encoding="utf-8")
    found = re.findall(r"state push --branch (\S+)", prompt)
    assert len(found) == 1, found
    return found[0]


def denied_commands():
    """The command names fix_step.HOSTED_BASH_DENY_RULES deny, `Bash(git *)`
    giving `git`."""
    names = [re.fullmatch(r"Bash\((\S+) \*\)", rule).group(1)
             for rule in fix_step.HOSTED_BASH_DENY_RULES]
    assert names, fix_step.HOSTED_BASH_DENY_RULES
    return names


def _tools(names):
    return ", ".join(names)


HOSTED_FIX_WITHOUT_BASH = [tool for tool in fix_step.HOSTED_FIX_TOOLS if tool != "Bash"]

#: (row, [hosted phrases], [local phrases]). Each phrase is the one the
#: provenance round's mutant of that cell removed or inverted.
BOUNDARY_CELLS = [
    ("Process boundary",
     ["The VM, discarded after the run"],
     ["An OS sandbox", "self-tested before every gate",
      "missing or failing, the sweep refuses"]),
    ("Your checkout",
     ["Not on the VM"],
     ["Out of reach", "a throwaway clone"]),
    ("Credentials at risk",
     ["its `GITHUB_TOKEN`, scoped by the workflow's `permissions:`",
      "the session's GitHub access, which acts as you", "the run's model credentials"],
     ["Your home directory"]),
    ("Agents' tools",
     [f"Scan: {_tools(tier1.SCAN_TOOLS)}.",
      f"Fix: {_tools(HOSTED_FIX_WITHOUT_BASH)}; Bash limited to the repository's own test command",
      *(f"{name} denied" for name in denied_commands())],
     [f"Scan: {_tools(tier1.SCAN_TOOLS)}.", "no shell"]),
    ("Your tests",
     ["Run unsandboxed in the clone", "with the VM's network"],
     ["Run in the OS sandbox, with no network"]),
    ("One sweep at a time",
     ["the workflow's concurrency group queues a second run",
      "On a Routine nothing does", "can overlap"],
     ["The supervisor lock"]),
    ("Kill switch",
     ["`.curamoir/PAUSE` committed to the default branch"],
     ["The label, or `.curamoir/PAUSE` in your checkout"]),
    ("Spend",
     ["GitHub Actions bills the API key, a Routine your Claude usage",
      "does not yet see earlier hosted runs", "so it does not stop them"],
     ["prices tokens against the monthly ceiling"]),
    ("Memory between runs",
     ["Only what is committed", "never the default branch",
      f"Workflow: `{state_branch.DEFAULT_BRANCH}`",
      f"Routine: `{routine_state_branch()}`"],
     ["The checkout"]),
]


def test_the_boundary_cells_cover_every_row():
    assert [row for row, _, _ in BOUNDARY_CELLS] == [
        name for name in boundary_rows() if name not in ("", "---")]


@pytest.mark.parametrize(("row", "hosted", "local"), BOUNDARY_CELLS,
                         ids=[row for row, _, _ in BOUNDARY_CELLS])
def test_each_boundary_cell_says_what_its_runner_holds(row, hosted, local):
    _, hosted_cell, local_cell = boundary_rows()[row]

    for phrase in hosted:
        assert phrase in hosted_cell, (row, "Hosted", phrase)
    for phrase in local:
        assert phrase in local_cell, (row, "Local", phrase)


def test_the_two_state_branches_are_the_code_s_and_differ():
    workflow = (TEMPLATES / "github" / "curamoir-sweep.yml").read_text(encoding="utf-8")

    assert f"vars.CURAMOIR_STATE_BRANCH || '{state_branch.DEFAULT_BRANCH}'" in workflow
    assert routine_state_branch() != state_branch.DEFAULT_BRANCH


def test_the_guardrails_keep_their_bullets_and_per_runner_caveats():
    guardrails = " ".join(flat(section("Guardrails").split("### ")[0]))

    for phrase in ("It never writes to your default branch",
                   "enforced in code, not in a prompt",
                   "A Routine cannot",
                   "A label that cannot be read does not pause",
                   "the label was not checked",
                   "it refuses to start",
                   "It does not yet hold on a hosted runner",
                   "it is not what keeps a hostile agent in",
                   "An open-PR cap",
                   "It works in a throwaway clone, never your checkout",
                   "A Routine is the exception for its own session",
                   "[Hosted runner limits](#hosted-runner-limits)",
                   "**Redaction** on everything it writes"):
        assert phrase in guardrails, phrase


def test_an_unmarked_hosted_declaration_is_said_to_be_refused():
    """config.resolve_runner refuses `hosted` with no marker set."""
    with pytest.raises(config.RunnerDeclarationError):
        config.resolve_runner({config.RUNNER_VARIABLE: "hosted"})
    boundary = " ".join(flat(section("Where the security boundary is")))

    assert "refused where they are absent" in boundary
    assert "accepted only where" in boundary
    assert "Unset, the run is local" in boundary


def test_the_hosted_runner_limits_name_the_open_gap():
    limits = " ".join(flat(section("Hosted runner limits")))

    for phrase in ("the prompt is not the fence",
                   "Your tests run there unsandboxed",
                   "This is not solved yet",
                   "Keep the workflow's `permissions:` as written",
                   "Add no other secret to the sweep step's `env:`",
                   "a secret under any other name reaches it",
                   "protect it"):
        assert phrase in limits, phrase


def test_the_withheld_token_names_are_fix_steps_exactly():
    limits = " ".join(flat(section("Hosted runner limits")))
    _, listed = limits.split("a secret under any other name reaches it:", 1)
    listed = listed.split(". ", 1)[0]

    assert set(re.findall(r"`([A-Z_]+)`", listed)) == fix_step.HOSTED_RUN_TOKEN_VARIABLES


def test_the_local_adapter_carries_the_moved_prose():
    """R8: the stale-lock and CURAMOIR_JOB prose arrives here, not just
    leaves Known limits."""
    local = " ".join(flat(section("Local adapter")))

    for phrase in ("Where the sandbox is missing or fails its self-test, the sweep refuses",
                   "never a credential directory such as `~/.ssh` or `~/.config`",
                   "No local job's agent has a shell",
                   "The supervisor lock holds one job per repository",
                   "A stale lock",
                   "is reported, never overridden",
                   f"`{job_guard.JOB_VARIABLE}` is the guard's other signal",
                   "`0` and `false` included, so do not export it"):
        assert phrase in local, phrase


def test_current_status_and_install_keep_their_caveats():
    status = " ".join(flat(section("Current status")))
    install = " ".join(flat(section("Install")))

    assert "Neither hosted runner has yet been exercised end to end" in status
    assert "a local sweep refuses to run your tests on its fixes, so it opens no PR" in install
    assert "A hosted runner needs none: the VM is the boundary there" in install


def test_cost_states_the_configs_ceiling_and_who_pays():
    cost = " ".join(flat(section("Cost")))
    ceiling = config.DEFAULTS["budget"]["monthly_ceiling_usd"]

    assert f"default ${ceiling} ceiling" in cost
    assert "billed per token to the `ANTHROPIC_API_KEY`" in cost
    assert "A Routine runs as you and counts against your Claude usage" in cost
    # An estimate with no source in the code: pinned against itself.
    assert "roughly $4\N{EN DASH}5 a month" in cost
    assert "That figure is an estimate, not a measurement" in cost
