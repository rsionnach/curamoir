"""Reading a grades file that may not have been written by us.

Epic 7 owns grading and has not landed, so every `grades.json` in existence
today is hand-written — and `/curamoir:status`'s contract is that a section
whose producing epic has not landed reports ABSENCE rather than erroring.

Three call sites used to index straight into `grades["modules"].items()` and
`entry.get("grade")`, so a plausible hand-written shape crashed both
`/curamoir:status` and `/curamoir:brief`. One reader now answers the shape
question for all of them, which is also the seam Epic 7 will replace.
"""


def graded_modules(grades):
    """`{module: entry}` for every well-formed entry, else an empty mapping.

    Lenient by design: a grades file the current schema does not recognise is
    the same situation as no grades file, and both must render as absence.
    """
    if not isinstance(grades, dict):
        return {}
    modules = grades.get("modules")
    if not isinstance(modules, dict):
        return {}
    return {
        name: entry for name, entry in modules.items()
        if isinstance(name, str) and isinstance(entry, dict) and _scalar_grade(entry)
    }


def _scalar_grade(entry):
    """Whether `entry`'s grade is something a ranking can look up.

    `_lowest_graded` does `ranking.get(grade, -1)`, and `dict.get` RAISES on an
    unhashable key — so `{"grade": ["B"]}` crashed the brief while status, which
    only formats, survived. A grade that is not a string is not a grade.
    """
    grade = entry.get("grade")
    return grade is None or isinstance(grade, str)
