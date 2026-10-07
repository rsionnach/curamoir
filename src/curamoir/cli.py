"""The deterministic CLI behind cúramóir's slash commands.

Keeping the real work here, rather than in prompt text, is the same principle the
guard layer rests on: what must happen reliably happens in code. A slash command
is a thin wrapper that invokes this and reports what it said.
"""

import argparse
import sys

from curamoir.brief import due_for_brief_from_config, write_brief
from curamoir.doctor import diagnose, render_report
from curamoir.handoff import write_handoff
from curamoir.scaffold import (
    AlreadyInitialised, TemplateDrift, detect_languages, detect_modules, init_repo,
)
from curamoir.status import collect_status, render_status


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

    arguments = parser.parse_args(argv)
    if arguments.command == "init":
        return _init(arguments.repo)
    if arguments.command == "status":
        print(render_status(collect_status(arguments.repo)))
        return 0
    if arguments.command == "brief":
        if arguments.if_due and not due_for_brief_from_config(arguments.repo):
            print("No brief needed: the last handoff is recent.")
            return 0
        path = write_brief(arguments.repo)
        print(f"Wrote {path}\n")
        print(path.read_text(encoding="utf-8"))
        return 0
    if arguments.command == "handoff":
        path = write_handoff(arguments.repo, note=arguments.note,
                             next_step=arguments.next_step)
        print(f"Wrote {path}")
        print(path.read_text(encoding="utf-8"))
        return 0
    if arguments.command == "doctor":
        findings = diagnose(arguments.repo)
        print(render_report(findings))
        # Non-zero only on error: a warning is a degraded-but-usable setup, and
        # failing CI over one would make the distinction meaningless.
        return 1 if any(f.level == "error" for f in findings) else 0
    return 2


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
