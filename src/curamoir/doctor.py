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

import json
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
    findings.extend(_check_hooks(repo))
    findings.append(_check_denylist_job_detection())
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


# One probe per hook event: the payload to send, and what proves it worked.
# Driven off hooks.json rather than hardcoded, so registering a sixth event
# cannot leave doctor silently ignoring it — which is how four of five events
# went unchecked after Epic 8 landed.
_HOOK_PROBES = {
    "PreToolUse": {
        "script": "curamoir-hook-pretooluse",
        "payload": {
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": "git reset --hard"},
        },
        "env": {"CURAMOIR_JOB": "doctor"},
        "expect": "deny",
        "proves": "fires and denies a destructive command",
        "if_not": (
            "Command hooks fail OPEN, so a hook that does not deny is protecting "
            "nothing. Check hooks/hooks.json and that python3 can import curamoir "
            "from the plugin's src/ directory."
        ),
    },
    "Stop": {
        "script": "curamoir-hook-stop",
        "payload": {"hook_event_name": "Stop", "last_assistant_message": "doctor probe"},
        "writes": ("handoff", "journal.jsonl"),
        "proves": "fires and records the turn",
        "if_not": "Check scripts/curamoir-hook-stop is executable and python3 is on PATH.",
    },
    "PreCompact": {
        "script": "curamoir-hook-precompact",
        "payload": {"hook_event_name": "PreCompact", "trigger": "manual"},
        "writes": ("handoff", "latest.md"),
        "proves": "fires and writes a handoff",
        "if_not": "Check scripts/curamoir-hook-precompact is executable.",
    },
    "SessionEnd": {
        "script": "curamoir-hook-sessionend",
        "payload": {"hook_event_name": "SessionEnd", "reason": "clear"},
        "writes": ("handoff", "latest.md"),
        "proves": "fires and finalises the handoff",
        "if_not": "Check scripts/curamoir-hook-sessionend is executable.",
    },
    "SessionStart": {
        "script": "curamoir-hook-sessionstart",
        "payload": {"hook_event_name": "SessionStart", "source": "startup"},
        "injects": True,
        "proves": "fires",
        "if_not": "Check scripts/curamoir-hook-sessionstart is executable.",
    },
}


def _registered_hook_events():
    """The events hooks.json actually registers."""
    path = Path(__file__).resolve().parents[2] / "hooks" / "hooks.json"
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("hooks", {}))
    except (ValueError, OSError):
        return []


# Probe order matters: the PreCompact and SessionEnd probes WRITE a handoff, so
# probing SessionStart after them would always report "injects" even on a repo
# that has never had one — doctor would be describing state it had just created.
# Read-only probes first.
_PROBE_ORDER = ("PreToolUse", "SessionStart", "Stop", "PreCompact", "SessionEnd")


def _check_hooks(repo):
    """Run EVERY registered hook rather than asserting hooks.json exists.

    E1-R4 says "verify each hook fires (dry-run)". Checking the file proves
    nothing about whether a hook works — which is exactly how the E0-R5
    import-path bug survived, and how four of five events went unchecked once
    Epic 8 registered them.
    """
    events = _registered_hook_events()
    if not events:
        return [Finding(
            "hooks", "error", "hooks/hooks.json is missing or unreadable",
            fix="Reinstall the plugin.",
        )]

    findings = []
    ordered = [e for e in _PROBE_ORDER if e in events]
    ordered += [e for e in events if e not in _PROBE_ORDER]
    for event in ordered:
        probe = _HOOK_PROBES.get(event)
        if probe is None:
            findings.append(Finding(
                f"hook ({event})", "warning",
                "registered but doctor has no probe for it",
                fix=f"Add a probe for {event} to _HOOK_PROBES in doctor.py.",
            ))
            continue
        findings.append(_run_hook_probe(repo, event, probe))
    return findings


def _run_hook_probe(repo, event, probe, script_path=None):
    import os

    wrapper = script_path or Path(__file__).resolve().parents[2] / "scripts" / probe["script"]
    label = f"hook ({event})"
    if not wrapper.is_file():
        return Finding(label, "error", f"{wrapper} is missing",
                       fix="Reinstall the plugin; the hook script is absent.")

    payload = dict(probe["payload"], cwd=str(repo))
    environment = {"PATH": os.environ.get("PATH", "")}
    environment.update(probe.get("env", {}))

    before = _probe_marker(repo, probe)
    try:
        result = subprocess.run(
            [str(wrapper)], input=json.dumps(payload),
            capture_output=True, text=True, timeout=10, env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Finding(label, "error", f"hook could not be run ({error})",
                       fix="Check python3 is on PATH and the plugin directory is readable.")

    if result.returncode != 0:
        return Finding(label, "error", f"hook exited {result.returncode}: {result.stderr[:200]}",
                       fix=probe["if_not"])

    if "expect" in probe:
        if probe["expect"] in result.stdout:
            return Finding(label, "ok", probe["proves"])
        return Finding(label, "error",
                       f"ran but did not produce {probe['expect']!r} (stdout={result.stdout[:120]!r})",
                       fix=probe["if_not"])

    if "writes" in probe:
        after = _probe_marker(repo, probe)
        # The file must exist AND have changed. An `or after is not None` here
        # made the comparison irrelevant: because hooks fail OPEN and exit 0, and
        # because the SessionEnd probe runs after PreCompact has already written
        # latest.md, a hook that did nothing still reported ok.
        if after is not None and after != before:
            return Finding(label, "ok", probe["proves"])
        return Finding(
            label, "error",
            f"ran (exit 0) but wrote nothing to {'/'.join(probe['writes'])}",
            fix=probe["if_not"],
        )

    if probe.get("injects"):
        # Distinguish "injected something" from "ran". Reporting the same detail
        # either way proves only that the script is executable.
        if result.stdout.strip():
            return Finding(label, "ok", "fires and injects the previous handoff")
        return Finding(
            label, "ok",
            "fires; injected nothing, which is correct when no handoff exists yet",
        )

    return Finding(label, "ok", probe["proves"])


def _check_denylist_job_detection():
    """Disclose the denylist's current silent-failure mode.

    The denylist enforces only inside a cúramóir job. DEC-5 established two
    detection signals: the CURAMOIR_JOB environment variable, with the supervisor
    lock as a fallback precisely because CLAUDE_CODE_SUBPROCESS_ENV_SCRUB can
    strip env vars from hook processes.

    The fallback does not work yet, because nothing acquires or writes the lock
    (E0-R2's unmet half, tracked as E10-LOCK). So if the env var is stripped, the
    denylist stops enforcing and says nothing.

    This check exists because a guardrail with an undisclosed silent-failure mode
    is worse than an absent one: the absent one does not invite reliance. Remove
    it when E10-LOCK lands and the fallback becomes real.
    """
    from curamoir.guards.lock import LOCK_PATH

    return Finding(
        check="denylist job detection",
        level="warning",
        detail=(
            "the destructive-command denylist enforces only inside a cúramóir "
            "job, detected from the CURAMOIR_JOB env var with the supervisor lock "
            "as fallback. Nothing writes the lock yet, so if the env var is "
            "stripped (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB) the denylist stops "
            "enforcing silently"
        ),
        fix=(
            "Until E10-LOCK lands: do not rely on the denylist as a safety "
            "boundary, and keep CLAUDE_CODE_SUBPROCESS_ENV_SCRUB unset so the "
            f"primary signal survives. The fallback reads .curamoir/{'/'.join(LOCK_PATH)}."
        ),
    )


def _probe_marker(repo, probe):
    """Mtime of the file a hook is expected to write, or None if absent."""
    if "writes" not in probe:
        return None
    path = Path(repo).joinpath(".curamoir", *probe["writes"])
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


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
