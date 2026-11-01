"""cm-2qp.8 (E14.8, docs/direction-platform-first.md §7): docs/runners.md,
the two seams another provider plugs into, with the GitHub Actions workflow
as the worked example and one paragraph each for Cursor and Codex.

What is pinned is what can drift from the code, not the prose: the
acceptance rule for CURAMOIR_RUNNER=hosted is spelled with config's own
constants (so a marker renamed in config fails here until the page says
so); the run_fix_step obligations name fix_step's own constants; every
YAML excerpt the page quotes is still in templates/github/curamoir-sweep.yml
(the template stays the single source, and an excerpt it no longer holds is
a stale page); the exit codes are runner's and state_branch's; and the
Routine environment section stays the marked place cm-2qp.10 fills. The
user's instruction says "No adapter code", so the page carries no Python.
"""

import re
from pathlib import Path

from curamoir import config, fix_step, runner, state_branch

ROOT = Path(__file__).resolve().parents[2]
RUNNERS = ROOT / "docs" / "runners.md"
TEMPLATE = ROOT / "templates" / "github" / "curamoir-sweep.yml"
README = ROOT / "README.md"
COMMAND = ROOT / "commands" / "routine.md"
DIRECTION = ROOT / "docs" / "direction-platform-first.md"


def page():
    return RUNNERS.read_text(encoding="utf-8")


def flat(text):
    """`text` as one line: the page wraps at about 76 columns."""
    return " ".join(text.split())


def section(heading, text=None):
    """The body of the `## `/`### ` section titled `heading`, up to the next
    heading of the same or a higher level."""
    text = page() if text is None else text
    match = re.search(rf"^(#+) {re.escape(heading)}\n", text, re.MULTILINE)
    assert match, f"docs/runners.md has no {heading!r} heading"
    level = len(match.group(1))
    rest = text[match.end():]
    end = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return rest[:end.start()] if end else rest


def fences(text, language):
    return re.findall(rf"^```{language}\n(.*?)^```", text, flags=re.DOTALL | re.MULTILINE)


def lines_without_indent(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_the_runners_page_exists():
    assert RUNNERS.is_file()


def test_it_names_both_seams():
    assert section("Seam A: the scheduler")
    assert section("Seam B: the agent runner")


def test_seam_a_states_the_acceptance_rule_with_configs_own_names():
    """config.resolve_runner's rule, exactly: CLAUDE_CODE_REMOTE=true, or
    GITHUB_ACTIONS=true with RUNNER_ENVIRONMENT=github-hosted."""
    seam = section("Seam A: the scheduler")

    assert f"`{config.RUNNER_VARIABLE}=hosted`" in seam
    assert f"`{config.CLAUDE_MARKER}=true`" in seam
    assert f"`{config.GITHUB_MARKER}=true`" in seam
    assert f"`{config.RUNNER_ENVIRONMENT_VARIABLE}={config.GITHUB_HOSTED}`" in seam
    text = flat(seam)
    assert "never echoed" in text
    assert "a marker without the declaration runs local" in text.lower()


def test_seam_a_says_a_new_marker_is_a_code_change_in_config():
    text = flat(section("Seam A: the scheduler"))

    assert "config.resolve_runner" in text
    assert "a code change, not a documentation change" in text


def test_seam_a_gives_the_runner_and_state_push_exit_codes():
    text = flat(section("Seam A: the scheduler"))

    assert "curamoir run <job>" in text
    assert "curamoir state push --branch" in text
    assert f"exit {runner.EXIT_REFUSED}" in text
    for code in state_branch.EXIT_CODES.values():
        assert f"`{code}`" in text, code
    assert "It skipped" in text


def test_seam_b_names_the_obligations_fix_step_carries_today():
    """The contract's In / Must / Out / Not its job, plus what a non-Claude
    CLI must meet that lives inside fix_step (carry-over from E14.3)."""
    text = flat(section("Seam B: the agent runner"))

    for part in ("**In.**", "**Must.**", "**Out.**", "**Not its job.**"):
        assert part in text, part
    for tool in fix_step.SHELL_FREE_TOOLS | fix_step.HOSTED_SHELL_TOOLS:
        assert tool in text, tool
    assert "cm-xq9" in text
    assert "git_state" in text
    assert "`usage`" in text and "`total_cost_usd`" in text
    assert "max_turns" in text
    assert "HOSTED_TEST_ENVIRONMENT" in text
    assert "HOSTED_RUN_TOKEN_VARIABLES" in text
    assert "CURAMOIR_JOB" in text and "CLAUDE_PLUGIN_ROOT" in text
    # What a FixStepRun carries besides the payload, and the pricing caveat.
    assert "`fix_step.NOT_STARTED`" in text and "`--model <model>`" in text
    assert "`test_command`" in text
    assert "`budget.prices`" in text and "NOT enforced" in text
    for field in ("`is_error`", "`subtype`", "`result`", "`total_cost_usd`"):
        assert f"| {field} |" in text, field


def test_the_page_and_fix_steps_docstring_agree_on_the_contract():
    """The additions the page makes to "Out" and "In" are in the source too."""
    contract = flat(fix_step.__doc__.split("**The run_fix_step contract**", 1)[1]
                    .split("tests/test_fix_step.py pins", 1)[0])

    for name in ("`test_command`", "NOT_STARTED", "`git_state`", "`--model <model>`",
                 "`usage`", "`total_cost_usd`"):
        assert name in contract, name


def test_every_yaml_excerpt_is_still_in_the_template():
    """Each fenced ```yaml block is a run of consecutive lines of the
    template, compared with indentation stripped (an excerpt is shown
    dedented). An excerpt the template no longer holds fails here."""
    template = lines_without_indent(TEMPLATE.read_text(encoding="utf-8"))
    excerpts = fences(page(), "yaml")

    assert len(excerpts) >= 5
    for excerpt in excerpts:
        wanted = lines_without_indent(excerpt)
        assert wanted, "an empty yaml excerpt"
        assert any(template[start:start + len(wanted)] == wanted
                   for start in range(len(template) - len(wanted) + 1)), excerpt


def test_the_worked_example_walks_the_template():
    text = flat(section("The worked example: GitHub Actions"))

    for point in ("templates/github/curamoir-sweep.yml", "CURAMOIR_SHA",
                  "persist-credentials: false", "gh auth setup-git",
                  "enable-cache: false", "if: always()", "CLAUDE_CODE_VERSION",
                  "AST_GREP_VERSION", "ANTHROPIC_API_KEY", "CURAMOIR_REF",
                  "github-actions[bot]"):
        assert point in text, point


def test_there_is_no_adapter_code():
    assert not fences(page(), "python")
    assert "def " not in page()


def test_it_has_the_routine_cursor_and_codex_sections():
    routine = flat(section("Claude Code Routine"))
    assert "templates/routine/sweep-prompt.md" in routine
    assert "claude/curamoir-state" in routine
    assert "Run now" in routine
    assert "it also needs" in flat(section("Cursor"))
    assert "the local adapter" in flat(section("Codex"))


def test_the_routine_environment_section_is_the_place_cm_2qp_10_fills():
    """A subsection of the Routine section, so its anchor stays
    #routine-environment whatever the Routine section's own text."""
    environment = section("Routine environment")

    assert "### Routine environment\n" in section("Claude Code Routine")
    assert "<!-- cm-2qp.10" in environment
    assert "cm-2qp.10" in COMMAND.read_text(encoding="utf-8")


def test_operator_text_names_no_bead_ids_outside_comments():
    """A reader setting up a runner gets the reason, not a tracker id."""
    visible = re.sub(r"<!--.*?-->", "", page(), flags=re.DOTALL)

    for bead in ("cm-2qp.10", "cm-2qp.11"):
        assert bead not in visible, bead


def test_it_opens_with_which_section_is_for_whom():
    opening = page().split("## Seam A", 1)[0]

    for anchor in ("#the-worked-example-github-actions", "#claude-code-routine",
                   "#seam-a-the-scheduler", "#seam-b-the-agent-runner"):
        assert f"]({anchor})" in opening, anchor


def test_seam_a_lists_what_else_the_vm_needs():
    text = flat(section("Seam A: the scheduler"))

    assert "It also needs" in text
    assert f"exit {runner.EXIT_UNUSABLE_ROOT}" in text


def test_the_readme_and_the_routine_command_point_at_the_page():
    assert "](docs/runners.md)" in README.read_text(encoding="utf-8")
    assert "docs/runners.md" in section("3. The cloud environment",
                                        COMMAND.read_text(encoding="utf-8"))


def test_the_direction_doc_no_longer_says_the_fix_step_returns_a_diff():
    """§7's seam B: the edits stay uncommitted in the clone's work tree and
    the caller reads the diff; only the payload is returned."""
    text = flat(DIRECTION.read_text(encoding="utf-8"))

    assert "return a diff and token counts" not in text
    assert "docs/runners.md" in text
