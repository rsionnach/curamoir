"""Write the synthetic reviewer reports for tests/fixtures/r5-reports/ (cm-rzs).

Each report keeps the structure and numbers of one real report from cm-70j's
R5 run (record count, severities, verification levels, the fix: null share,
closures, the regression flag, the prose layout), with an invented project
("tally", a small ledger CLI), invented paths, lines, claims and evidence.
A closure's `resolves` is computed with the parser's own fingerprint(), so the
verify reports close the open set of the report before them.

    python3 tests/fixtures/r5-reports/make_synthetic_reports.py [<out dir>]

The out dir defaults to this script's own directory; the suite writes to a
temporary one and compares, so the committed fixtures are always what this
script writes.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / ".claude/hooks/lib"))  # the repository root's
import r5_findings  # noqa: E402


def rec(lens, severity, path, start, end, claim, evidence, fix, level, **extra):
    r = {"lens": lens, "severity": severity, "path": path, "start_line": start,
         "end_line": end, "claim": claim, "evidence": evidence, "fix": fix,
         "verification_level": level}
    r.update(extra)
    return r


def closes(earlier, path, start, end, claim, evidence, level):
    """A closure of `earlier`, with fix: null, at the same severity."""
    return rec(earlier["lens"], earlier["severity"], path, start, end, claim, evidence, None,
               level, resolves=r5_findings.fingerprint(earlier["lens"], earlier["path"],
                                                        earlier["claim"]))


def write(out, name, prose, records):
    line = "R5-FINDINGS: " + json.dumps(records, ensure_ascii=False)
    (out / name).write_text(prose.rstrip("\n") + "\n" + line + "\n")


def main(out):
    """Write the six reports into the directory `out`."""
    # --- correctness 1: three reproduced minors, in the full template layout ----
    c1 = [
        rec("correctness", "MINOR", "src/tally/ledger.py", 88, 88,
            "load_entries returns the header row as an entry when the file has no data rows, so an empty ledger reports one entry dated 'date' instead of none.",
            "scratch ledger with only the header line: tally list -> 1 entry {\"date\": \"date\", \"amount\": \"amount\"}; tally total -> ValueError: could not convert string to float: 'amount'",
            "Skip the first row whenever it matches the header, before the empty check, so a header-only file yields an empty list.",
            "reproduced"),
        rec("correctness", "MINOR", "src/tally/ledger.py", 141, 152,
            "append_entry writes the journal line before it validates the amount, so a rejected entry still leaves a line in the journal after the command exits 1.",
            "tally add 2026-01-05 abc -> exit 1 'amount must be a number'; journal.log then holds one line '2026-01-05 abc'; tally replay -> fails on that line",
            "Validate the amount before any write, or write the journal line and the ledger row in one step.",
            "reproduced"),
        rec("correctness", "MINOR", "src/tally/categories.py", 30, 44,
            "normalise_category strips every digit from the name, not only a trailing count, so distinct categories collide ('rent 2025' and 'rent 2026' both become 'rent').",
            "normalise_category('rent 2025') == normalise_category('rent 2026') == 'rent'; 'q1 travel' -> 'q travel'; 'travel (x3)' -> 'travel (x3)'",
            "Strip only a trailing count of the form (xN) and leave other digits in place.",
            "reproduced"),
    ]
    write(out, "synthetic-correctness-1.md", """### Correctness Issues
#### Critical
None.
#### Important
None.
#### Minor
1. src/tally/ledger.py:88: load_entries returns the header row as an entry when the file has no data rows, so an empty ledger reports one entry dated 'date' (reproduced).
2. src/tally/ledger.py:141-152: append_entry writes the journal line before it validates the amount, so a rejected entry still leaves a journal line (reproduced).
3. src/tally/categories.py:30-44: normalise_category strips every digit, so 'rent 2025' and 'rent 2026' collide (reproduced).
### Correctness Assessment
**Verdict:** PASS WITH FIXES
""", c1)

    # --- correctness 4: one IMPORTANT regression, one MINOR, one closure of iteration 3
    c3_prior = rec("correctness", "MINOR", "src/tally/report.py", 60, 72,
                   "monthly_totals counts an entry dated on the last day of a month in the next month.",
                   "(iteration 3; not part of any fixture)", None, "reproduced")
    c4 = [
        rec("correctness", "IMPORTANT", "src/tally/report.py", 101, 104,
            "The fix for month boundaries moved the time-zone conversion after the grouping, so an entry recorded just after midnight UTC is now counted in the previous month.",
            "Scratch run: TZ=Pacific/Auckland tally add 2026-03-01T00:30 12.00 -> tally report --month 2026-02 lists it, --month 2026-03 does not. Before iteration 3's fix it was listed under 2026-03.",
            "Convert each timestamp to the ledger's zone before grouping, with a test for an entry just after midnight UTC.",
            "reproduced", regression=True),
        rec("correctness", "MINOR", "src/tally/report.py", 120, 131,
            "Two entries with the same id are both summed, in the same file and across imported files, because the totals never check ids for duplicates.",
            "Scratch run: two rows with id 7 and amount 5.00 -> tally total 10.00; tally import other.csv holding id 7 again -> 15.00; tally list shows three rows for id 7.",
            "Keep the first entry for each id and report the duplicates, counting each id once.",
            "reproduced"),
        closes(c3_prior, "src/tally/report.py", 60, 72,
               "Iteration 3's finding is closed: an entry is grouped by its own calendar date, so the last day of a month stays in that month.",
               "def month_of(entry): return entry.date.strftime('%Y-%m'); test_the_last_day_of_a_month_stays_in_it passes; 212 passed.",
               "reproduced"),
    ]
    write(out, "synthetic-correctness-4.md",
          "## Pass 1 (correctness), iteration 4: iteration 3's finding closed; one IMPORTANT regression (entries just after midnight UTC move to the previous month) and one MINOR (duplicate ids are summed twice).\n**Verdict:** PASS WITH FIXES\n",
          c4)

    # --- correctness 5: both open findings of iteration 4 closed -----------------
    c5 = [
        closes(c4[0], "src/tally/report.py", 101, 106,
               "Each timestamp is converted to the ledger's zone before grouping, so an entry just after midnight UTC is counted in the month it was recorded in.",
               "local = entry.at.astimezone(ledger.zone); key = local.strftime('%Y-%m'); scratch run: TZ=Pacific/Auckland entry at 2026-03-01T00:30 is listed under --month 2026-03 only",
               "reproduced"),
        closes(c4[1], "src/tally/report.py", 118, 140,
               "Duplicate ids are now counted once and reported, so a repeated id in one file or across imports no longer inflates the totals.",
               "seen = set(); if entry.id in seen: duplicates.append(entry.id); continue; scratch run: three rows with id 7 -> tally total 5.00 and 'duplicate id 7 ignored (2)'",
               "reproduced"),
    ]
    write(out, "synthetic-correctness-5.md",
          "## Pass 1 (correctness), iteration 5: no new findings; both prior open findings closed (reproduced).\n**Verdict:** PASS\n",
          c5)

    # --- clarity 1: seven cited minors ---------------------------------------
    k1 = [
        rec("clarity", "MINOR", "src/tally/cli.py", 212, 212,
            "The word 'pending' means both an entry not yet saved and an import not yet confirmed, in the same function.",
            "pending = []  # rows read but not saved ... if pending: print('pending import, run tally confirm')",
            "Use two names: unsaved_rows and import_awaiting_confirmation.", "cited"),
        rec("clarity", "MINOR", "src/tally/ledger.py", 160, 190,
            "The local names in summarise differ from the keys of the dict it returns, so a reader has to map total to sum and n to count by hand.",
            "total = ...; n = ...; return {'sum': total, 'count': n, 'mean': total / n}",
            "Name the locals after the keys they become.", "cited"),
        rec("clarity", "MINOR", "src/tally/cli.py", 305, 306,
            "The help text for --since does not say whether the date is inclusive.",
            "parser.add_argument('--since', help='only entries after this date')",
            "Say 'on or after' if it is inclusive, and pin it in a test.", "cited"),
        rec("clarity", "MINOR", "src/tally/ledger.py", 95, 101,
            "The comment on the fallback in load_entries describes the old CSV dialect, not the one the code now reads.",
            "# older files use semicolons; fall back to them\nreader = csv.reader(f, dialect='excel-tab')",
            "Rewrite the comment to name the tab dialect and why it is the fallback.", "cited"),
        rec("clarity", "MINOR", "docs/usage.md", 44, 45,
            "The usage guide says 'see the example below' but the example is above the sentence.",
            "docs/usage.md:44: 'See the example below for an import with categories.' The example is at lines 30-38.",
            "Say 'above', or move the example under the sentence.", "cited"),
        rec("clarity", "MINOR", "scripts/tally-backup.sh", 12, 13,
            "The error message on a failed copy does not say which file failed or that the backup was skipped.",
            "cp \"$src\" \"$dest\" || { echo 'copy failed' >&2; exit 1; }",
            "Name the source and destination, and say the backup was not written.", "cited"),
        rec("clarity", "MINOR", "tests/test_tally_ledger.py", 18, 21,
            "A test comment refers to a fixture that was renamed, and the module docstring still describes the old file layout.",
            "# uses the sample_rows fixture (now ledger_rows); docstring: 'Tests for ledger.csv in the home directory'",
            "Update the comment to ledger_rows and the docstring to the current layout.", "cited"),
    ]
    write(out, "synthetic-clarity-1.md",
          "## Pass 2 (clarity), iteration 1: no important issues; seven minors (pending's two meanings, locals vs returned keys, --since help, a stale fallback comment, 'below' in the usage guide, the backup error message, a stale test comment and docstring).\n**Verdict:** PASS WITH FIXES\n",
          k1)

    # --- clarity 2: all seven closed, fix: null, cited -------------------------
    k2_lines = [(212, 214), (160, 192), (305, 307), (95, 99), (44, 44), (10, 16), (1, 24)]
    k2_claims_and_evidence = [
        ("'pending' now has one meaning; the import case is import_awaiting_confirmation.",
         "unsaved_rows = []; ... if import_awaiting_confirmation: print('import awaiting confirmation, run tally confirm')"),
        ("summarise's locals are named after the keys it returns.",
         "sum_ = ...; count = ...; return {'sum': sum_, 'count': count, 'mean': sum_ / count}"),
        ("The --since help says the date is inclusive.",
         "parser.add_argument('--since', help='only entries on or after this date')"),
        ("The fallback comment now names the tab dialect and why it is tried second.",
         "# exports from the old spreadsheet are tab-separated; try that second"),
        ("The usage guide now says 'above'.",
         "docs/usage.md:44: 'See the example above for an import with categories.'"),
        ("The backup error names both files and says no backup was written.",
         "echo \"tally-backup: could not copy $src to $dest; no backup written\" >&2"),
        ("The test comment names ledger_rows and the docstring describes the current layout.",
         "# uses the ledger_rows fixture; docstring: 'Tests for the ledger in the data directory.'"),
    ]
    k2 = [closes(prior, prior["path"], s, e, claim, ev, "cited")
          for prior, (s, e), (claim, ev) in zip(k1, k2_lines, k2_claims_and_evidence, strict=True)]
    write(out, "synthetic-clarity-2.md",
          "## Pass 2 (clarity), iteration 2: all seven iteration-1 minors resolved; nothing new.\n**Verdict:** PASS\n",
          k2)

    # --- edge cases 1: one IMPORTANT, four reproduced minors, one walked --------
    e1 = [
        rec("edge_cases", "IMPORTANT", "src/tally/importer.py", 75, 87,
            "When two rows of an import share an id, the first one wins, so a later correction with a larger amount is silently dropped and the totals stay low.",
            "Scratch import with rows (id 3, 10.00) then (id 3, 100.00, note 'corrected') -> tally total 10.00; no warning printed.",
            "Keep the row marked as a correction, or refuse the import and name the duplicate id.", "reproduced"),
        rec("edge_cases", "MINOR", "src/tally/cli.py", 140, 152,
            "A very large import is passed to the sort helper as command-line arguments, which fails with 'Argument list too long' past a few hundred thousand rows.",
            "Scratch file of 400000 rows: tally import big.csv -> OSError: [Errno 7] Argument list too long: 'sort'",
            "Pass the rows on standard input instead of as arguments.", "reproduced"),
        rec("edge_cases", "MINOR", "src/tally/categories.py", 52, 54,
            "An empty category name is accepted and stored, so it shows as a blank line in reports.",
            "tally add 2026-01-05 4.00 --category '' -> exit 0; tally report shows a category row with no name",
            "Refuse an empty or all-space category name.", "reproduced"),
        rec("edge_cases", "MINOR", "src/tally/importer.py", 110, 122,
            "A file that is not valid UTF-8 exits 2, the code for a usage error, instead of 3, the code for a bad input file.",
            "printf 'date,amount\\n2026-01-05,\\xff\\n' > bad.csv; tally import bad.csv -> exit 2 'usage: tally import FILE'",
            "Catch the decode error and exit 3 with the file name.", "reproduced"),
        rec("edge_cases", "MINOR", "src/tally/cli.py", 260, 264,
            "The --rates file is read without checking its shape, so a list instead of an object fails later with a KeyError far from the cause.",
            "echo '[1, 2]' > rates.json; tally total --rates rates.json -> KeyError: 'EUR' in convert()",
            "Check that the rates file is an object of currency to number when it is read.", "reproduced"),
        rec("edge_cases", "MINOR", "src/tally/cli.py", 140, 152,
            "If the process is killed between writing the sorted temporary file and renaming it over the ledger, the temporary file is left behind and the next import reads it as an extra ledger.",
            "Walked: write_sorted() writes ledger.csv.tmp, then os.replace(); load_all() globs ledger*.csv, which matches ledger.csv.tmp.",
            "Name the temporary file so the glob cannot match it, and remove a stale one at start.", "walked"),
    ]
    write(out, "synthetic-edge-cases-1.md",
          "## Pass 3 (edge cases), iteration 1: one IMPORTANT (first-wins on a duplicate id drops a later correction) and five minors (argument length on large imports, an empty category, the wrong exit code for a non-UTF-8 file, an unchecked rates file, a stale temporary file after a kill).\n**Verdict:** PASS WITH FIXES\n",
          e1)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE)
