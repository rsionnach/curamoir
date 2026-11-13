"""The deterministic CLI behind cúramóir's slash commands.

Keeping the real work here, rather than in prompt text, is the same principle the
guard layer rests on: what must happen reliably happens in code. A slash command
is a thin wrapper that invokes this and reports what it said.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from curamoir.brief import due_for_brief_from_config, gh_pr_states, write_brief
from curamoir.config import resolve_github_api
from curamoir.doctor import diagnose, render_report
from curamoir.handoff import write_handoff
from curamoir.runner import run_job
from curamoir.scaffold import (
    AlreadyInitialised,
    TemplateDrift,
    UnsafePath,
    detect_languages,
    detect_modules,
    init_repo,
    write_github_workflow,
)
from curamoir.state_branch import DEFAULT_BRANCH, push_ledger
from curamoir.status import collect_status, render_status
from curamoir.sweep import LintUntrusted, work_list, work_list_document


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="curamoir", description="Entropy management for agent-written codebases."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="scaffold .curamoir/ in a repository")
    init.add_argument("--repo", default=".", help="repository root (default: cwd)")
    init.add_argument("--github-workflow", action="store_true",
                      help="also write .github/workflows/curamoir-sweep.yml, the scheduled "
                           "GitHub Actions sweep; never over an existing file")

    status = subcommands.add_parser("status", help="show lock, pause, spend, grades, PRs")
    status.add_argument("--repo", default=".", help="repository root (default: cwd)")

    doctor = subcommands.add_parser("doctor", help="validate the setup and say how to fix it")
    doctor.add_argument("--repo", default=".", help="repository root (default: cwd)")

    handoff = subcommands.add_parser("handoff", help="write a session handoff now")
    handoff.add_argument("--repo", default=".", help="repository root (default: cwd)")
    handoff.add_argument("--note", default=None, help="what you want your future self to know")
    handoff.add_argument("--next", dest="next_step", default=None,
                         help="the intended next step (E8-R1's most useful field)")

    brief = subcommands.add_parser("brief", help="generate the away-brief")
    brief.add_argument("--repo", default=".", help="repository root (default: cwd)")
    brief.add_argument("--if-due", action="store_true",
                       help="only generate when the absence threshold has passed")

    sweep = subcommands.add_parser(
        "sweep-work-list", help="run curamoir-lint and print the sweep's work list (E3-R1)")
    sweep.add_argument("--repo", default=".", help="repository root (default: cwd)")

    run = subcommands.add_parser(
        "run", help="run a scheduled job behind the guards, in E10-R1's order")
    # A free string, not argparse `choices`: a job not built yet must exit
    # naming the bead that builds it, which a usage error cannot say.
    run.add_argument("job", help="the job to run (curamoir.runner.JOBS)")
    run.add_argument("--repo", default=".", help="repository root (default: cwd)")

    state = subcommands.add_parser(
        "state", help="the hosted runner's ledger on its state branch")
    state_commands = state.add_subparsers(dest="state_command", required=True)
    state_push = state_commands.add_parser(
        "push", help="commit this run's new ledger lines to the state branch on origin")
    state_push.add_argument("--repo", default=".", help="repository root (default: cwd)")
    state_push.add_argument("--branch", default=DEFAULT_BRANCH,
                            help=f"the state branch (default: {DEFAULT_BRANCH}); never the "
                                 "default branch")

    arguments = parser.parse_args(argv)
    try:
        return _run(arguments)
    except OSError as failure:
        # A slash command relays this straight to the developer, and a stack
        # trace is the one thing it must not show them. `init` already turned
        # two faults into sentences (AlreadyInitialised, TemplateDrift); this
        # covers the rest — a `.curamoir` that is a file, a read-only checkout,
        # a full disk.
        print(f"cúramóir could not complete that: {failure}")
        return 1


def _run(arguments):
    if arguments.command == "init":
        return _init(arguments.repo, github_workflow=arguments.github_workflow)
    if arguments.command == "state" and arguments.state_command == "push":
        pushed = push_ledger(arguments.repo, arguments.branch)
        # UTF-8 bytes whatever the locale, as for `run`: the exit code is
        # what the workflow's step reads (state_branch.EXIT_CODES).
        _write_utf8(pushed.sentence + "\n")
        return pushed.exit_code
    if arguments.command == "status":
        print(render_status(collect_status(arguments.repo)))
        return 0
    if arguments.command == "brief":
        uninitialised = _uninitialised(arguments.repo)
        if uninitialised is not None:
            return uninitialised
        if arguments.if_due and not due_for_brief_from_config(arguments.repo):
            print("No brief needed: the last handoff is recent.")
            return 0
        # cm-2qp.9: this command's own entry point decides how gh is asked,
        # as runner.run_job does for a scheduled brief.
        path = write_brief(arguments.repo, pr_states=gh_pr_states(
            arguments.repo, github_api=resolve_github_api(os.environ)))
        print(f"Wrote {path}\n")
        print(path.read_text(encoding="utf-8"))
        return 0
    if arguments.command == "handoff":
        uninitialised = _uninitialised(arguments.repo)
        if uninitialised is not None:
            return uninitialised
        path = write_handoff(arguments.repo, note=arguments.note,
                             next_step=arguments.next_step)
        if path is None:
            print(
                "cúramóir could not write the handoff. Check that "
                ".curamoir/handoff is writable, then run /curamoir:doctor."
            )
            return 1
        print(f"Wrote {path}")
        print(path.read_text(encoding="utf-8"))
        return 0
    if arguments.command == "run":
        ran = run_job(arguments.repo, arguments.job)
        # As UTF-8 bytes (_write_utf8): print() under an ASCII locale raises
        # on the "ú" in every sentence, and main() would turn each outcome
        # into exit 1, breaking the exit codes a scheduler reads.
        _write_utf8(ran.sentence + "\n")
        return ran.exit_code
    if arguments.command == "sweep-work-list":
        return _sweep_work_list(arguments.repo)
    if arguments.command == "doctor":
        findings = diagnose(arguments.repo)
        print(render_report(findings))
        # Non-zero only on error: a warning is a degraded-but-usable setup, and
        # failing CI over one would make the distinction meaningless.
        return 1 if any(f.level == "error" for f in findings) else 0
    return 2


def _sweep_work_list(repo):
    """The work list as JSON on stdout, for the sweep skill to act on.

    Exit 0 whether or not there is work (`outcome` says which); exit 2 with
    nothing on stdout when the lint cannot be trusted, so an agent cannot
    mistake a partial list for the work list. The shape is cúramóir-internal,
    read only by skills/sweep/SKILL.md; the versioned contract is the lint's
    own report.
    """
    try:
        result = work_list(repo)
    except LintUntrusted as untrusted:
        # stderr's error handler is backslashreplace, so a path the locale
        # cannot encode still reaches the developer rather than raising.
        print(f"{untrusted}\nThe sweep must stop here: do not edit anything.",
              file=sys.stderr)
        return 2
    text = json.dumps(work_list_document(result), indent=2, ensure_ascii=False) + "\n"
    # UTF-8 bytes whatever the locale (_write_utf8). The whole document is
    # built before anything is written, so every failure above leaves stdout
    # empty.
    try:
        _write_utf8(text)
    except OSError as failure:
        # A closed pipe or full disk mid-write: stdout may hold part of the
        # document, which the skill treats as not the work list. Exit 2 so it
        # stops rather than reading main()'s exit-1 sentence as the result.
        print(f"the work list could not be written: {failure}\n"
              "The sweep must stop here: do not edit anything.", file=sys.stderr)
        return 2
    return 0


def _write_utf8(text):
    """Write `text` to stdout as UTF-8 bytes whatever the locale.

    Printed through stdout's own encoding, a non-ASCII path or message under
    an ASCII locale raises UnicodeEncodeError, which main() does not catch:
    the caller would see a traceback and exit 1. An OSError (a closed pipe,
    a full disk) is the caller's to handle.
    """
    if sys.stdout is None:
        # stdout closed (`>&-`): nowhere to write, and print()'s own silent
        # no-op, rather than an AttributeError that costs the exit code.
        return
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        # An in-memory text stream (a test's capture) takes str whatever
        # the locale.
        sys.stdout.write(text)
        sys.stdout.flush()
    else:
        sys.stdout.flush()
        buffer.write(text.encode("utf-8"))
        buffer.flush()


def _uninitialised(repo):
    """1 and a message if cúramóir is not set up here, else None.

    `status` already required this and the HOOKS check for it (`_entry.run`
    returns early when `.curamoir` is not a directory), but `brief` and
    `handoff` wrote through `mkdir(parents=True)`. Running either before `init`
    therefore created a `.curamoir/` holding only that one artefact, after which
    `init` refused permanently and the repo never got curamoir.yaml,
    principles.md or state/ at all.
    """
    if (Path(repo) / ".curamoir").is_dir():
        return None
    print("cúramóir is not initialised here. Run /curamoir:init first.")
    return 1


def _init(repo, github_workflow=False):
    languages = detect_languages(repo) or ["none detected"]
    modules = detect_modules(repo)

    try:
        base = init_repo(repo)
    except AlreadyInitialised as existing:
        print(f"cúramóir is already initialised here.\n{existing}")
        if github_workflow:
            # The workflow does not depend on `.curamoir/`, and a developer
            # who initialised before it existed asks for it this way. Then
            # the workflow is what was asked for, so its result is the exit
            # code: 0 when it was written (or its .new was), 1 when its path
            # was refused. The slash command reports a non-zero exit as a
            # failure, and "already initialised" is a note here, not one.
            # Without the flag the refusal stays 1, as before cm-2qp.4.
            return _init_workflow(repo)
        return 1
    except UnsafePath as unsafe:
        print(f"cúramóir did not initialise here.\n{unsafe}")
        return 1
    except TemplateDrift as drift:
        # A message, not a traceback: this is a packaging fault in cúramóir
        # itself, and the developer can do nothing with a stack trace.
        print(f"cúramóir's bundled config template is damaged.\n{drift}")
        return 1

    print(f"Scaffolded {base}")
    print(f"  languages: {', '.join(languages)}")
    if modules:
        print(f"  modules ({len(modules)}):")
        for module in modules:
            print(f"    - {module}")
        if len(modules) < 3:
            print(
                "  note: fewer than three modules, so review selection will "
                "round-robin rather than choose by grade."
            )
    else:
        print(
            "  modules: none detected. Set modules.include explicitly in "
            ".curamoir/curamoir.yaml, or grading has nothing to grade."
        )
    if github_workflow and _init_workflow(repo):
        return 1
    print("\nNext: review .curamoir/curamoir.yaml, then run /curamoir:doctor.")
    return 0


def _init_workflow(repo):
    """Write and report the workflow: 0, or 1 when a path was unsafe."""
    try:
        written = write_github_workflow(repo)
    except UnsafePath as unsafe:
        print(f"  workflow: not written. {unsafe}")
        return 1
    _report_workflow(repo, written)
    return 0


def _report_workflow(repo, written):
    """What `init --github-workflow` did, in the words init uses for the config."""
    where = written.path.relative_to(Path(repo)).as_posix()
    if written.pending is not None:
        print(f"  workflow: {where} already exists; nothing was overwritten. The template "
              f"is at {where}.new (GitHub does not run it): diff the two and merge by hand "
              "if you want the changes.")
        return
    print(f"  workflow: wrote {where}, the scheduled GitHub Actions sweep. It runs nothing "
          "until it is committed and pushed, and needs the CURAMOIR_REF repository "
          "variable (a cúramóir tag or commit) and the ANTHROPIC_API_KEY repository "
          "secret (billed per token); its header says what else.")


if __name__ == "__main__":
    sys.exit(main())
