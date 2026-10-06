"""E1-R4: /curamoir:doctor — every problem named, with the fix.

"Actionable" is the requirement: each problem names what to do, not just what is
wrong. A doctor that reports "bd not found" and stops has moved the work rather
than done it.

Degraded is not broken. `bd` absent is a documented mode (filing.beads: auto →
GitHub-only), and ast-grep absent means AST rules skip while regex rules still
run (DEC-3). Both are reported as degraded, not as failures, or the developer
learns to ignore the output.
"""

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
