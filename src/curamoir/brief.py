"""E9: the away-brief — the artefact that makes cúramóir legible to its audience.

This is what a developer reads after three weeks away. §22 ships it in M1, before
sweeps, reviews, grades or spend data exist, so every section renders honestly
when its source epic has not landed. "0 findings" would be a lie; "not available
yet" is the truth, and the difference matters to someone deciding whether to trust
the thing.

Section 6 is the point of the whole epic: exactly ONE suggested first task,
chosen by priority then smallness, with the reason it was chosen. Not three. A
list is a backlog, and a backlog is what the developer already had.

Capped at 600 words (E9-R4), plain markdown with no plugin-only links (E9-R5),
because it may be read on a phone or as a GitHub issue with cúramóir nowhere in
sight.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from curamoir.redaction import write_artefact
from curamoir.repo_config import config_value

WORD_LIMIT = 600
MAX_FIRST_TASK_MINUTES = 90
DEFAULT_THRESHOLD_DAYS = 5
NOT_YET = "not available yet"
# Sentinel for "no handoff at all". Large enough to trigger a brief, and never
# rendered as a number — "Away 10000 day(s)" is a falsehood on the face of the
# artefact a stranger is most likely to read.
UNKNOWN_ABSENCE = 10_000


def due_for_brief(repo, threshold_days=DEFAULT_THRESHOLD_DAYS, cadence="on_return", now=None):
    """Whether a brief should be generated.

    `cadence: weekly` is unconditional — it is a documented config value (§6.3),
    so it has to actually do something rather than silently behaving like
    `on_return`.
    """
    if cadence == "weekly":
        return True
    return _absence_days(Path(repo), now) >= threshold_days


def due_for_brief_from_config(repo, now=None):
    """`due_for_brief` with the repo's own cadence and threshold.

    E9-R1's threshold and `cadence.brief` were previously only arguments to a
    Python function, so the documented `weekly` mode was unreachable however it
    was configured.
    """
    return due_for_brief(
        repo,
        threshold_days=config_value(
            repo, "handoff", "brief_threshold_days", default=DEFAULT_THRESHOLD_DAYS
        ),
        cadence=config_value(repo, "cadence", "brief", default="on_return"),
        now=now,
    )


def build_brief(repo, now=None):
    """Everything the brief reports, as plain data."""
    repo = Path(repo)
    base = repo / ".curamoir"
    grades = _read_json(base / "state" / "grades.json")

    return {
        "at": (now() if now else datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "absence_days": _absence_days(repo, now),
        "jobs": _read_jobs(base),
        "spend": _read_period_spend(base),
        "changed": _read_changed(base),
        "findings": _read_findings(base),
        "grades": grades,
        "blocked_on_you": _read_blocked(base),
        "start_here": _choose_first_task(repo),
        "lowest_graded": _lowest_graded(grades),
    }


def render_brief(brief):
    """Markdown, six fixed sections in order, capped at WORD_LIMIT words."""
    lines = [
        f"# cúramóir brief — {brief['at'][:10]}",
        "",
        "## Since you were here",
        "",
        f"- Away {brief['absence_days']} day(s)."
        if brief["absence_days"] < UNKNOWN_ABSENCE
        else "- No previous handoff, so the length of the absence is unknown.",
    ]
    lines.append(f"- Jobs run: {brief['jobs'] if brief['jobs'] is not None else NOT_YET}.")
    lines.append(f"- Spend this period: {brief['spend'] if brief['spend'] is not None else NOT_YET}.")

    lines += ["", "## What changed", ""]
    lines += _bullets(brief["changed"], "No sweep PRs — " + NOT_YET + ".")

    lines += ["", "## What was found", ""]
    lines += _bullets(brief["findings"], "No review findings — " + NOT_YET + ".")

    lines += ["", "## Grades", ""]
    if not brief["grades"] or not brief["grades"].get("modules"):
        lines.append(f"Grades {NOT_YET}.")
    else:
        lines.append("| module | grade |")
        lines.append("|---|---|")
        for module, data in sorted(brief["grades"]["modules"].items()):
            lines.append(f"| {module} | {data.get('grade', '?')} |")

    lines += ["", "## Blocked on you", ""]
    lines += _bullets(brief["blocked_on_you"], "Nothing is waiting on you.")

    lines += ["", "## Start here", ""]
    first = brief["start_here"]
    if first:
        estimate = f", ~{first['estimate']} min" if first.get("estimate") else ""
        lines.append(f"**{first['id']}** — {first['title']}{estimate}.")
        lines.append("")
        lines.append(f"Chosen because {first['reason']}.")
    else:
        lines.append(
            f"No ready bead estimated at {MAX_FIRST_TASK_MINUTES} minutes or less."
        )
        if brief["lowest_graded"]:
            lines.append("")
            lines.append(
                f"Run `/curamoir:review {brief['lowest_graded']}` instead — it is the "
                "lowest-graded module."
            )
        else:
            lines.append("")
            lines.append("Run `/curamoir:review` to pick a module to look at.")

    return _cap_words("\n".join(lines), WORD_LIMIT)


def _choose_first_task(repo):
    """Exactly one ready bead, by priority then smallness.

    `in_progress` is excluded: the requirement says "a ready bead", and something
    already claimed is not a fresh start — offering it back would be telling the
    developer to resume whatever they abandoned, which is the opposite of help.
    """
    candidates = [
        bead for bead in _read_beads(repo)
        if bead.get("status") == "open"
        # Numeric, explicitly: a bead with "estimate": "30" raised TypeError out
        # of the sort and crashed /curamoir:brief.
        and isinstance(bead.get("estimate"), (int, float))
        and not isinstance(bead.get("estimate"), bool)
        and bead["estimate"] <= MAX_FIRST_TASK_MINUTES
    ]
    if not candidates:
        return None

    def rank(bead):
        priority = bead.get("priority", 9)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            priority = 9
        return (priority, bead["estimate"])

    candidates.sort(key=rank)
    best = candidates[0]
    return {
        "id": best.get("id", "?"),
        "title": best.get("title", ""),
        "estimate": best.get("estimate"),
        "reason": (
            f"it is the highest-priority ready bead (P{best.get('priority', '?')}) that "
            f"fits in {MAX_FIRST_TASK_MINUTES} minutes"
        ),
    }


def _absence_days(repo, now=None):
    moment = now() if now else datetime.now(timezone.utc)
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    if not handoff.is_file():
        # No handoff is either a fresh install or a very long absence. Both are
        # exactly when a brief helps, so report an absence that triggers one.
        return UNKNOWN_ABSENCE
    written = datetime.fromtimestamp(handoff.stat().st_mtime, tz=timezone.utc)
    return max(0, (moment - written).days)


def _read_beads(repo):
    path = repo / ".beads" / "issues.jsonl"
    if not path.is_file():
        return []
    beads = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                beads.append(json.loads(line))
    except (ValueError, OSError):
        return []
    return beads


def _read_jobs(base):
    """Job count from the ledger, or None when the runner has never run."""
    path = base / "state" / "ledger.jsonl"
    if not path.is_file():
        return None
    try:
        return sum(
            1 for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("kind") == "job"
        )
    except (ValueError, OSError):
        return None


def _read_period_spend(base):
    """Spend over the absence, or None until E11-R1 attributes cost.

    Deliberately NOT `status._read_spend`, which is month-to-date against the
    ceiling: a brief reports what this absence cost. Reporting $0.00 before
    anything is tracked would understate rather than inform.
    """
    return None


def _read_changed(base):
    """Sweep PRs. None until Epic 3 exists."""
    return None


def _read_findings(base):
    """Review findings grouped by lens and severity. None until Epic 5 exists."""
    return None


def _read_blocked(base):
    """Promotion candidates, PRs awaiting review, issues needing a decision.

    Reads what exists now: the PAUSE file is the one thing that can already be
    blocking a returning developer, and it explains everything else being empty.
    """
    blocked = []
    if (base / "PAUSE").exists():
        blocked.append(
            "cúramóir is PAUSED (.curamoir/PAUSE). Nothing ran. Delete the file to resume."
        )
    return blocked or None


def _lowest_graded(grades):
    if not grades or not grades.get("modules"):
        return None
    ranking = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}

    def rank(item):
        data = item[1]
        grade = data.get("grade") if isinstance(data, dict) else None
        # An unrecognised or missing grade ranks BEST-known, not worst. Ranking
        # it 9 with reverse=True reported a typo'd grade as the lowest-graded
        # module, ahead of a genuine F — so the brief would send the developer
        # to the wrong place.
        return ranking.get(grade, -1)

    modules = sorted(grades["modules"].items(), key=rank, reverse=True)
    return modules[0][0] if modules and rank(modules[0]) >= 0 else None


def _bullets(items, empty_message):
    if not items:
        return [empty_message]
    return [f"- {item}" for item in items]


def _read_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _cap_words(text, limit):
    """Link out rather than truncate. E9-R4: "Anything beyond links out."

    A section cut mid-sentence is worse than a short brief, so whole lines are
    dropped and a pointer to the full state replaces them.
    """
    if len(text.split()) <= limit:
        return text
    kept, used = [], 0
    for line in text.splitlines():
        count = len(line.split())
        if used + count > limit - 25:
            break
        kept.append(line)
        used += count
    kept += [
        "",
        "_This brief reached its length limit. Full state: `/curamoir:status`._",
    ]
    return "\n".join(kept)


def write_brief(repo, now=None, post_issue=None, post=False):
    """Write `briefs/latest.md`, and optionally post it as a GitHub issue.

    Through E0-R7's redacting write path. This is the artefact where a leak costs
    most: it is committed, and it may become a PUBLIC issue so it can be read on a
    phone (E9-R3).

    `post_issue` is injected and only called when `post` is true. Posting by
    default would publish to a repository the developer has not agreed to publish
    to. A failed post never costs us the file — the file is the deliverable and
    the issue is a convenience.
    """
    repo = Path(repo)
    brief = build_brief(repo, now=now)
    rendered = render_brief(brief)
    path = write_artefact(repo / ".curamoir" / "briefs" / "latest.md", rendered)

    if post and post_issue is not None:
        try:
            post_issue(f"cúramóir brief — {brief['at'][:10]}", rendered)
        except Exception:
            # Recorded by the caller's ledger line; never fatal here.
            pass
    return path
