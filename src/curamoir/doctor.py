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

One warning is currently unconditional: the denylist's job-detection fallback is
not implemented (E10-LOCK), and an undisclosed silent-failure mode is worse than
a declared one. So a healthy repo reports WARNINGS rather than OK until that
lands — see `_check_job_detection`.
"""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from curamoir.config import ConfigError, load_config_text
from curamoir.guards.lock import LOCK_PATH
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
    findings.append(_check_job_detection())
    findings.append(_check_writable(base))
    findings.append(_check_gitignore(base))
    findings.extend(_check_tools(tools, repo=repo))

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
# cannot leave doctor silently ignoring it.
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

    E1-R4 says "verify each hook fires (dry-run)". The file existing proves
    nothing about whether a hook actually runs under the installed layout.
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
        findings.append(_probe_hook(repo, event, probe))
    return findings


def _probe_target(repo, probe, probe_target=None):
    """Where to run a probe.

    Writing probes run against a SCRATCH repo, never the developer's. The hooks
    they invoke are the real ones, and three of them write a handoff — so probing
    in place would replace `handoff/latest.md`, append fabricated journal notes
    (which become the next handoff's "Last decision"), and burn two of ten
    history slots per run. Running doctor to find out why your handoff looks
    wrong would guarantee it was wrong.

    The probe only needs to observe that the hook writes SOMEWHERE, so a skeleton
    is enough.
    """
    if not probe.get("writes"):
        return repo, None
    if probe_target is not None:
        # Injected by tests so the "file present and unchanged" state is
        # reachable. _probe_repo otherwise owns a fresh scratch tree, which made
        # that state impossible to construct — and it is the one the bug let
        # through, so the fix had no test.
        return Path(probe_target), None
    scratch = Path(tempfile.mkdtemp(prefix="curamoir-doctor-"))
    (scratch / ".curamoir" / "handoff" / "history").mkdir(parents=True, exist_ok=True)
    (scratch / ".curamoir" / "state").mkdir(parents=True, exist_ok=True)
    return scratch, scratch


def _probe_hook(repo, event, probe, script_path=None, probe_target=None):
    wrapper = script_path or Path(__file__).resolve().parents[2] / "scripts" / probe["script"]
    label = f"hook ({event})"
    if not wrapper.is_file():
        return Finding(label, "error", f"{wrapper} is missing",
                       fix="Reinstall the plugin; the hook script is absent.")

    target, scratch = _probe_target(repo, probe, probe_target=probe_target)
    try:
        return _evaluate_probe(repo, target, wrapper, label, probe)
    finally:
        # Every exit from _evaluate_probe must remove the scratch tree,
        # including the early returns for a subprocess that could not run.
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _evaluate_probe(repo, target, wrapper, label, probe):
    payload = dict(probe["payload"], cwd=str(target))
    environment = {"PATH": os.environ.get("PATH", "")}
    environment.update(probe.get("env", {}))

    before = _probe_marker(target, probe)
    try:
        result = subprocess.run(
            [str(wrapper)], input=json.dumps(payload),
            capture_output=True, text=True, timeout=10, env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Finding(label, "error", f"hook could not be run ({error})",
                       fix="Check python3 is on PATH and the plugin directory is readable.")

    if result.returncode != 0:
        return (Finding(
            label, "error", f"hook exited {result.returncode}: {result.stderr[:200]}",
            fix=probe["if_not"],
        ))

    if "expect" in probe:
        if probe["expect"] in result.stdout:
            return (Finding(label, "ok", probe["proves"]))
        return (Finding(
            label, "error",
            f"ran but did not produce {probe['expect']!r} (stdout={result.stdout[:120]!r})",
            fix=probe["if_not"],
        ))

    if "writes" in probe:
        after = _probe_marker(target, probe)
        # The file must exist AND have changed. Existence alone proves nothing:
        # hooks fail OPEN and exit 0, and the SessionEnd probe runs after
        # PreCompact has already written latest.md.
        if after is not None and after != before:
            return (Finding(label, "ok", probe["proves"]))
        return (Finding(
            label, "error",
            f"ran (exit 0) but wrote nothing to {'/'.join(probe['writes'])}",
            fix=probe["if_not"],
        ))

    if probe.get("injects"):
        handoff_exists = (repo / ".curamoir" / "handoff" / "latest.md").is_file()
        if result.stdout.strip():
            return (Finding(label, "ok", "fires and injects the previous handoff"))
        if handoff_exists:
            # The one case that must fail: a handoff is present, so there WAS
            # something to inject. The wrappers exit 0 when no interpreter is
            # found, so an entirely dead hook used to report ok here.
            return (Finding(
                label, "error",
                "a handoff exists but the hook injected nothing",
                fix=probe["if_not"],
            ))
        return (Finding(
            label, "ok", "fires; nothing to inject yet (no handoff written)",
        ))

    return (Finding(label, "ok", probe["proves"]))


def _check_writable(base):
    """Whether cúramóir can actually write where it needs to.

    Checked separately because the hook probes now run in a scratch repo so they
    cannot destroy the handoff they diagnose — which also means they no longer
    prove the REAL `.curamoir/handoff/` is writable. Without this, doctor would
    report every hook ok on a repo with a read-only `.curamoir/`.
    """
    required = (base, base / "handoff", base / "state", base / "briefs")
    # A directory that does not EXIST was skipped by the old `is_dir()` filter
    # and so contributed nothing, which let an empty `.curamoir/` report
    # "handoff, state and briefs are writable". Reachable whenever something
    # created `.curamoir/` before `init` did.
    missing = [str(path.relative_to(base.parent)) for path in required if not path.is_dir()]
    if missing:
        return Finding(
            "writable state", "error",
            "missing: " + ", ".join(missing),
            fix="Run /curamoir:init to complete the scaffold.",
        )
    unwritable = [
        str(path.relative_to(base.parent))
        for path in required
        if not _can_create_a_file_in(path)
    ]
    if unwritable:
        return Finding(
            "writable state", "error",
            "not writable: " + ", ".join(unwritable),
            fix="Fix the permissions; cúramóir cannot write handoffs, briefs or the ledger.",
        )
    return Finding("writable state", "ok", "handoff, state and briefs are writable")


def _check_gitignore(base):
    """Whether machine-local state is kept out of git.

    `init` writes this file, but `init_repo` refuses an existing `.curamoir/` —
    so no repo initialised before the file existed will ever get one, and the
    journal holds verbatim session text. doctor is the only thing that sees
    those repos again.
    """
    ignore = base / ".gitignore"
    if not ignore.is_file():
        return Finding(
            "gitignore", "warning",
            ".curamoir/.gitignore is missing, so the journal (verbatim session "
            "text) and the lock (a pid) would be committed",
            fix=(
                "Create .curamoir/.gitignore with: handoff/journal.jsonl, "
                "state/lock, curamoir.yaml.new, *.tmp"
            ),
        )
    missing = [
        pattern for pattern in ("handoff/journal.jsonl", "state/lock")
        if pattern not in ignore.read_text(encoding="utf-8", errors="replace")
    ]
    if missing:
        return Finding(
            "gitignore", "warning",
            ".curamoir/.gitignore does not cover " + ", ".join(missing),
            fix="Add those patterns to .curamoir/.gitignore.",
        )
    return Finding("gitignore", "ok", "machine-local state is not committed")


def _can_create_a_file_in(directory):
    """Whether a file can actually be created in `directory`.

    `os.access(path, W_OK)` answers from the permission bits, which lie for
    root and on ACL-governed or read-only-mounted volumes. doctor's own
    principle is that only behaviour proves behaviour, so this writes.
    """
    # A UNIQUE name via mkstemp, not a fixed one. With a fixed name a leftover
    # probe — from a doctor killed between create and unlink, or arriving
    # through `git pull`, since `.curamoir/` is committed — made
    # FileExistsError (an OSError) read as "cannot write", permanently, with a
    # fix the developer cannot act on because the permissions are fine.
    try:
        handle, probe = tempfile.mkstemp(dir=directory, prefix=".curamoir-write-probe")
    except OSError:
        return False
    os.close(handle)
    os.unlink(probe)
    return True


def _check_job_detection():
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
    return Finding(
        check="job detection",
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


#: pyproject's `requires-python`, as a tuple to compare against.
MINIMUM_PYTHON = (3, 12)
#: Short: doctor is interactive, and `bd` reaching a down server fails fast.
_BD_PROBE_SECONDS = 5


def _probe_bd(level, fix, repo=None):
    """`bd`'s health, by asking it something cheap and real.

    Only when the repo HAS a beads database. `filing.beads: auto` explicitly
    supports a repo that does not use beads, and in one `bd ready` exits
    non-zero with "no beads database found" — which this reported as a fault,
    with Dolt advice that cannot help. That is the failure this module's own
    docstring warns against: "reporting either as a failure teaches the
    developer to ignore the output".
    """
    path = shutil.which("bd")
    if path is None:
        return Finding(
            "bd", level,
            "beads filing unavailable; findings will be GitHub-only with a ledger note",
            fix=fix,
        )
    if repo is not None and not (Path(repo) / ".beads").exists():
        return Finding("bd", "ok", "available (this repo has no beads database)")
    try:
        result = subprocess.run(
            [path, "ready", "--json"], capture_output=True, text=True,
            timeout=_BD_PROBE_SECONDS, stdin=subprocess.DEVNULL,
            # cwd=repo, or `/curamoir:doctor --repo /other` probes bd against
            # whatever directory the doctor process happens to be in.
            cwd=str(repo) if repo is not None else None,
        )
    except (OSError, subprocess.SubprocessError):
        return Finding("bd", level, "present but did not respond", fix=fix)
    if result.returncode != 0:
        complaint = (result.stderr or result.stdout or "").strip().splitlines()
        return Finding(
            "bd", level,
            "present but not working: " + (complaint[0] if complaint else "non-zero exit"),
            fix="Check that the Dolt server is running: `bd dolt status`.",
        )
    return Finding("bd", "ok", "available")


#: The candidates the hook wrappers try, in their order. doctor must resolve the
#: interpreter the WRAPPERS will use, not the one named `python3` — on a machine
#: with 3.12 installed alongside an older default, asking only about `python3`
#: reported an error for hooks that work perfectly. The list is duplicated from
#: `scripts/curamoir-hook-*` rather than read from them, because doctor must not
#: depend on parsing shell; a test asserts the two agree.
WRAPPER_INTERPRETERS = ("python3.14", "python3.13", "python3.12", "python3", "python")


def _wrapper_interpreter():
    """The interpreter a hook wrapper would exec, or None if there is none."""
    for candidate in WRAPPER_INTERPRETERS:
        found = shutil.which(candidate)
        if found is not None:
            return found
    return None


def _probe_python(resolved, level, detail, fix):
    """The version the WRAPPERS would use, not the one running this code.

    The fix text has always said "Install Python 3.12 or later" while the check
    only asked whether `python3` resolves — and on a Mac with Command Line
    Tools it resolves to 3.9, which is what the hook wrappers would use.
    """
    if resolved is None:
        return Finding("python3", level, detail, fix=fix)
    try:
        result = subprocess.run(
            [resolved, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return Finding("python3", "warning", "present but would not report its version",
                       fix=fix)
    reported = (result.stdout or "").strip()
    try:
        version = tuple(int(part) for part in reported.split("."))
    except ValueError:
        return Finding("python3", "warning",
                       f"present but reported an unreadable version ({reported!r})", fix=fix)
    if version < MINIMUM_PYTHON:
        return Finding(
            "python3", "error",
            f"python3 on PATH is {reported}, below the 3.12 cúramóir requires",
            fix="Install Python 3.12 or later and ensure it is first on PATH.",
        )
    return Finding("python3", "ok", f"available ({reported})")


def _check_tools(tools, repo=None):
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
         "no python3 (or python) on PATH, so every hook wrapper exits silently",
         "Install Python 3.12 or later and ensure python3 is on PATH."),
    )
    findings = []
    for name, level, detail, fix in specs:
        available = tools.get(name)
        if available is None and name == "bd":
            # `bd` is the one prerequisite that was checked by mere presence,
            # and its documented failure mode is "installed, but the Dolt
            # server is not reachable" — in which case the handoff and the
            # brief read a stale export while doctor calls the setup fine.
            findings.append(_probe_bd(level, fix, repo=repo))
            continue
        if available is None and name == "python3":
            resolved = _wrapper_interpreter()
            findings.append(_probe_python(resolved, level, detail, fix))
            continue
        if available is None:
            # `bd` and `python3` are handled above and `continue` out, so this
            # is the generic arm only. It used to carry a python3 branch that
            # became unreachable when `_probe_python` took over, with the whole
            # rationale for probing the WRAPPERS' interpreter still attached —
            # and a reader who trusts a comment edits the copy it sits on.
            available = shutil.which(name) is not None
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
