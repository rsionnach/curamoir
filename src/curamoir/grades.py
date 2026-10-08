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
        if isinstance(name, str) and isinstance(entry, dict)
    }
