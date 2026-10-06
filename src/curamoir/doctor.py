"""E1-R4: /curamoir:doctor — validate the setup and say how to fix it.

Every finding carries a fix. A doctor that reports "bd not found" and stops has
moved the work rather than done it.

Three levels, and the distinction matters:
  ok        nothing to do
  warning   degraded but usable, and documented as such
  error     cúramóir cannot work until this is fixed

`bd` absent is a warning, not an error: `filing.beads: auto` means GitHub-only
with a ledger note. `ast-grep` absent is a warning: AST rules skip while regex
rules still run (DEC-3). Reporting either as a failure teaches the developer to
ignore the output, which costs more than the check is worth.
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from curamoir.config import ConfigError, load_config_text
from curamoir.scaffold import detect_languages, detect_modules

LEVEL_ORDER = {"error": 0, "warning": 1, "ok": 2}


@dataclass
class Finding:
    check: str
    level: str
    detail: str = ""
    fix: str = field(default="")


def diagnose(repo, tools=None):
    """Check the installation and configuration of `repo`.

    `tools` lets a caller state what is available instead of probing PATH, which
    keeps the checks testable. Omitted keys are probed.
    """
    repo = Path(repo)
    tools = {} if tools is None else dict(tools)
    findings = []

    base = repo / ".curamoir"
    if not base.is_dir():
        return [Finding(
            check="initialised",
            level="error",
            detail=f"{base} does not exist",
            fix="Run /curamoir:init to scaffold .curamoir/ in this repository.",
        )]
    findings.append(Finding("initialised", "ok", str(base)))

    findings.append(_check_pyyaml(tools))
    findings.append(_check_config(base))
    findings.extend(_check_modules(repo))
    findings.append(_check_pause(base))
    findings.append(_check_hook(repo))
    findings.extend(_check_tools(tools))

    return sorted(findings, key=lambda f: LEVEL_ORDER[f.level])


def render_report(findings):
    """Errors first, then warnings, then what is fine."""
    lines = ["cúramóir doctor", ""]
    for level, heading in (("error", "ERRORS"), ("warning", "WARNINGS"), ("ok", "OK")):
        group = [f for f in findings if f.level == level]
        if not group:
            continue
        lines.append(f"{heading} ({len(group)})")
        for finding in group:
            detail_lines = finding.detail.splitlines() if finding.detail else []
            if len(detail_lines) <= 1:
                lines.append(f"  {finding.check}: {finding.detail}" if finding.detail
                             else f"  {finding.check}")
            else:
                # A multi-problem detail stays one problem per line, indented, so
                # a config with three bad keys reads as three things to fix.
                lines.append(f"  {finding.check}:")
                lines.extend(f"    {line}" for line in detail_lines)
            if finding.fix:
                lines.append(f"    fix: {finding.fix}")
        lines.append("")
    return "\n".join(lines)


def _check_pyyaml(tools):
    available = tools.get("yaml")
    if available is None:
        # find_spec rather than an import: this is a probe, and an unused import
        # reads as dead code to every linter that looks at it.
        from importlib.util import find_spec

        available = find_spec("yaml") is not None
    if available:
        return Finding("PyYAML", "ok", "importable")
    return Finding(
        check="PyYAML",
        level="error",
        detail="not importable, so curamoir.yaml cannot be read",
        fix=(
            "pip install pyyaml  (a Claude Code plugin is an unpacked directory "
            "with no install step, so Python dependencies are not installed for you)"
        ),
    )


def _check_config(base):
    path = base / "curamoir.yaml"
    if not path.is_file():
        return Finding(
            "config", "error", f"{path} is missing",
            fix="Run /curamoir:init, or copy templates/curamoir.yaml into .curamoir/.",
        )
    try:
        load_config_text(path.read_text(encoding="utf-8"))
    except ConfigError as invalid:
        return Finding(
            "config", "error", "\n".join(invalid.problems),
            fix="Correct the keys named above in .curamoir/curamoir.yaml.",
        )
    return Finding("config", "ok", "valid")


def _check_modules(repo):
    modules = detect_modules(repo)
    languages = detect_languages(repo) or ["none detected"]
    findings = [Finding("languages", "ok", ", ".join(languages))]

    if not modules:
        findings.append(Finding(
            "modules", "warning", "no modules detected",
            fix=("Set modules.include explicitly in .curamoir/curamoir.yaml. "
                 "With no modules, grading and review selection have nothing to work on."),
        ))
    elif len(modules) < 3:
        findings.append(Finding(
            "modules", "warning",
            f"{len(modules)} detected: {', '.join(modules)}",
            fix=("Fewer than three modules means review selection round-robins "
                 "instead of choosing by grade. Fine for a small repo; set "
                 "modules.include if detection has missed something."),
        ))
    else:
        findings.append(Finding("modules", "ok", f"{len(modules)}: {', '.join(modules)}"))
    return findings


def _check_pause(base):
    if (base / "PAUSE").exists():
        return Finding(
            "pause", "warning", ".curamoir/PAUSE is present, so scheduled jobs will not run",
            fix="Delete .curamoir/PAUSE to resume scheduled runs.",
        )
    return Finding("pause", "ok", "not paused")


def _check_hook(repo):
    """Run the hook rather than merely checking that hooks.json exists.

    Asserting the file is present proves nothing about whether the hook works —
    which is exactly how the E0-R5 import-path bug survived until E1-R1.
    """
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "curamoir-hook-pretooluse"
    if not wrapper.is_file():
        return Finding(
            "hook (PreToolUse)", "error", f"{wrapper} is missing",
            fix="Reinstall the plugin; the denylist hook script is absent.",
        )

    payload = (
        '{"hook_event_name":"PreToolUse","tool_name":"Bash",'
        '"tool_input":{"command":"git reset --hard"},"cwd":"%s"}' % repo
    )
    try:
        result = subprocess.run(
            [str(wrapper)], input=payload, capture_output=True, text=True, timeout=10,
            env={"PATH": __import__("os").environ.get("PATH", ""), "CURAMOIR_JOB": "doctor"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Finding(
            "hook (PreToolUse)", "error", f"hook could not be run ({error})",
            fix="Check that python3 is on PATH and the plugin directory is readable.",
        )

    if "deny" in result.stdout:
        return Finding("hook (PreToolUse)", "ok", "fires and denies a destructive command")
    return Finding(
        "hook (PreToolUse)", "error",
        f"hook ran but did not deny `git reset --hard` (stdout={result.stdout!r})",
        fix=("Command hooks fail OPEN, so a hook that does not deny is protecting "
             "nothing. Check hooks/hooks.json and that python3 can import curamoir "
             "from the plugin's src/ directory."),
    )


def _check_tools(tools):
    specs = (
        ("bd", "warning",
         "beads filing unavailable; findings will be GitHub-only with a ledger note",
         "brew install beads  (or see https://github.com/steveyegge/beads)"),
        ("gh", "warning",
         "GitHub operations unavailable: no PR creation, no issue filing, no PR cap",
         "brew install gh && gh auth login"),
        ("ast-grep", "warning",
         "AST-matched rules will be skipped; regex rules still run",
         "pip install ast-grep-cli  (or npm i -g @ast-grep/cli, or brew install ast-grep)"),
        ("python3", "error",
         "cúramóir's deterministic helpers cannot run",
         "Install Python 3.12 or later and ensure python3 is on PATH."),
    )
    findings = []
    for name, level, detail, fix in specs:
        available = tools.get(name)
        if available is None:
            available = shutil.which(name if name != "python3" else sys.executable) is not None
        findings.append(
            Finding(name, "ok", "available") if available
            else Finding(name, level, detail, fix=fix)
        )

    container = tools.get("container")
    if container is None:
        container = any(shutil.which(runtime) for runtime in ("docker", "podman", "nerdctl"))
    findings.append(
        Finding("container runtime", "ok", "available") if container
        else Finding(
            "container runtime", "warning",
            "the quickstart smoke test (Epic 12) needs a clean container",
            fix="Install Docker or Podman, or disable the smoke job in cadence.",
        )
    )
    return findings
