"""E1-R3: /curamoir:status — the whole picture, locally, in under two seconds.

No model calls and no network by default. Those are not performance notes, they
are the feature: a status you can only run deliberately is one you stop running,
and this command's job is to be the cheapest possible way back into a repo.

Collection is separated from rendering so the constraints are testable and so
later consumers — the away-brief (E9-R2) in particular — can read the same
structure rather than scraping text.

Sections whose producing epic has not landed report absence rather than erroring
or inventing a zero. This command ships in M0, before grades (E7), the runner
(E10) and the sweep (Epic 3).
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from curamoir.budget import PriceTable, UnknownModel, combined_shortfall, month_to_date
from curamoir.cadence import scheduled_jobs
from curamoir.config import DEFAULTS, ConfigError, load_config_text
from curamoir.grades import graded_modules
from curamoir.ledger import Ledger

NOT_AVAILABLE = "not yet available"


def collect_status(repo, open_pr_branches=None, now=None):
    """Everything the status command reports, as plain data.

    `open_pr_branches` is optional and injected. Omitting it means no `gh` call
    is made at all, which is the default: the two-second promise cannot survive
    an unbounded network call.
    """
    repo = Path(repo)
    now = now or (lambda: datetime.now(timezone.utc))
    base = repo / ".curamoir"

    if not base.is_dir():
        return {"initialised": False, "repo": str(repo)}

    config, config_error = _read_config(base)
    return {
        "initialised": True,
        "repo": str(repo),
        "config_error": config_error,
        "paused": (base / "PAUSE").exists(),
        "lock": _read_lock(base),
        "spend": _read_spend(base, config, now),
        "grades": _read_json(base / "state" / "grades.json"),
        "cadence": _read_json(base / "state" / "cadence.json"),
        "handoff": _read_handoff_age(base, now),
        "open_prs": _read_open_prs(open_pr_branches, config),
    }


def render_status(status):
    """A compact human rendering. Deliberately plain text: no colour, no boxes,
    nothing that breaks when piped or pasted into an issue."""
    if not status.get("initialised"):
        return (
            f"cúramóir is not initialised in {status['repo']}.\n"
            "Run /curamoir:init to scaffold .curamoir/."
        )

    lines = [f"cúramóir — {status['repo']}", ""]

    if status.get("config_error"):
        lines += ["  config:    INVALID", f"             {status['config_error']}", ""]

    lines.append(f"  state:     {'PAUSED (.curamoir/PAUSE present)' if status['paused'] else 'running'}")

    lock = status["lock"]
    if lock["held"] and lock.get("detail"):
        # Held but unreadable: the detail is the only thing that explains it.
        # Rendering the holder fields instead produced "held by None, pid None on
        # None", which tells the developer nothing about why.
        lines.append(f"  lock:      {lock['detail']}")
    elif lock["held"]:
        lines.append(f"  lock:      held by {lock['job']!r}, pid {lock['pid']} on {lock['host']}")
    else:
        lines.append(f"  lock:      {lock['detail'] or 'free'}")

    spend = status["spend"]
    if spend["unavailable"]:
        lines.append(f"  spend:     unavailable — {spend['detail']}")
    else:
        headroom = spend["ceiling_usd"] - spend["month_to_date_usd"]
        lines.append(
            f"  spend:     ${spend['month_to_date_usd']:.2f} of "
            f"${spend['ceiling_usd']:.2f} this month (${headroom:.2f} left)"
        )
        if spend.get("detail"):
            lines.append(f"             ⚠ {spend['detail']}")

    handoff = status["handoff"]
    if handoff["age_days"] is None:
        lines.append("  handoff:   never written")
    else:
        lines.append(f"  handoff:   {handoff['age_days']} day(s) old")

    lines.append(f"  grades:    {_summarise_grades(status['grades'])}")
    lines.append(f"  next jobs: {_summarise_cadence(status['cadence'])}")

    prs = status["open_prs"]
    if not prs["checked"]:
        lines.append("  open PRs:  not checked (no network call made)")
    elif prs["unavailable"]:
        lines.append(f"  open PRs:  unavailable — {prs['detail']}")
    else:
        lines.append(f"  open PRs:  {len(prs['ours'])} of {prs['cap']} ({', '.join(prs['ours']) or 'none'})")

    return "\n".join(lines)


def _read_config(base):
    path = base / "curamoir.yaml"
    if not path.is_file():
        # deepcopy, not the module-level object: handing it out is one careless
        # mutation away from poisoning every later load_config in the process.
        return deepcopy(DEFAULTS), f"{path.name} is missing; showing built-in defaults"
    try:
        return load_config_text(path.read_text(encoding="utf-8")), None
    except ConfigError as invalid:
        return deepcopy(DEFAULTS), str(invalid).replace("\n", " ")


def _read_lock(base):
    path = base / "state" / "lock"
    if not path.exists():
        return {"held": False, "detail": None, "job": None, "pid": None, "host": None}
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        return {
            "held": True, "detail": None,
            "job": held.get("job"), "pid": held.get("pid"), "host": held.get("host"),
            "started": held.get("started"),
        }
    except (ValueError, OSError) as error:
        # Present but unreadable is not free. Say so rather than implying free.
        return {
            "held": True, "detail": f"lock present but unreadable ({error})",
            "job": None, "pid": None, "host": None,
        }


def _read_spend(base, config, now):
    """Month-to-date spend against the ceiling, or why it is unavailable."""
    ceiling = config["budget"]["monthly_ceiling_usd"]
    ledger = Ledger(base / "state" / "ledger.jsonl")
    try:
        spend = month_to_date(ledger, PriceTable(config["budget"]["prices"]), now())
    except UnknownModel as unknown:
        return {
            "unavailable": True, "month_to_date_usd": None, "ceiling_usd": ceiling,
            "unreadable_lines": ledger.unreadable_lines(),
            "detail": f"ledger names unpriced model {unknown.args[0]!r}",
        }

    # Lines that would not parse PLUS entries that could not be attributed:
    # both mean some of the file did not reach the total, which is the one
    # thing the caveat below has to say.
    unreadable = combined_shortfall(ledger.unreadable_lines(), spend.unaccounted)
    return {
        "unavailable": False, "month_to_date_usd": round(spend.usd, 2),
        "ceiling_usd": ceiling,
        "unreadable_lines": unreadable,
        "detail": (
            f"{unreadable} unreadable ledger line(s): this total is a lower bound"
            if unreadable else None
        ),
    }


def _read_handoff_age(base, now):
    path = base / "handoff" / "latest.md"
    if not path.is_file():
        # None, never 0: zero would read as "written just now", the opposite of
        # the truth and the worst thing to tell someone returning.
        return {"age_days": None, "path": str(path)}
    written = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    # Clamped at 0, as `brief._absence_days` already was. Clock skew, a
    # restored backup or a synced mount gives a file an mtime in the future,
    # and "-3 day(s) old" tells the reader nothing they can act on.
    return {"age_days": max(0, (now() - written).days), "path": str(path)}


def _read_open_prs(open_pr_branches, config):
    prefix = config["guardrails"]["branch_prefix"]
    cap = config["sweep"]["max_open_prs"]
    if open_pr_branches is None:
        return {"checked": False, "unavailable": False, "ours": [], "cap": cap, "detail": None}
    try:
        branches = list(open_pr_branches() or [])
    except Exception as error:
        return {
            "checked": True, "unavailable": True, "ours": [], "cap": cap,
            "detail": str(error),
        }
    return {
        "checked": True, "unavailable": False, "cap": cap, "detail": None,
        "ours": [name for name in branches if name.startswith(prefix)],
    }


def _read_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _summarise_grades(grades):
    graded = graded_modules(grades)
    if not graded:
        return NOT_AVAILABLE + " (no grades computed yet — Epic 7)"
    return ", ".join(
        f"{module} {data.get('grade', '?')}" for module, data in sorted(graded.items())
    )


def _summarise_cadence(cadence):
    scheduled = scheduled_jobs(cadence)
    if not scheduled:
        return NOT_AVAILABLE + " (no cadence recorded yet — Epic 10)"
    return ", ".join(f"{name} {summary}" for name, summary in sorted(scheduled.items()))


