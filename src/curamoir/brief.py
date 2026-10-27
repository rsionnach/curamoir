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
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from curamoir import github, publish
from curamoir.grades import graded_modules
from curamoir.idempotence import sweep_prs
from curamoir.ledger import parse_ledger_timestamp, repo_ledger
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
NO_SWEEP_PRS = "No sweep PRs since you were here."
#: How "What changed" names a PR's state, from gh's word for it. CLOSED is
#: absent on purpose: a PR closed without merging changed nothing.
STATE_WORDS = {"OPEN": "open", "MERGED": "merged"}


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


def build_brief(repo, now=None, pr_states=None):
    """Everything the brief reports, as plain data.

    `pr_states` is gh's PR lookup for "What changed" (`gh_pr_states`), a
    callable taking no arguments; None, and every PR's state is unknown.
    """
    repo = Path(repo)
    base = repo / ".curamoir"
    grades = _read_json(base / "state" / "grades.json")

    return {
        "at": (now() if now else datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "absence_days": _absence_days(repo, now),
        "jobs": _read_jobs(base),
        "spend": _read_period_spend(base),
        "changed": _read_changed(repo, pr_states),
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
    lines += _bullets(brief["changed"], NO_SWEEP_PRS)

    lines += ["", "## What was found", ""]
    lines += _bullets(brief["findings"], "No review findings — " + NOT_YET + ".")

    lines += ["", "## Grades", ""]
    graded = graded_modules(brief["grades"])
    if not graded:
        lines.append(f"Grades {NOT_YET}.")
    else:
        lines.append("| module | grade |")
        lines.append("|---|---|")
        for module, data in sorted(graded.items()):
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
    written = _handoff_time(repo)
    if written is None:
        # No handoff is either a fresh install or a very long absence. Both are
        # exactly when a brief helps, so report an absence that triggers one.
        return UNKNOWN_ABSENCE
    return max(0, (moment - written).days)


def _handoff_time(repo):
    """When the last handoff was written, or None when there is none: the
    start of the absence."""
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    if not handoff.is_file():
        return None
    return datetime.fromtimestamp(handoff.stat().st_mtime, tz=timezone.utc)


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


def gh_pr_states(repo):
    """The `pr_states` lookup for the repository at `repo`: gh's, of
    origin's repository, as the sweep that opened the PRs asked it."""
    repo = Path(repo)

    def lookup():
        return github.pr_states(repo, gh_repo=publish.origin_gh_repo(repo))
    return lookup


def _read_changed(repo, pr_states):
    """One line per sweep PR from this absence (E9-R2 §2): opened during it,
    merged during it, or still open. A PR closed without merging is left
    out, and with no handoff every PR counts.

    The ledger says which PRs the sweep opened (each sweep line's `pr`, as
    idempotence.sweep_prs reads it, and the `fixes` it recorded); gh says
    what became of them, asked once and only when there is a PR to ask
    about. A PR gh does not list, or lists on another branch, has an
    unknown state, and so does every PR when gh fails: the brief is the
    deliverable, so a gh failure only costs it the states. A PR of unknown
    state opened before the absence is left out: nothing says it changed.
    """
    prs = _sweep_prs(repo_ledger(repo).entries())
    if not prs:
        return []
    states = {}
    if pr_states is not None:
        # Exception, not GhError: the lookup also reads origin's URL
        # (PublishRefused), and nothing it raises may cost the brief.
        with suppress(Exception):
            answered = pr_states()
            states = answered if isinstance(answered, Mapping) else {}
    since = _handoff_time(repo)
    lines = []
    for pr in prs:
        known = states.get(pr["number"])
        if not (isinstance(known, Mapping) and known.get("branch") == pr["branch"]):
            known = {}
        state = known.get("state")
        if state == "CLOSED":
            continue
        merged_at = known.get("merged_at")
        recent = (since is None or state == "OPEN"
                  or (pr["opened"] is not None and pr["opened"] >= since)
                  or (isinstance(merged_at, datetime) and merged_at >= since))
        if recent:
            lines.append(_changed_line(pr, STATE_WORDS.get(state, "state unknown")))
    return lines


def _sweep_prs(entries):
    """The sweep's PRs, oldest number first, each as its ledger `pr` record
    plus `opened` (the earliest `at` of a line carrying it, or None) and
    `fixes` (the latest such line's `fixes` list, or None: a line written
    after the PR opened but before its fixes were recorded has none)."""
    records = {branch: dict(record, opened=None, fixes=None)
               for branch, record in sweep_prs(entries).items() if record is not None}
    for entry in entries:
        if not (isinstance(entry, Mapping) and entry.get("job") == "sweep"
                and entry.get("kind") == "job" and isinstance(entry.get("pr"), Mapping)):
            continue
        record = records.get(entry["pr"].get("branch"))
        if record is None:
            continue
        at = parse_ledger_timestamp(entry.get("at"))
        if at is not None and (record["opened"] is None or at < record["opened"]):
            record["opened"] = at
        if isinstance(entry.get("fixes"), list):
            record["fixes"] = entry["fixes"]
    return sorted(records.values(), key=lambda record: record["number"])


def _changed_line(pr, state):
    """`#12, merged: 3 fixes of 2 principles, opened 2026-10-20. <url>`"""
    fixes = pr["fixes"]
    if fixes is None:
        counted = "fixes not recorded"
    else:
        principles = {fix.get("principle_id") for fix in fixes if isinstance(fix, Mapping)}
        counted = (f"{len(fixes)} {'fix' if len(fixes) == 1 else 'fixes'} of "
                   f"{len(principles)} {'principle' if len(principles) == 1 else 'principles'}")
    opened = f", opened {pr['opened'].date().isoformat()}" if pr["opened"] else ""
    url = f" {pr['url']}" if pr["url"] else ""
    return f"#{pr['number']}, {state}: {counted}{opened}.{url}"


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
    graded = graded_modules(grades)
    if not graded:
        return None
    ranking = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}

    def rank(item):
        grade = item[1].get("grade")
        # An unrecognised or missing grade ranks BEST-known, not worst. Ranking
        # it 9 with reverse=True reported a typo'd grade as the lowest-graded
        # module, ahead of a genuine F — so the brief would send the developer
        # to the wrong place.
        return ranking.get(grade, -1)

    modules = sorted(graded.items(), key=rank, reverse=True)
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
    """Markdown capped at `limit` words, stopping at the first line that does
    not fit.

    `break`, not `continue` — the opposite of `handoff._cap_words`, and
    deliberately so. A handoff must keep its short structural fields even when
    one long line does not fit; a brief links out instead (E9-R4), so stopping
    cleanly beats keeping fragments from further down.
    """
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


def write_brief(repo, now=None, post_issue=None, post=False, pr_states=None):
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
    brief = build_brief(repo, now=now, pr_states=pr_states)
    rendered = render_brief(brief)
    path = write_artefact(repo / ".curamoir" / "briefs" / "latest.md", rendered)

    if post and post_issue is not None:
        # suppress(Exception): the brief is already on disk by now, and a failed
        # post must not lose it. Recorded by the caller's ledger line.
        with suppress(Exception):
            post_issue(f"cúramóir brief — {brief['at'][:10]}", rendered)
    return path
