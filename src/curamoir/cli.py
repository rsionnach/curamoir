"""The deterministic CLI behind cúramóir's slash commands.

Keeping the real work here, rather than in prompt text, is the same principle the
guard layer rests on: what must happen reliably happens in code. A slash command
is a thin wrapper that invokes this and reports what it said.
"""

import argparse
import sys

from curamoir.scaffold import AlreadyInitialised, detect_languages, detect_modules, init_repo


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="curamoir", description="Entropy management for agent-written codebases."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="scaffold .curamoir/ in a repository")
    init.add_argument("--repo", default=".", help="repository root (default: cwd)")

    arguments = parser.parse_args(argv)
    if arguments.command == "init":
        return _init(arguments.repo)
    return 2


def _init(repo):
    languages = detect_languages(repo) or ["none detected"]
    modules = detect_modules(repo)

    try:
        base = init_repo(repo)
    except AlreadyInitialised as existing:
        print(f"cúramóir is already initialised here.\n{existing}")
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
