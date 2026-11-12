"""cm-2qp.7 (E14.7, docs/direction-platform-first.md §3, §5 R8 and §8 (5)):
the README's "Current status" and "Guardrails" are written against the
runner-kinds table.

What is pinned is what the bead and the user's instruction name: the
sentence the direction retires ("nothing is scheduled for you yet") and the
removed command (`/curamoir:schedule`, R1) stay out; the README links the
direction doc; the boundary table has a column per runner kind and every
row; and the "Local adapter" subsection says plainly that the local agent's
own `claude -p` is not OS-sandboxed (user decision 2026-10-29, cm-xy9.8
parked). The command table's rows are test_routine_command.py's.

How the security statements are pinned (user decision 2026-11-12, after the
provenance review of cm-2qp.7, round 2): the boundary table is the boundary
contract, so every cell of it, in both columns, is pinned exactly
(whitespace-normalised) against the literal table in BOUNDARY_CELLS below.
Every security statement outside the table is pinned as a whole sentence or
clause, up to and including its closing punctuation. Round 1 had pinned a
key phrase per claim instead, and a negation appended to a phrase ("The
supervisor lock does not hold here", "so you need not protect it") or put
inside it ("This is not enforced in code") still passed. Whole cells and
whole sentences fail on those, so rewording a security statement means
rewording its test: that is the point.

Where a README fact has a source in the code, that part of the expected text
is built from it, so a change there fails here until the README follows:

- the withheld token names: fix_step.HOSTED_RUN_TOKEN_VARIABLES, as a set;
- the agents' tools: tier1.SCAN_TOOLS, fix_step.FIX_TOOLS and
  HOSTED_FIX_TOOLS, and "git denied" from fix_step.HOSTED_BASH_DENY_RULES;
- the workflow's state branch: state_branch.DEFAULT_BRANCH (and the
  workflow template's default); the Routine's: the `state push --branch`
  line of templates/routine/sweep-prompt.md, which is where that name
  lives;
- the runner declaration's variable: config.RUNNER_VARIABLE, and the
  refusal of an unmarked `hosted`: config.resolve_runner, called;
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


def prose(heading):
    """The section titled `heading`, as one `flat()` line."""
    return " ".join(flat(section(heading)))


def assert_whole(text, *sentences):
    """Each of `sentences` is in `text` whole: a full sentence or clause,
    ending at its punctuation, so a word put inside it fails."""
    for sentence in sentences:
        assert re.search(r"[.:;!?)*]$", sentence), f"not a whole clause: {sentence!r}"
        assert sentence in text, sentence


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


def test_assert_whole_refuses_a_fragment():
    with pytest.raises(AssertionError, match="not a whole clause"):
        assert_whole("so protect it, as the recipe says.", "so protect it")


def test_the_readme_no_longer_says_nothing_is_scheduled():
    assert "nothing is scheduled for you yet" not in " ".join(flat(readme())).lower()


def test_the_readme_never_names_the_removed_schedule_command():
    assert "/curamoir:schedule" not in readme()


def test_current_status_links_the_direction_doc():
    assert f"]({DIRECTION})" in section("Current status")
    assert (ROOT / DIRECTION).is_file()


def test_current_status_names_both_hosted_runners_and_one_per_repository():
    assert_whole(
        prose("Current status"),
        "The sweep is scheduled on a hosted runner, a disposable VM: the GitHub Actions"
        " workflow `/curamoir:init` offers to write, or a Claude Code Routine whose prompt"
        " and `/schedule` steps `/curamoir:routine` shows.",
        "Use one runner per repository: the two share no lock and no ledger.",
    )


def test_the_boundary_table_has_a_column_per_runner():
    boundary = section("Where the security boundary is")
    header = next(line for line in boundary.splitlines() if line.startswith("| |"))

    assert "Hosted" in header and "Local" in header
    rows = boundary_rows()
    for row, _, _ in BOUNDARY_CELLS:
        assert row in rows, row
        cells = rows[row]
        assert len(cells) == 3 and all(cells), (row, cells)


def test_the_hosted_kill_switch_names_the_default_branch():
    boundary = section("Where the security boundary is")
    kill_switch = next(line for line in boundary.splitlines() if line.startswith("| Kill switch |"))

    assert "`.curamoir/PAUSE` committed to the default branch" in kill_switch


def test_the_local_adapter_says_its_agent_is_not_os_sandboxed():
    assert_whole(prose("Local adapter"),
                 "**The agent's own `claude -p` is not OS-sandboxed.** It runs as you.")


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
    guardrails = prose("Guardrails")

    assert "never loads" not in guardrails
    assert_whole(
        guardrails,
        "**The repository's own Claude Code configuration never reaches the scan or the"
        " fix agent.** Both run Claude Code in restricted mode, so the clone's settings,"
        " hooks and plugins are not read; the command guard above comes back through"
        " cúramóir's own settings.",
    )
    assert_whole(
        prose("Hosted runner limits"),
        "On a Routine, the session that runs the sweep is itself a Claude Code session in"
        " a clone of your repository, so it loads the default branch's `CLAUDE.md`,"
        " settings and hooks, and they run with the session's GitHub access and Claude"
        " credentials.",
        "The prompt tells it to ignore the repository's instructions, but that is a"
        " prompt, not a fence: a hook still runs.",
    )


def test_the_runner_declaration_is_said_to_check_markers_not_prove_the_runner():
    """config.resolve_runner accepts `hosted` on the runners' environment
    markers alone, which a laptop could set."""
    boundary = prose("Where the security boundary is")

    assert "refused anywhere else" not in boundary
    assert_whole(boundary,
                 "The check catches a mistaken declaration; it does not prove where the run is.")


def test_the_local_test_network_row_says_which_platform_gets_loopback():
    """os_sandbox: bwrap's --unshare-net always gives a private loopback;
    `sweep.test_loopback` gates loopback on macOS only. The table row
    (pinned whole in BOUNDARY_CELLS) and the Local adapter's bullet both
    say so."""
    assert BOUNDARY_CELLS[4][2] == (
        "Run in the OS sandbox, with no network: on macOS not even loopback unless"
        " `sweep.test_loopback: true`, on Linux a private loopback only")
    assert_whole(
        prose("Local adapter"),
        "have no network (on macOS not even loopback unless `sweep.test_loopback: true`"
        " is committed, for a suite that starts a local server; on Linux a private"
        " loopback only);",
    )


def test_the_local_adapter_is_said_to_count_as_a_runner():
    """cm-2qp.7 redo, edge cases: a laptop cron sweep alongside a hosted
    runner overlaps it (the local lock is in the checkout, a hosted run's on
    its VM) and its ceiling misses hosted spend, so both "one runner per
    repository" and the Local adapter say the local adapter counts."""
    assert_whole(
        prose("Current status"),
        "The local adapter counts as a runner too: with a hosted runner, remove any cron"
        " or launchd entry from an earlier setup (see [Local adapter](#local-adapter)).",
    )
    assert_whole(
        prose("Local adapter"),
        "The local adapter counts as a runner.",
        "On a repository with a hosted runner, remove any cron or launchd entry from an"
        " earlier setup, and run a local sweep only by hand while no hosted run is going:"
        " the local lock lives in your checkout and a hosted run's on its VM, so neither"
        " sees the other, and the local ledger never sees hosted spend.",
    )


def test_install_says_github_hosted_runners_only():
    """config.resolve_runner refuses `hosted` on a GitHub runner whose
    RUNNER_ENVIRONMENT is not `github-hosted` (Reason.RUNNER, exit 3), and
    the workflow shows exit 3 as a green job with a warning."""
    assert_whole(
        prose("Install"),
        "It runs on GitHub-hosted runners only: on a self-hosted runner, or on GitHub"
        " Enterprise Server, the sweep is refused with reason `runner`, and the job still"
        " shows green, with a warning.",
        "Run the local adapter there instead.",
    )


# --- The boundary table: pinned exactly, as the boundary contract -----------

TEMPLATES = ROOT / "templates"


def boundary_rows():
    """The boundary table's rows, `{row name: [name, hosted, local]}`, each
    cell whitespace-normalised."""
    boundary = section("Where the security boundary is")
    return {cells[0]: cells for line in boundary.splitlines() if line.startswith("| ")
            for cells in [[" ".join(cell.split()) for cell in line.strip().strip("|").split("|")]]}


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
    """`Read, Grep, Glob`: a tool list as the table writes it."""
    return ", ".join(names)


def _and(names):
    """`a`, `a and b`, `a, b and c`."""
    names = list(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


SCAN = _tools(tier1.SCAN_TOOLS)

#: The hosted fix agent's tools other than Bash, and what the cell says of
#: Bash: limited if HOSTED_FIX_TOOLS has it, absent otherwise.
HOSTED_FIX = _tools(tool for tool in fix_step.HOSTED_FIX_TOOLS if tool != "Bash")
HOSTED_BASH = (
    "Bash limited to the repository's own test command and Claude Code's built-in"
    " read-only commands (no Bash at all if that command cannot be named safely)"
    if "Bash" in fix_step.HOSTED_FIX_TOOLS else "no Bash")

#: The local fix agent's tools: "those" (the scan's) and the rest of
#: FIX_TOOLS, which holds only while FIX_TOOLS includes every scan tool.
LOCAL_FIX = (
    f"those and {_and(t for t in fix_step.FIX_TOOLS if t not in tier1.SCAN_TOOLS)}"
    if set(tier1.SCAN_TOOLS) < set(fix_step.FIX_TOOLS) else _tools(fix_step.FIX_TOOLS))

#: (row, Hosted cell, Local cell): the whole boundary table, as the contract.
BOUNDARY_CELLS = [
    ("Process boundary",
     "The VM, discarded after the run",
     "An OS sandbox (`sandbox-exec`, `bwrap`) around your tests, self-tested before"
     " every gate; missing or failing, the sweep refuses"),
    ("Your checkout",
     "Not on the VM",
     "Out of reach: the work happens in a throwaway clone"),
    ("Credentials at risk",
     "What the run holds: on Actions its `GITHUB_TOKEN`, scoped by the workflow's"
     " `permissions:`; on a Routine the session's GitHub access, which acts as you; and,"
     " on both, the run's model credentials",
     "Your home directory"),
    ("Agents' tools",
     f"Scan: {SCAN}. Fix: {HOSTED_FIX}; {HOSTED_BASH}; {_and(denied_commands())} denied",
     f"Scan: {SCAN}. Fix: {LOCAL_FIX}; no shell"),
    ("Your tests",
     "Run unsandboxed in the clone, with the VM's network",
     "Run in the OS sandbox, with no network: on macOS not even loopback unless"
     " `sweep.test_loopback: true`, on Linux a private loopback only"),
    ("One sweep at a time",
     "On Actions, the workflow's concurrency group queues a second run. On a Routine"
     " nothing does: each run's lock is on its own VM, so a scheduled run and **Run now**"
     " can overlap; don't start a run while one is going",
     "The supervisor lock ([Local adapter](#local-adapter))"),
    ("Kill switch",
     "The label, or a `.curamoir/PAUSE` committed to the default branch (a run started"
     " by hand on another branch reads that branch's)",
     "The label, or `.curamoir/PAUSE` in your checkout"),
    ("Spend",
     "The ledger records tokens; GitHub Actions bills the API key, a Routine your Claude"
     " usage. The monthly ceiling does not yet see earlier hosted runs, so it does not"
     " stop them",
     "The ledger prices tokens against the monthly ceiling"),
    ("Memory between runs",
     "Only what is committed. Ledger lines go to a state branch, never the default"
     f" branch. Workflow: `{state_branch.DEFAULT_BRANCH}` (its `CURAMOIR_STATE_BRANCH`"
     f" variable can name another). Routine: `{routine_state_branch()}` (the prompt's"
     " own name, so the two runners' ledgers never mix)",
     "The checkout"),
]


def test_the_boundary_cells_cover_every_row():
    assert [row for row, _, _ in BOUNDARY_CELLS] == [
        name for name in boundary_rows() if name not in ("", "---")]


@pytest.mark.parametrize(("row", "hosted", "local"), BOUNDARY_CELLS,
                         ids=[row for row, _, _ in BOUNDARY_CELLS])
def test_each_boundary_cell_says_what_its_runner_holds(row, hosted, local):
    _, hosted_cell, local_cell = boundary_rows()[row]

    assert hosted_cell == hosted, (row, "Hosted")
    assert local_cell == local, (row, "Local")


def test_the_two_state_branches_are_the_code_s_and_differ():
    workflow = (TEMPLATES / "github" / "curamoir-sweep.yml").read_text(encoding="utf-8")

    assert f"vars.CURAMOIR_STATE_BRANCH || '{state_branch.DEFAULT_BRANCH}'" in workflow
    assert routine_state_branch() != state_branch.DEFAULT_BRANCH


# --- Security prose: whole sentences --------------------------------------


def test_the_guardrails_keep_their_bullets_and_per_runner_caveats():
    guardrails = " ".join(flat(section("Guardrails").split("### ")[0]))

    assert_whole(
        guardrails,
        "These are built for every runner; where a runner weakens one, the bullet and the"
        " table below say so:",
        "**It never writes to your default branch.** Work goes to `curamoir/*` branches"
        " and arrives as a PR.",
        "This is enforced in code, not in a prompt.",
        "**One sweep at a time**, where the runner can enforce it.",
        "A Routine cannot (see the table's One sweep at a time row).",
        "**A kill switch.** The `curamoir:paused` label, or a `.curamoir/PAUSE` file,"
        " stops every scheduled job.",
        "A label that cannot be read does not pause: the run goes ahead, and its ledger"
        " line says the label was not checked.",
        "**A spend ceiling.** Month-to-date cost is computed from the ledger before each"
        " run.",
        "If a run would overspend, it refuses to start.",
        "It does not yet hold on a hosted runner (the table's Spend row).",
        "This guard catches an agent's honest mistakes; it is not what keeps a hostile"
        " agent in (see below).",
        "**An open-PR cap.** You will never come back to a wall of PRs.",
        "**It works in a throwaway clone, never your checkout.**",
        "Only the reviewed patch of the files in scope leaves the clone; anything else the"
        " agent touched is put back or refuses the run.",
        "A Routine is the exception for its own session: see [Hosted runner"
        " limits](#hosted-runner-limits).",
        "**Redaction** on everything it writes.",
    )


def test_an_unmarked_hosted_declaration_is_said_to_be_refused():
    """config.resolve_runner refuses `hosted` with no marker set."""
    with pytest.raises(config.RunnerDeclarationError):
        config.resolve_runner({config.RUNNER_VARIABLE: "hosted"})

    assert_whole(
        prose("Where the security boundary is"),
        "Which runner a run is on is declared, never guessed:"
        f" `{config.RUNNER_VARIABLE}=hosted`, which the workflow and the Routine prompt"
        " set, is accepted only where a GitHub-hosted Actions runner's or a Claude Code"
        " cloud session's own environment markers are set, and refused where they are"
        " absent.",
        "Unset, the run is local.",
    )


def test_the_hosted_runner_limits_name_the_open_gap():
    assert_whole(
        prose("Hosted runner limits"),
        "The fix agent's prompt on a hosted runner forbids git, `bd`, `gh`, installs and"
        " the network, but the prompt is not the fence: a git command that writes the"
        " clone's `.git` (or moves HEAD) refuses the whole run, and a stray file is"
        " dropped from the patch.",
        "Past that, the boundary is the disposable VM and the scope of the run's token,"
        " nothing finer.",
        "Your tests run there unsandboxed, as the runner's user.",
        "The fix agent's process is not given the run's GitHub and Actions tokens, but the"
        " fix agent's Bash, and code the tests run, can still read what the sweep holds"
        " (on Actions, the run's `GITHUB_TOKEN` and `ANTHROPIC_API_KEY`), and a process"
        " either leaves behind can reach the run's later steps, the PR included.",
        "This is not solved yet.",
        "Keep the workflow's `permissions:` as written.",
        "Add no other secret to the sweep step's `env:`.",
        "The agent is kept from these names only, so a secret under any other name"
        " reaches it:",
        "Only what is on the default branch loads, so protect it, as the Routine recipe"
        " says.",
    )


def test_the_withheld_token_names_are_fix_steps_exactly():
    limits = prose("Hosted runner limits")
    _, listed = limits.split("a secret under any other name reaches it:", 1)
    listed = listed.split(". ", 1)[0]

    assert set(re.findall(r"`([A-Z_]+)`", listed)) == fix_step.HOSTED_RUN_TOKEN_VARIABLES


def test_the_local_adapter_carries_the_moved_prose():
    """R8: the stale-lock and CURAMOIR_JOB prose arrives here, not just
    leaves Known limits."""
    assert_whole(
        prose("Local adapter"),
        "**Your tests run OS-sandboxed**, under `sandbox-exec` (macOS) or `bwrap` (Linux).",
        "They can write only inside the clone and their own temporary directory;",
        "cannot read your home directory beyond what the run needs (the reused venv and"
        " interpreter, PATH, a version manager's installed versions; never a credential"
        " directory such as `~/.ssh` or `~/.config`);",
        "and reach no daemon's socket and, on macOS, no system service that acts outside"
        " the sandbox (`open`, `launchctl`, AppleEvents, DNS).",
        "Where the sandbox is missing or fails its self-test, the sweep refuses rather"
        " than run the tests unconfined.",
        "On macOS the tests can still read files outside your home directory.",
        "**No local job's agent has a shell.** Any tool beyond those in the table, Bash"
        " above all, is refused when the job is set up; only a hosted runner's fix agent"
        " gets one.",
        "With no shell, a file the agent writes is never run by it; the tests run it only"
        " inside the OS sandbox.",
        "Its reads, and any edit it attempts outside the clone, are fenced by Claude Code's"
        " permission rules (scoped to the clone, with every other path denied), which"
        " Claude Code applies to Grep and Glob on a best-effort basis.",
        "Nothing outside the clone is reviewed by the sweep.",
        "**The supervisor lock holds one job per repository.** A killed job can leave its"
        " lock behind.",
        "Because the lock is per repository, while a job runs every session in that"
        " repository is treated as inside it, yours included, and the command guard is on"
        " there.",
        "A stale lock, left by a job that was killed, is reported, never overridden, and"
        " keeps the guard on in your own sessions until you clear it; the denial says so,"
        " and `/curamoir:status` and `/curamoir:doctor` say whether the lock looks stale.",
        f"**`{job_guard.JOB_VARIABLE}` is the guard's other signal.** `curamoir run` sets"
        " it for the job and everything it starts; in a clone, where there is no lock, it"
        " is the only one, so anything that drops it from a process's environment stops"
        " the guard there.",
        "Any non-empty value counts, `0` and `false` included, so do not export it in your"
        " own shell.",
    )


def test_current_status_and_install_keep_their_caveats():
    assert_whole(prose("Current status"),
                 "Neither hosted runner has yet been exercised end to end.")
    assert_whole(
        prose("Install"),
        "Without one, a local sweep refuses to run your tests on its fixes, so it opens"
        " no PR; other platforms are not supported.",
        "A hosted runner needs none: the VM is the boundary there.",
    )


def test_cost_states_the_configs_ceiling_and_who_pays():
    ceiling = config.DEFAULTS["budget"]["monthly_ceiling_usd"]

    assert_whole(
        prose("Cost"),
        "The GitHub Actions workflow costs API money: its model calls are billed per token"
        " to the `ANTHROPIC_API_KEY` it is given.",
        "(Whether a Claude subscription may be used unattended there is an open question,"
        " so the workflow does not wire it.)",
        "A Routine runs as you and counts against your Claude usage, as does a sweep on"
        " your own machine.",
        "The ledger records every run's tokens.",
        # An estimate with no source in the code: pinned against itself.
        "The current estimate for the workflow on a small repo is roughly"
        f" $4\N{EN DASH}5 a month against a default ${ceiling} ceiling.",
        "That figure is an estimate, not a measurement.",
    )
