#!/usr/bin/env python3
"""r5-mutations.py <bead> <base> <head>: the bridge's mechanical kill check.

cm-8ap.19 (Rob, 2026-11-24). Run by r5-supervise on EVERY provenance round,
before the provenance reviewer is dispatched:

    uv run python .claude/hooks/r5-mutations.py <bead> <base> <head>

It reads the bead's brief from `bd show <bead> --json` (its description,
design, acceptance criteria and notes, in that order), and:

- `bd show` failing, or a brief that cannot be read: exit 2, "could not run".
- a brief read successfully with no `r5-mutations` block: exit 0, verdict
  `none`. The supervisor tells the reviewer that no mechanical checks ran.
- otherwise it runs `curamoir run mutations --repo <checkout> --base <base>
  --head <head> --brief <file>` and keeps the JSON lines it printed in
  `.claude/r5-reports/<bead>-mutations-<n>.jsonl` (the brief it gave beside
  them, `.brief.md`). The verdict:
  - `pass`: exit 0, as many lines as the entries' `total`, every one red.
    Exit 0.
  - `block`: exit 1 with every line present and some green, stale or
    inconclusive. Exit 1.
  - `could_not_run`: anything else (exit 3 or 4, a crash, fewer lines than
    `total`, a line that does not parse). Exit 2: the supervisor blocks and
    escalates to the user.

The verdict is recorded as a `mutations_run` event in the run's state
(`r5-state.sh event`), and `r5-state.sh done provenance` refuses unless the
last `mutations_run` since provenance was last requested is `pass` or
`none`, whatever the reviewer's R5-FINDINGS line says.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFO_STRING = "r5-mutations"
BRIEF_FIELDS = ("description", "design", "acceptance_criteria", "notes")

EXIT_PASS, EXIT_BLOCK, EXIT_COULD_NOT_RUN = 0, 1, 2


def main(argv) -> int:
    if len(argv) != 4:
        print("usage: r5-mutations.py <bead> <base> <head>", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    bead, base, head = argv[1:]
    checkout = _main_checkout()
    reports = checkout / ".claude" / "r5-reports"
    brief = _brief(bead, checkout)
    if brief is None:
        return _record(checkout, "could_not_run", None, 0, 0, None,
                       f"the brief of {bead} could not be read", EXIT_COULD_NOT_RUN)
    if not _has_block(brief):
        print(f"none: {bead}'s brief has no `{INFO_STRING}` block, so no mechanical kill "
              "check ran; tell the provenance reviewer so.")
        return _record(checkout, "none", None, 0, 0, None, "no block", EXIT_PASS)
    reports.mkdir(parents=True, exist_ok=True)
    n = 1 + len(list(reports.glob(f"{bead}-mutations-*.jsonl")))
    # Joined as text: a bead id has dots (cm-8ap.19), which Path.with_suffix
    # would take for the file's suffix.
    stem = f"{bead}-mutations-{n}"
    jsonl, brief_file = reports / f"{stem}.jsonl", reports / f"{stem}.brief.md"
    brief_file.write_text(brief, encoding="utf-8")
    command = [*_curamoir(), "run", "mutations", "--repo", str(checkout), "--base", base,
               "--head", head, "--brief", str(brief_file)]
    try:
        done = subprocess.run(command, cwd=checkout, capture_output=True, text=True)
    except OSError as error:
        return _record(checkout, "could_not_run", None, 0, 0, None,
                       f"curamoir could not be started: {error}", EXIT_COULD_NOT_RUN)
    jsonl.write_text(done.stdout, encoding="utf-8")
    sys.stderr.write(done.stderr)
    lines, problem = [], None
    for text in done.stdout.splitlines():
        try:
            lines.append(json.loads(text))
        except ValueError:
            problem = "a line on stdout is not JSON"
    totals = {line.get("total") for line in lines if isinstance(line, dict)}
    total = totals.pop() if len(totals) == 1 else None
    report = str(jsonl.relative_to(checkout))
    if problem is None and total is not None and len(lines) == total:
        results = [line.get("result") for line in lines]
        if done.returncode == 0 and all(result == "red" for result in results):
            return _record(checkout, "pass", done.returncode, len(lines), total, report,
                           f"all {total} mutation(s) killed", EXIT_PASS)
        if done.returncode == 1:
            return _record(checkout, "block", done.returncode, len(lines), total, report,
                           ", ".join(f"entry {line['index']} {line['result']}" for line in lines
                                     if line.get("result") != "red"), EXIT_BLOCK)
    why = problem or (f"exit {done.returncode}, {len(lines)} line(s) of "
                      f"{total if total is not None else 'an unknown number'}")
    return _record(checkout, "could_not_run", done.returncode, len(lines), total or 0, report,
                   why, EXIT_COULD_NOT_RUN)


def _main_checkout() -> Path:
    common = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], capture_output=True, text=True, check=True)
    return Path(common.stdout.strip()).parent


def _brief(bead: str, checkout: Path) -> str | None:
    try:
        shown = subprocess.run(["bd", "show", bead, "--json"], cwd=checkout,
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if shown.returncode != 0:
        return None
    try:
        data = json.loads(shown.stdout)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if len(data) == 1 else None
    if not isinstance(data, dict):
        return None
    return "\n\n".join(data[field] for field in BRIEF_FIELDS
                       if isinstance(data.get(field), str) and data[field])


def _has_block(text: str) -> bool:
    """Whether the brief holds an `r5-mutations` fenced block at all, decided
    by the runner's own CommonMark reading (a fence inside another fence,
    say, is not one) when curamoir can be imported here; whether the block
    is valid is the runner's to say (it refuses)."""
    try:
        from curamoir.gate.mutation_list import MutationListError, extract_block
    except ImportError:
        return _looks_fenced(text)
    try:
        extract_block(text)
    except MutationListError as error:
        # Two blocks is still "a block": the runner refuses it, which blocks.
        return "blocks" in str(error)
    return True


def _looks_fenced(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) <= 3 and stripped[:3] in ("```", "~~~"):
            info = stripped.lstrip("`~").strip().split()
            if info and info[0] == INFO_STRING:
                return True
    return False


def _curamoir() -> list:
    found = shutil.which("curamoir")
    return [found] if found else [sys.executable, "-m", "curamoir.cli"]


def _record(checkout, verdict, code, lines, total, report, why, exit_code) -> int:
    event = {"verdict": verdict, "exit": code, "lines": lines, "total": total,
             "report": report, "why": why}
    subprocess.run(["bash", str(HERE / "r5-state.sh"), "event", "mutations_run",
                    json.dumps(event)], cwd=checkout, check=False)
    print(f"{verdict}: {why}" + (f" ({report})" if report else ""))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
