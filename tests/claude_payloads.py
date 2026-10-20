"""The `claude -p --output-format json` result objects the stand-in `claude`
scripts print, in one place, so the tests' payloads are shaped by the CLI's
contract and not by what the parser happens to read.

**Provenance.** The shape is from the Claude Code CLI docs, not captured
from a run: https://code.claude.com/docs/en/headless ("Output formats",
the json result object), the page src/curamoir/fix_step.py already cites for
`total_cost_usd`. The repository holds no copy of that page and no recorded
output, so the field set below (type, subtype, is_error, duration_ms,
duration_api_ms, num_turns, result, session_id, total_cost_usd, usage) is the
documented one, and the values are invented. A run that hits `--max-turns`
ends with `subtype: "error_max_turns"`, `is_error: true`, and no `result`.
No test here calls a model.
"""

#: An example `usage` object, in the Messages API's token-count fields.
USAGE = {"input_tokens": 1200, "output_tokens": 340,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

_COMMON = {"type": "result", "duration_ms": 2310, "duration_api_ms": 2050,
           "session_id": "00000000-0000-4000-8000-000000000000"}


def success(result, cost=0.02, usage=None, num_turns=3):
    """A run that finished: `subtype: "success"`, `is_error: false`."""
    return {**_COMMON, "subtype": "success", "is_error": False, "num_turns": num_turns,
            "result": result, "total_cost_usd": cost,
            "usage": dict(USAGE if usage is None else usage)}


def is_error(result, cost=0.02):
    """A run the CLI reports as an error although it exited 0 and gave a
    `result` (its text is the error)."""
    return {**success(result, cost), "is_error": True}


def error_max_turns(cost=0.02, num_turns=30):
    """A run stopped at `--max-turns`: no `result` at all."""
    return {**_COMMON, "subtype": "error_max_turns", "is_error": True,
            "num_turns": num_turns, "total_cost_usd": cost, "usage": dict(USAGE)}
