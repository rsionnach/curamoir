"""E1-R4: /curamoir:doctor — every problem named, with the fix.

"Actionable" is the requirement: each problem names what to do, not just what is
wrong. A doctor that reports "bd not found" and stops has moved the work rather
than done it.

Degraded is not broken. `bd` absent is a documented mode (filing.beads: auto →
GitHub-only), and ast-grep absent means AST rules skip while regex rules still
run (DEC-3). Both are reported as degraded, not as failures, or the developer
learns to ignore the output.
"""

import pytest

from curamoir.doctor import diagnose, render_report


def initialised(tmp_path):
    from curamoir.scaffold import init_repo

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


def levels(findings):
    return {finding.level for finding in findings}


def by_name(findings, fragment):
    return [f for f in findings if fragment.lower() in f.check.lower()]


def test_a_healthy_repo_reports_no_errors(tmp_path):
    findings = diagnose(initialised(tmp_path), tools={"bd": True, "gh": True, "ast-grep": True,
                                                     "python3": True, "yaml": True, "container": True})

    assert "error" not in levels(findings)


def test_an_uninitialised_repo_is_an_error_that_names_the_fix(tmp_path):
    findings = diagnose(tmp_path, tools={})

    errors = [f for f in findings if f.level == "error"]
    assert errors
    assert any("/curamoir:init" in f.fix for f in errors)


def test_invalid_config_is_an_error_naming_the_offending_key(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "curamoir.yaml").write_text(
        "version: 1\nsweep:\n  max_open_prz: 3\n", encoding="utf-8")

    findings = diagnose(repo, tools={"python3": True, "yaml": True})

    config = by_name(findings, "config")
    assert any(f.level == "error" for f in config)
    assert any("sweep.max_open_prz" in f.detail for f in config)
    assert any(len(f.detail.splitlines()) >= 1 for f in config)


def test_missing_bd_is_degraded_not_an_error(tmp_path):
    """filing.beads: auto means GitHub-only with a ledger note, which is a
    documented mode rather than a failure."""
    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": True, "bd": False})

    bd = by_name(findings, "bd")
    assert bd and bd[0].level == "warning"
    assert "install" in bd[0].fix.lower() or "brew" in bd[0].fix.lower()


def test_missing_ast_grep_is_degraded_and_names_the_install_command(tmp_path):
    """DEC-3: AST rules skip, regex rules still run."""
    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": True, "ast-grep": False})

    found = by_name(findings, "ast-grep")
    assert found and found[0].level == "warning"
    assert "pip install ast-grep-cli" in found[0].fix


def test_missing_pyyaml_is_an_error_because_nothing_can_read_the_config(tmp_path):
    """The deployment risk recorded on this bead: a plugin is an unpacked
    directory with no pip step, so the only runtime dependency is not
    guaranteed. Without it the config cannot be read and nothing works."""
    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": False})

    found = by_name(findings, "pyyaml")
    assert found and found[0].level == "error"
    assert "pip install pyyaml" in found[0].fix


def test_the_hook_is_checked_by_actually_running_it(tmp_path):
    """"Verify each hook fires (dry-run)" — assert on behaviour, not on the
    presence of hooks.json, which proves nothing about whether it works."""
    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": True})

    hook = by_name(findings, "hook")
    assert hook, "doctor did not check the hook at all"
    assert hook[0].level == "ok", hook[0].detail


def test_a_paused_repo_is_surfaced_because_it_explains_everything_else(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "PAUSE").write_text("", encoding="utf-8")

    findings = diagnose(repo, tools={"python3": True, "yaml": True})

    paused = by_name(findings, "pause")
    assert paused and paused[0].level == "warning"
    assert "PAUSE" in paused[0].fix


def test_every_problem_carries_a_fix(tmp_path):
    """The requirement is actionable output. A finding without a fix is a
    complaint."""
    findings = diagnose(tmp_path, tools={})

    for finding in findings:
        if finding.level in ("error", "warning"):
            assert finding.fix.strip(), f"{finding.check} has no fix"


def test_the_report_groups_by_level_and_leads_with_errors(tmp_path):
    report = render_report(diagnose(tmp_path, tools={}))

    assert "error" in report.lower()
    first_error = report.lower().index("error")
    assert first_error < len(report) // 2, "errors must lead the report"


def test_modules_detected_is_reported_so_bad_detection_is_visible(tmp_path):
    """Wrong module detection silently ruins grading and review selection, so
    doctor must show what it found."""
    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": True})

    modules = by_name(findings, "module")
    assert modules
    assert "src/app" in modules[0].detail


def test_doctor_checks_every_registered_hook_not_just_one(tmp_path):
    """E1-R4: "verify EACH hook fires (dry-run)". Epic 8 added four hook events
    on the same branch and doctor was not revisited, so four of five were not
    reported at all — neither ok nor absent."""
    import json
    from pathlib import Path

    registered = json.loads(
        (Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"].keys()

    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": True})
    checked = " ".join(f.check for f in findings)

    for event in registered:
        assert event in checked, f"doctor never mentions the {event} hook"


def test_every_hook_check_actually_runs_the_hook(tmp_path):
    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": True})

    hooks = [f for f in findings if "hook" in f.check.lower()]
    assert len(hooks) >= 5, [f.check for f in hooks]
    for finding in hooks:
        assert finding.level == "ok", f"{finding.check}: {finding.detail}"


def test_a_hook_that_writes_nothing_fails_its_probe(tmp_path):
    """The `writes` probe read `after != before OR after is not None`, so the
    second disjunct made the comparison irrelevant: if the file merely EXISTED
    the probe passed. Hooks fail open and exit 0, so a hook that died internally
    still reported `ok` — and the SessionEnd probe runs after PreCompact has
    written latest.md, so it could never fail for the reason it exists to catch.
    """
    from curamoir.doctor import _run_hook_probe

    repo = initialised(tmp_path)
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# pre-existing\n", encoding="utf-8")

    stub = tmp_path / "does-nothing"
    stub.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    finding = _run_hook_probe(repo, "SessionEnd", {
        "script": "does-nothing", "payload": {"hook_event_name": "SessionEnd"},
        "writes": ("handoff", "latest.md"), "proves": "x", "if_not": "y",
    }, script_path=stub)

    assert finding.level == "error", finding
    assert "missing" not in finding.detail.lower(), (
        "failed because the script was absent, not because nothing was written — "
        "this test would pass against the bug it is meant to catch"
    )
    assert "wrote nothing" in finding.detail.lower(), finding.detail


def test_the_sessionstart_probe_distinguishes_injecting_from_merely_running(tmp_path):
    """`expect_or_empty` was declared and never consumed, so the probe fell
    through to an unconditional `ok` — it proved the script was executable and
    nothing more.

    Tested at probe level because doctor's own PreCompact probe writes a handoff,
    so through `diagnose` both states look alike. That coupling is itself why
    SessionStart is now probed before the probes that write.
    """
    from curamoir.doctor import _HOOK_PROBES, _run_hook_probe

    probe = _HOOK_PROBES["SessionStart"]

    bare = initialised(tmp_path)
    without = _run_hook_probe(bare, "SessionStart", probe)

    handoff = bare / ".curamoir" / "handoff" / "latest.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# handoff\n\nbranch: main\n", encoding="utf-8")
    with_handoff = _run_hook_probe(bare, "SessionStart", probe)

    assert without.level == "ok" and with_handoff.level == "ok"
    assert without.detail != with_handoff.detail, (
        "the probe reports the same thing whether or not anything was injected"
    )
    assert "inject" in with_handoff.detail.lower()


def test_doctor_discloses_that_the_denylist_cannot_detect_a_job_yet(tmp_path):
    """The denylist enforces only inside a cúramóir job, detected by the
    CURAMOIR_JOB env var with the supervisor lock as fallback (DEC-5, because
    CLAUDE_CODE_SUBPROCESS_ENV_SCRUB can strip env). But nothing writes a lock
    yet (E10-LOCK), so under a scrubbed environment the denylist silently stops
    enforcing.

    A guardrail with an undisclosed silent-failure mode is worse than an absent
    one, so doctor must say so until E10-LOCK lands.
    """
    findings = diagnose(initialised(tmp_path), tools={"python3": True, "yaml": True})

    disclosure = [f for f in findings if "denylist" in f.check.lower()]
    assert disclosure, [f.check for f in findings]
    assert disclosure[0].level == "warning"
    assert "scrub" in disclosure[0].detail.lower() or "env" in disclosure[0].detail.lower()
    assert disclosure[0].fix


def test_the_writes_probe_distinguishes_wrote_from_did_not_write(tmp_path):
    """The predicate's `after != before` half had NO test.

    The previous test pre-created latest.md inside `repo`, but `_probe_repo`
    redirects every writes-probe to a fresh scratch tree — so in the probe's real
    target both before and after were None, and the fixed form and the buggy
    `after != before or after is not None` form evaluated identically. Restoring
    the exact original bug left the full 506-test suite green.

    Three states, against a target the test controls."""
    from curamoir.doctor import _run_hook_probe

    repo = initialised(tmp_path)
    writer = tmp_path / "writes"
    writer.write_text(
        "#!/usr/bin/env bash\n"
        'cwd=$(python3 -c "import json,sys;print(json.load(sys.stdin)[\'cwd\'])")\n'
        'mkdir -p "$cwd/.curamoir/handoff" && date +%s%N > "$cwd/.curamoir/handoff/latest.md"\n',
        encoding="utf-8")
    writer.chmod(0o755)
    silent = tmp_path / "silent"
    silent.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    silent.chmod(0o755)

    probe = {"script": "x", "payload": {"hook_event_name": "SessionEnd"},
             "writes": ("handoff", "latest.md"), "proves": "wrote", "if_not": "fix"}

    # (a) nothing there, hook writes -> ok
    assert _run_hook_probe(repo, "SessionEnd", probe, script_path=writer).level == "ok"

    # (b) file PRESENT and UNCHANGED, hook writes nothing -> error. This is the
    # state the old test could not construct, and the one the bug let through.
    target = tmp_path / "preexisting"
    (target / ".curamoir" / "handoff").mkdir(parents=True)
    (target / ".curamoir" / "handoff" / "latest.md").write_text("already here\n", encoding="utf-8")
    finding = _run_hook_probe(repo, "SessionEnd", probe, script_path=silent, probe_target=target)
    assert finding.level == "error", finding
    assert "wrote nothing" in finding.detail.lower()

    # (c) file present, hook rewrites it -> ok
    assert _run_hook_probe(
        repo, "SessionEnd", probe, script_path=writer, probe_target=target
    ).level == "ok"


def test_doctor_leaves_the_real_handoff_byte_identical(tmp_path):
    """_probe_repo's central safety property had no test: deleting the scratch
    redirect entirely left the suite green, while the docstring records doctor
    having overwritten latest.md, fabricated journal notes that became the next
    handoff's "Last decision", and burned two of ten history slots per run."""
    repo = initialised(tmp_path)
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# MINE\n\ndo not touch\n", encoding="utf-8")
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    journal.write_text('{"at":"t","note":"mine"}\n', encoding="utf-8")
    before = (handoff.read_bytes(), handoff.stat().st_mtime_ns,
              journal.read_bytes(), journal.stat().st_mtime_ns)

    diagnose(repo, tools={"python3": True, "yaml": True})

    assert (handoff.read_bytes(), handoff.stat().st_mtime_ns,
            journal.read_bytes(), journal.stat().st_mtime_ns) == before
    assert list((repo / ".curamoir" / "handoff" / "history").glob("*.md")) == []


def test_fewer_than_three_modules_is_reported_as_a_warning(tmp_path):
    """The warning explains E5-R3's round-robin fallback — the thing the shared
    fixture exists to avoid — but no test checked the LEVEL, only the text, so
    disabling the branch entirely stayed green."""
    repo = initialised(tmp_path)  # one module: src/app

    findings = diagnose(repo, tools={"python3": True, "yaml": True})
    modules = [f for f in findings if f.check == "modules"]

    assert modules and modules[0].level == "warning", modules
    assert "round-robin" in modules[0].fix.lower()


def test_a_registered_event_with_no_probe_is_a_warning_not_ok(tmp_path):
    """The probe table was made data-driven so a new event could not go
    unchecked — but the warning for an unprobed event was itself untested, so a
    sixth event would have reported healthy."""
    from curamoir import doctor

    repo = initialised(tmp_path)
    original = doctor._registered_hook_events

    try:
        doctor._registered_hook_events = lambda: ["PreToolUse", "FutureEvent"]
        findings = doctor._check_hooks(repo)
    finally:
        doctor._registered_hook_events = original

    future = [f for f in findings if "FutureEvent" in f.check]
    assert future and future[0].level == "warning", findings
    assert "no probe" in future[0].detail.lower()


def test_an_unwritable_state_directory_is_reported(tmp_path):
    """_check_writable exists BECAUSE the hook probes moved to a scratch tree and
    therefore stopped proving the real .curamoir/ is writable. Nothing chmodded
    anything, so flipping its condition to False stayed green."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores mode bits")

    repo = initialised(tmp_path)
    handoff = repo / ".curamoir" / "handoff"
    handoff.mkdir(parents=True, exist_ok=True)
    os.chmod(handoff, 0o500)
    try:
        findings = diagnose(repo, tools={"python3": True, "yaml": True})
    finally:
        os.chmod(handoff, 0o700)

    writable = [f for f in findings if f.check == "writable state"]
    assert writable and writable[0].level == "error", writable
    assert "handoff" in writable[0].detail
