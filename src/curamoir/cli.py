"""The deterministic CLI behind cúramóir's slash commands.

Keeping the real work here, rather than in prompt text, is the same principle the
guard layer rests on: what must happen reliably happens in code. A slash command
is a thin wrapper that invokes this and reports what it said.
"""

import argparse
import json
import sys
from pathlib import Path

from curamoir.brief import due_for_brief_from_config, write_brief
from curamoir.doctor import diagnose, render_report
from curamoir.handoff import write_handoff
from curamoir.scaffold import (
    AlreadyInitialised,
    TemplateDrift,
    detect_languages,
    detect_modules,
    init_repo,
)
from curamoir.status import collect_status, render_status
from curamoir.sweep import LintUntrusted, work_list


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="curamoir", description="Entropy management for agent-written codebases."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="scaffold .curamoir/ in a repository")
    init.add_argument("--repo", default=".", help="repository root (default: cwd)")

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
        return _init(arguments.repo)
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
        path = write_brief(arguments.repo)
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
    principles = []
    for principle_id, items in result.by_principle().items():
        principles.append({
            "principle_id": principle_id,
            "principle_rule": items[0].principle_rule,
            "why_link": items[0].why_link,
            "violations": [
                {name: getattr(item, name) for name in (
                    "path", "line", "col", "end_line", "end_col", "severity", "rule_id",
                    "message", "fix", "fix_source", "match", "fingerprint")}
                for item in items
            ],
        })
    text = json.dumps({
        "outcome": result.outcome,
        "count": len(result.items),
        "fail_on": result.report.fail_on,
        "rules_checked": list(result.report.rules_checked),
        # Kinds only: the skill reports which notes there were (no_rules
        # above all), not their paths or reasons.
        "notes": [note.kind for note in result.report.notes],
        "paths": list(result.paths),
        "principles": principles,
    }, indent=2, ensure_ascii=False) + "\n"
    # UTF-8 bytes whatever the locale: printed through stdout's own encoding,
    # a non-ASCII path or message under an ASCII locale raises
    # UnicodeEncodeError, which main() does not catch, and the skill would see
    # a traceback and exit 1. The whole document is built before anything is
    # written, so every failure above leaves stdout empty.
    try:
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
    except OSError as failure:
        # A closed pipe or full disk mid-write: stdout may hold part of the
        # document, which the skill treats as not the work list. Exit 2 so it
        # stops rather than reading main()'s exit-1 sentence as the result.
        print(f"the work list could not be written: {failure}\n"
              "The sweep must stop here: do not edit anything.", file=sys.stderr)
        return 2
    return 0


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


def _init(repo):
    languages = detect_languages(repo) or ["none detected"]
    modules = detect_modules(repo)

    try:
        base = init_repo(repo)
    except AlreadyInitialised as existing:
        print(f"cúramóir is already initialised here.\n{existing}")
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
    print("\nNext: review .curamoir/curamoir.yaml, then run /curamoir:doctor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
