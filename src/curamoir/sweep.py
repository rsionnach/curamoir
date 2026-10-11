"""E3-R1, first clause: the sweep runs `curamoir-lint` first and treats its
output as the work list.

This module is the deterministic half of that sentence: it runs the lint,
decides whether the report can be acted on, and orders what it found. The
judgement half — fixing — belongs to the cheap-tier agent driven by
`skills/sweep/SKILL.md`, which reads this module's output and nothing else.

**Run as a subprocess, not in-process.** The spec says "run `curamoir-lint`",
and the standalone runner, its exit code and its JSON are the contract
(docs/curamoir-lint.md). Calling `scripts/curamoir-lint` means the sweep gates
on exactly what CI gates on — the same interpreter choice, the same `--timeout`
(which uses SIGALRM, and so bounds a run only in its own process's main
thread), the same refusal to start Python inside the repository — and it reads
the report through the same reader the promotion ladder will, so the sweep
cannot come to depend on a `LintResult` field the JSON does not carry. The cost
is a process start per sweep, which is nothing for a weekly job.

**What is trusted.** Only a run that exited 0 or 1 with a `complete` report
whose verdict agrees with its exit code. Everything else raises LintUntrusted:
an exit of 2 (including the partial report printed when rules were skipped —
its findings are real, but what the skipped rules did not search has not been
shown clean, and a sweep that fixed half would report the repo as swept), any
other exit, output the reader refuses, a hang. Raising rather than returning an
empty list is deliberate: an untrustworthy report must not be confusable with
`no_work`, and inside a `JobRun` the exception becomes a `failure` ledger line
with this message, which is what the brief should show.

**What enters the list.** Every violation, at every severity. The lint reports
all of them and its threshold decides only its exit code; the spec makes the
output the work list, not the part that fails the gate. (The fixture's
errors-carry-context rule is a `warning`, below the default `fail_on`, so
filtering at the threshold would drop two of E3's five planted violations.)
How many the sweep then fixes is E3-R3's cap and E3-R2's scope, decided later
with severity available on every item.

**Order.** By principle, then path, line, column and rule, then fingerprint
(unique within a report, so the order is total). The PR body lists fixes by
principle and the ledger counts them by principle (E3-R3, E3-R5), so grouping
here keeps both stable across runs without each re-sorting.

Syntax that imports on 3.9, like the rest of what `scripts/curamoir` runs.
"""

import os
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from curamoir.lint_report import SEVERITIES, LintReport, LintReportError, parse_report

#: The standalone runner beside this checkout's `src/`, which is the one CI runs.
LINT = Path(__file__).resolve().parents[2] / "scripts" / "curamoir-lint"

#: The lint's own bound on the run, passed as `--timeout`: its default.
DEFAULT_TIMEOUT = 600

#: Extra seconds before the sweep kills a runner that ignores its own
#: `--timeout` (Python startup, writing the report).
DEFAULT_GRACE = 30

#: The work-list outcomes. NO_WORK is the string E3-R7's ledger line records.
WORK = "work"
NO_WORK = "no_work"

#: How much of the runner's stderr a refusal quotes: enough for its reasons,
#: bounded so a runaway log cannot bloat a ledger line.
_STDERR_TAIL = 2000


class LintUntrusted(Exception):
    """The lint's report cannot be acted on, so the sweep must stop without
    editing anything. `exit_code` is the runner's, or None if it never
    exited."""

    def __init__(self, message, exit_code=None):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class WorkList:
    """The violations the sweep is to fix, in work-list order (see the module
    docstring), and the report they came from (for its notes, tools and
    rules_checked, which the ledger line and PR body will want)."""

    items: tuple
    report: LintReport

    @property
    def outcome(self):
        """WORK or NO_WORK. This is the lint's half only: E3-R7's `no_work`
        also needs tier 1 (E3-R1b, cm-fkd.2) to have found nothing."""
        return WORK if self.items else NO_WORK

    @property
    def principle_ids(self):
        """The principles with work, sorted."""
        return tuple(OrderedDict.fromkeys(item.principle_id for item in self.items))

    @property
    def paths(self):
        """Every file with a violation, sorted: E3-R2's "only files with
        violations" is this set."""
        return tuple(sorted({item.path for item in self.items}))

    def by_principle(self):
        """principle id -> its items, in work-list order."""
        grouped = OrderedDict()
        for item in self.items:
            grouped.setdefault(item.principle_id, []).append(item)
        return OrderedDict((key, tuple(value)) for key, value in grouped.items())


def work_list(repo, lint=LINT, timeout=DEFAULT_TIMEOUT, grace=DEFAULT_GRACE):
    """Run the lint over `repo` and return its work list, or raise
    LintUntrusted. `lint` is the runner to call (a test may pass a stand-in)."""
    code, stdout, stderr = _run(repo, lint, timeout, grace)
    if code not in (0, 1):
        raise LintUntrusted(_untrusted_message(code, stdout, stderr), exit_code=code)
    try:
        report = parse_report(stdout)
    except LintReportError as error:
        raise LintUntrusted(
            f"curamoir-lint exited {code} but its report cannot be read: {error}",
            exit_code=code) from error
    if not report.complete:
        # The runner exits 2 for this; a report saying otherwise is from a
        # runner that does not keep the contract, which is no better.
        skipped = ", ".join(s.rule_id for s in report.rules_skipped)
        raise LintUntrusted(
            f"curamoir-lint exited {code} with an incomplete report (skipped: {skipped})",
            exit_code=code)
    threshold = SEVERITIES.index(report.fail_on)
    failing = sum(SEVERITIES.index(v.severity) >= threshold for v in report.violations)
    if (code == 1) != bool(failing):
        raise LintUntrusted(
            f"curamoir-lint exited {code} but its report has {failing} violation(s) at "
            f"or above fail_on {report.fail_on!r}; the exit code and the report disagree",
            exit_code=code)
    items = tuple(sorted(report.violations, key=_order))
    return WorkList(items=items, report=report)


def _order(item):
    return (item.principle_id, item.path, item.line, item.col, item.rule_id,
            item.end_line, item.end_col, item.fingerprint)


def _run(repo, lint, timeout, grace):
    command = [str(lint), "--repo", str(Path(repo).resolve()), "--format", "json",
               "--timeout", str(timeout)]
    # The report must be writable whatever the inherited locale: an encoding
    # that cannot represent a path is an exit 2 (docs/curamoir-lint.md).
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        result = subprocess.run(
            command, capture_output=True, env=environment, timeout=timeout + grace,
        )
    except subprocess.TimeoutExpired as error:
        raise LintUntrusted(
            f"curamoir-lint did not finish within {timeout + grace} seconds") from error
    except OSError as error:
        raise LintUntrusted(f"curamoir-lint could not be started ({lint}): {error}") from error
    return (result.returncode, result.stdout,
            result.stderr.decode("utf-8", errors="backslashreplace"))


def _untrusted_message(code, stdout, stderr):
    message = f"curamoir-lint exited {code}: the work list cannot be trusted"
    if code == 2 and stdout:
        # The partial report printed when rules were skipped: name them.
        try:
            skipped = parse_report(stdout).rules_skipped
        except LintReportError:
            skipped = ()
        if skipped:
            message += " (rules skipped: " + ", ".join(
                f"{s.rule_id}: {s.reason}" for s in skipped) + ")"
    tail = stderr.strip()[-_STDERR_TAIL:]
    if tail:
        message += f"\n{tail}"
    return message
