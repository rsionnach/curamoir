"""Reading a cadence file that may not have been written by us.

Epic 10 owns the runner that writes `cadence.json` and has not landed, so every
one in existence today is hand-written — the same position `grades.json` was in
when it got `grades.graded_modules`, and this module is deliberately that
module's twin. `/curamoir:status`'s contract is that a section whose producing
epic has not landed reports ABSENCE rather than erroring, which `status.py` and
`grades.py` both state; cadence was the one file left indexing straight into
`cadence.items()`, so a hand-written list reached the slash command as an
AttributeError traceback.

The shape is per-module, not per-job. §6.1 describes the file as "per-module
review interval and last-run" and E5-R4 stores back-off there: two consecutive
clean reviews double a module's interval, a P0/P1 finding resets it. So an entry
may be a mapping with `interval` and `last_run`, and a flat `{job: when}` — what
status assumed, and plausible in a hand-written file — stays readable too.
"""


def scheduled_jobs(cadence):
    """`{name: summary}` for every entry that can be summarised, else `{}`.

    Lenient by design, for the same reason `graded_modules` is: a cadence file
    the current schema does not recognise is the same situation as no cadence
    file, and both must render as absence rather than as an error.

    The value is always a string, so no caller can render a dict by accident —
    which is what produced `sweep: {'next': ..., 'last': ...}` in the output of
    the one command whose whole job is a fast way back into a repository.
    """
    if not isinstance(cadence, dict):
        return {}
    summaries = {}
    for name, entry in cadence.items():
        if not isinstance(name, str):
            continue
        summary = _summarise(entry)
        if summary is not None:
            summaries[name] = summary
    return summaries


def _summarise(entry):
    """One line for one entry, or None if it cannot be read."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return str(entry)
    if not isinstance(entry, dict):
        return None
    interval = entry.get("interval") or entry.get("next")
    last_run = entry.get("last_run") or entry.get("last")
    parts = [str(interval)] if isinstance(interval, (str, int, float)) else []
    if isinstance(last_run, (str, int, float)):
        parts.append(f"last {last_run}")
    return ", ".join(parts) if parts else None
