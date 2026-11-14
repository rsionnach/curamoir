### Correctness Issues
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
R5-FINDINGS: [{"lens": "correctness", "severity": "MINOR", "path": "src/tally/ledger.py", "start_line": 88, "end_line": 88, "claim": "load_entries returns the header row as an entry when the file has no data rows, so an empty ledger reports one entry dated 'date' instead of none.", "evidence": "scratch ledger with only the header line: tally list -> 1 entry {\"date\": \"date\", \"amount\": \"amount\"}; tally total -> ValueError: could not convert string to float: 'amount'", "fix": "Skip the first row whenever it matches the header, before the empty check, so a header-only file yields an empty list.", "verification_level": "reproduced"}, {"lens": "correctness", "severity": "MINOR", "path": "src/tally/ledger.py", "start_line": 141, "end_line": 152, "claim": "append_entry writes the journal line before it validates the amount, so a rejected entry still leaves a line in the journal after the command exits 1.", "evidence": "tally add 2026-01-05 abc -> exit 1 'amount must be a number'; journal.log then holds one line '2026-01-05 abc'; tally replay -> fails on that line", "fix": "Validate the amount before any write, or write the journal line and the ledger row in one step.", "verification_level": "reproduced"}, {"lens": "correctness", "severity": "MINOR", "path": "src/tally/categories.py", "start_line": 30, "end_line": 44, "claim": "normalise_category strips every digit from the name, not only a trailing count, so distinct categories collide ('rent 2025' and 'rent 2026' both become 'rent').", "evidence": "normalise_category('rent 2025') == normalise_category('rent 2026') == 'rent'; 'q1 travel' -> 'q travel'; 'travel (x3)' -> 'travel (x3)'", "fix": "Strip only a trailing count of the form (xN) and leave other digits in place.", "verification_level": "reproduced"}]
