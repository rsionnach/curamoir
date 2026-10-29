"""E1-R5: the `curamoir.yaml` schema, its defaults, and load-time validation.

Every error names the offending key by its full dotted path. That is the
requirement, and it is the whole value: a config error a returning developer
cannot locate costs more than the feature the key controls. All problems are
reported together, because fixing config one error per run is how people give up
on a tool.

Defaults are §6.3's block as amended by the five decisions recorded in the bead
graph:

- DEC-1  `models.cheap`/`deep` are `claude-sonnet-5`/`claude-opus-5`, plus an
         editable price table and a batch toggle (batch runs at 50%).
- DEC-2  `modules.strategy` defaults to `auto`, not `top-level-dirs`, which
         collapses a src-layout repo to a single module named `src`.
- DEC-3  rule matchers are `ast-grep` (primary) or `regex` (escape hatch).
- DEC-4  `filing.github` defaults to OFF — mirroring is opt-in.
- DEC-5  `sweep.tier1_scan` defaults to ON (E3-R1's "MAY additionally scan",
         cm-fkd.2): a starter set's principles have no rules, so without the
         tier-1 scan the promotion ladder (E4-R1) has no intake. No scan runs,
         and nothing is spent, when no principle is eligible.

Model ids are NOT validated against a list. The spec says "verify current model
ids", and a hardcoded whitelist would reject every model released after this
file was written. The key is validated; the value is the user's business.

PyYAML is needed only by `load_config_text`, and is imported there rather than
at module scope: a plugin is an unpacked directory with no install step, so
third-party imports cannot be assumed present. Everything else here is stdlib.

The runner kind (E14.1, cm-2qp.1; docs/direction-platform-first.md §3) is
at the end: `resolve_runner` reads CURAMOIR_RUNNER and the hosted markers
from an environment its caller hands in. It is run configuration that is
not curamoir.yaml's (the section's comment says why).
"""

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from curamoir.redaction import redact

NUMBER = "number"
#: A whole number of 0 or more. Every key that COUNTS something takes this
#: rather than NUMBER, because a count indexes and slices: `max_open_prs: -1`
#: made the PR cap refuse every job (`len(ours) < -1`), and `max_per_run: 2.5`
#: is a slice bound. The finiteness rule generalised by TYPE and the
#: non-negativity rule by name suffix, which left thirteen of these open; this
#: is the vocabulary that was missing. The money keys stay NUMBER, because they
#: are legitimately fractional.
COUNT = "whole number of 0 or more"
STRING = "string"
#: A non-empty string with no whitespace at either end. WHY for
#: guardrails.branch_prefix: as a plain STRING, `""` would pass, and then
#: the open-PR cap would count every open PR in the repository as
#: cúramóir's while the sweep's idempotence decision
#: (idempotence.sweep_branch_prefix) would refuse it; and `" curamoir/ "`
#: would match no branch git allows, so the cap and the decision would see
#: none of cúramóir's own PRs. idempotence.sweep_branch_prefix applies the
#: same rule.
NON_EMPTY_TRIMMED_STRING = "non-empty string with no leading or trailing whitespace"
BOOLEAN = "boolean"
STRING_LIST = "list of strings"
MAPPING = "mapping"


class WholeRange:
    """A whole number from `low` to `high`, both included: a COUNT whose
    zero or whose size is a mistake. WHY for sweep.test_timeout_seconds: 0
    timed out every test run, which the gate then reported as a red base,
    and a huge one overflowed the wait (OverflowError) instead of waiting."""

    def __init__(self, low, high):
        self.low, self.high = low, high

    def __repr__(self):
        return f"whole number from {self.low} to {self.high}"


#: A test run's timeout: at least a second, at most a day.
TIMEOUT_SECONDS = WholeRange(1, 86400)

#: The promotion ladder's window and threshold (E4-R1). WHY not COUNT: a
#: threshold of 0 makes every principle a candidate whether or not it was
#: ever fixed, and a window of 0 days counts nothing, so promotion silently
#: never happens. The upper bounds only refuse a typo's size (ten years; a
#: thousand fixes).
PROMOTION_WINDOW_DAYS = WholeRange(1, 3650)
PROMOTION_FIX_THRESHOLD = WholeRange(1, 1000)

DEFAULTS = {
    "version": 1,
    "modules": {
        # DEC-2: `auto` detects the importable package root, handling src/ layout,
        # and treats its immediate subpackages as modules.
        "strategy": "auto",
        "include": [],
        # DEC-2 item 3. `tests/` is excluded from GRADING, not from the sweep.
        "exclude": ["build/", "dist/", "*.egg-info/", "docs/", "tests/"],
    },
    "models": {
        "cheap": "claude-sonnet-5",
        "deep": "claude-opus-5",
    },
    "budget": {
        "monthly_ceiling_usd": 15,
        "per_run_ceiling_usd": 2,
        "on_ceiling": "pause",
        # Scheduled jobs are non-latency-sensitive by definition, and the Batch
        # API runs at 50%. Largest cost lever available, at no quality cost.
        "batch": True,
        # VERIFY PRICES before trusting a cost figure: published prices change,
        # and a stale table silently misreports spend. USD per million tokens,
        # verified 2026-10-05.
        "prices": {
            "claude-opus-5": {"input": 5.00, "output": 25.00},
            "claude-sonnet-5": {"input": 2.00, "output": 10.00},
            "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
        },
    },
    "cadence": {
        "sweep": "weekly",
        "review": "monthly",
        "brief": "on_return",
        "smoke": "weekly",
    },
    "review": {
        "lenses": ["correctness", "edge_cases"],
        "excellence_min_grade": "B",
        "modules_per_run": 2,
        "random_extra": 1,
        "backoff": {"clean_rounds_to_double": 2, "max_interval": "quarterly"},
    },
    "sweep": {
        "max_open_prs": 3,
        "max_files_per_pr": 20,
        # E3-R4 (green_gate). Empty = detect from the base commit; nothing
        # detectable means no PR, because "no test command" is not green.
        "test_command": "",
        "test_timeout_seconds": 600,
        # Every run counts, the full set and the base included. 12 isolates a
        # single offender among max_files_per_pr (20) fixes (green_gate); it
        # needs about 2 + 2*ceil(log2(max_files_per_pr)).
        "max_test_runs": 12,
        # cm-p2f (os_sandbox): on macOS the test run gets no network at all,
        # loopback included, unless this is true (a suite that starts its own
        # local server). Linux's run always has a private loopback that
        # reaches nothing of the host's.
        "test_loopback": False,
        # DEC-5 (E3-R1b, curamoir.tier1): scan for violations of principles
        # that have no rule yet, on models.cheap, once per base commit.
        "tier1_scan": True,
        # cm-t3a: globs (as rule globs, `**/` for any directories) the sweep
        # never scans, so the tier-1 scan never reads, reports or sends the
        # fix step to them. Lint rules scope themselves with their own
        # `exclude:`.
        "exclude": [],
    },
    "promotion": {"fix_threshold": 3, "window_days": 90},
    "filing": {
        "beads": "auto",
        # DEC-4: opt-in. Valuable on popular repos where outside contributors
        # want visibility; a second copy for an audience of one otherwise.
        "github": False,
        "labels": ["curamoir", "curamoir:review"],
        "max_per_run": 15,
    },
    "guardrails": {
        "default_branch_writes": "never",
        "branch_prefix": "curamoir/",
        "command_denylist": "default",
        "use_dcg_if_present": True,
    },
    "handoff": {
        # E8-R3: the SessionStart injection budget. Characters are what Claude
        # Code actually limits (10,000), and ~4 chars/token is the usual
        # approximation, so this is bounded by both.
        "session_start_token_budget": 1500,
        "keep_history": 10,
        "brief_threshold_days": 5,
    },
    # E2-R4: `curamoir-lint` exits non-zero on a violation at or above
    # `fail_on`. `error` by default, so `warning` and `info` are reported
    # without failing a build nobody asked to be strict.
    "rules": {"matcher": "ast-grep", "fail_on": "error"},
    "smoke": {
        "command": "bash scripts/quickstart.sh",
        "container": "python:3.12-slim",
    },
}

# Types and allowed values, keyed the same way as DEFAULTS. A key absent here is
# unknown and rejected by name.
SCHEMA = {
    "version": COUNT,
    "modules": {
        "strategy": ("auto", "top-level-dirs", "explicit"),
        "include": STRING_LIST,
        "exclude": STRING_LIST,
    },
    "models": {"cheap": STRING, "deep": STRING},
    "budget": {
        "monthly_ceiling_usd": NUMBER,
        "per_run_ceiling_usd": NUMBER,
        "on_ceiling": ("pause", "warn"),
        "batch": BOOLEAN,
        "prices": MAPPING,
    },
    "cadence": {
        "sweep": STRING, "review": STRING,
        # A fixed vocabulary, like modules.strategy and budget.on_ceiling: a
        # typo here silently disabled the documented `weekly` mode while doctor
        # went on reporting "config: valid".
        "brief": ("on_return", "weekly"),
        "smoke": STRING,
    },
    "review": {
        "lenses": STRING_LIST,
        "excellence_min_grade": ("A", "B", "C", "D", "F"),
        "modules_per_run": COUNT,
        "random_extra": COUNT,
        "backoff": {"clean_rounds_to_double": COUNT, "max_interval": STRING},
    },
    "sweep": {
        "max_open_prs": COUNT, "max_files_per_pr": COUNT,
        "test_command": STRING, "test_timeout_seconds": TIMEOUT_SECONDS, "max_test_runs": COUNT,
        "test_loopback": BOOLEAN, "tier1_scan": BOOLEAN, "exclude": STRING_LIST,
    },
    "promotion": {"fix_threshold": PROMOTION_FIX_THRESHOLD,
                  "window_days": PROMOTION_WINDOW_DAYS},
    "filing": {
        "beads": ("auto", "on", "off"),
        "github": BOOLEAN,
        "labels": STRING_LIST,
        "max_per_run": COUNT,
    },
    "guardrails": {
        # Only `never` exists. §6.3 documents only `never`, and
        # DefaultBranchGuard unconditionally refuses — admitting an `allow` the
        # code does not implement would be a promise the schema cannot keep.
        "default_branch_writes": ("never",),
        "branch_prefix": NON_EMPTY_TRIMMED_STRING,
        "command_denylist": STRING,
        "use_dcg_if_present": BOOLEAN,
    },
    "handoff": {
        "session_start_token_budget": COUNT,
        "keep_history": COUNT,
        "brief_threshold_days": COUNT,
    },
    # `fail_on` is closed like every other vocabulary here: `fail_on: eror`
    # read as "never fail" would loosen a gate without anybody deciding to.
    "rules": {"matcher": ("ast-grep", "regex"), "fail_on": ("info", "warning", "error")},
    "smoke": {"command": STRING, "container": STRING},
}


#: Characters that would break one message into several lines, or redraw the
#: terminal: C0 and C1 controls, DEL, and the Unicode line and paragraph
#: separators.
CONTROL_CHARACTERS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def printable(text):
    """`text` as it can be written in UTF-8. A filename that is not UTF-8
    reaches Python on Linux with surrogate escapes, which no encoder will
    write; shown instead as the bytes it holds (`bad\\xff.py`), as `ls -b`
    would."""
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        # A surrogate that is not an escaped byte: not from the filesystem.
        return text.encode("utf-8", "backslashreplace").decode("utf-8")
    return raw.decode("utf-8", "backslashreplace")


def one_line(text):
    """`text`, printable, with every control character escaped (`\\n`,
    `\\x1b`), so it is one line that cannot redraw a terminal.

    Here, in the module every reader of repository text already imports
    (rules and lint import this one, never the reverse), so a key, a value or
    a filename from the repo is escaped by the error that echoes it. Escaping
    is one-way: a name holding a backslash and an `n` prints exactly like one
    holding a newline. Idempotent, since its output has no control characters.
    """
    return CONTROL_CHARACTERS.sub(_escape_control, printable(str(text)))


def _escape_control(found):
    character = found.group(0)
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(character)
    if named:
        return named
    code = ord(character)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


class ConfigError(Exception):
    """Configuration that cannot be loaded, with every problem named.

    Each problem is made one line here, not where it is written: problems
    echo keys and values from the file, and lint, doctor and status print
    them, so a key holding a newline or an escape sequence would otherwise
    forge a line of their output. Done once here so a new message cannot
    forget it."""

    def __init__(self, problems):
        self.problems = [one_line(problem) for problem in problems]
        super().__init__(
            "invalid curamoir.yaml:\n"
            + "\n".join(f"  - {problem}" for problem in self.problems)
        )


def load_config(raw):
    """Validate `raw` against the schema and merge it over the defaults."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError([f"top level must be a {MAPPING}, got {_type_name(raw)}"])

    problems = []
    _validate(raw, SCHEMA, prefix="", problems=problems)
    if problems:
        raise ConfigError(problems)
    return _merge(deepcopy(DEFAULTS), raw)


def load_config_text(text):
    """Parse YAML then validate. With `safe_load_unique`, the only code here
    that needs PyYAML."""
    try:
        raw = safe_load_unique(text)
    except ImportError as missing:  # pragma: no cover - environment-dependent
        raise ConfigError([
            "PyYAML is required to read curamoir.yaml but is not importable "
            f"({missing}). Install it with `pip install pyyaml`, or run "
            "`/curamoir:doctor`, which reports it as a missing prerequisite."
        ]) from None
    except RefusedYAMLError as refused:
        raise ConfigError([str(refused)]) from None
    return load_config(raw or {})


class RefusedYAMLError(ValueError):
    """Valid YAML that curamoir.yaml and rule files do not accept, because it
    would let a value be set somewhere a reader of the key does not see it.
    The message is one line (see `one_line`)."""

    def __init__(self, message):
        super().__init__(one_line(message))


class DuplicateKeyError(RefusedYAMLError):
    """A YAML mapping names the same key twice."""


def safe_load_unique(text):
    """`yaml.safe_load`, refusing what would set a value out of a reader's
    sight (RefusedYAMLError). Shared by config and rule files. PyYAML must be
    importable; the caller reports it missing in its own terms.

    - A key repeated in one mapping (DuplicateKeyError). YAML keeps the last,
      which for `rules.fail_on` or a rule's `severity` is a setting changed
      with both lines in plain view.
    - Merge keys (`<<`), anchors (`&`) and aliases (`*`), anywhere. A merge
      sets keys of a mapping from elsewhere, and two of them in one mapping
      passed the duplicate check; an alias sets a value where it is not
      written; an anchor exists only to be aliased. Nothing in files this
      small needs them, and refusing all three is a rule that is simple to
      state and to check.
    """
    import yaml

    return yaml.load(text, Loader=_unique_key_loader())


_UNIQUE_KEY_LOADER = []


def _unique_key_loader():
    """The SafeLoader subclass, built on first use so this module still
    imports without PyYAML (see the module docstring)."""
    if _UNIQUE_KEY_LOADER:
        return _UNIQUE_KEY_LOADER[0]
    import yaml

    class UniqueKeyLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            # Checked on the event stream, before PyYAML resolves the alias,
            # so the line given is the one the author wrote it on.
            event = self.peek_event()
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise RefusedYAMLError(
                    f"line {event.start_mark.line + 1}: YAML anchors and aliases "
                    "(`&name`, `*name`) are not accepted here: an alias sets a "
                    "value away from the key it applies to. Write the value out."
                )
            return super().compose_node(parent, index)

    def construct_mapping(loader, node, deep=False):
        seen = {}
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise RefusedYAMLError(
                    f"line {key_node.start_mark.line + 1}: YAML merge keys (`<<`) "
                    "are not accepted here: a merge sets keys away from where a "
                    "reader of the mapping looks for them. Write the keys out."
                )
            key = loader.construct_object(key_node, deep=True)
            line = key_node.start_mark.line + 1
            try:
                first = seen.get(key)
                seen.setdefault(key, line)
            except TypeError:
                continue  # unhashable; SafeLoader's own error is the better one
            if first is not None:
                raise DuplicateKeyError(
                    f"{key}: duplicate key, on lines {first} and {line}; YAML "
                    "would silently keep only the last, so say which you mean once"
                )
        return loader.construct_mapping(node, deep=deep)

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    _UNIQUE_KEY_LOADER.append(UniqueKeyLoader)
    return UniqueKeyLoader


def _validate(value, schema, prefix, problems):
    # Keys and values are echoed raw here; ConfigError makes each problem one
    # line, so a hostile key cannot forge one.
    for key, given in value.items():
        path = f"{prefix}{key}"
        if key not in schema:
            problems.append(
                f"{path}: unknown key. Allowed here: "
                + ", ".join(sorted(schema))
            )
            continue
        expected = schema[key]

        if isinstance(expected, dict):
            if not isinstance(given, dict):
                problems.append(f"{path}: expected a {MAPPING}, got {_type_name(given)}")
            else:
                _validate(given, expected, prefix=f"{path}.", problems=problems)
        elif isinstance(expected, tuple):
            if given not in expected:
                problems.append(
                    f"{path}: {given!r} is not allowed. Allowed values: "
                    + ", ".join(repr(option) for option in expected)
                )
        else:
            _check_scalar(path, given, expected, problems)


def _check_scalar(path, given, expected, problems):
    if isinstance(expected, WholeRange):
        if isinstance(given, bool) or not isinstance(given, int):
            problems.append(f"{path}: expected a {expected!r}, got {_type_name(given)}")
        elif not expected.low <= given <= expected.high:
            problems.append(f"{path}: must be a {expected!r} (got {given})")
    elif expected is COUNT:
        # A count indexes and slices, so a float is not merely untidy: it
        # reached `files[: len(files) - keep]` as a TypeError that the hooks
        # swallow, so handoffs stopped silently. A negative one pruned the
        # entire archive, including the entry just written.
        if isinstance(given, bool) or not isinstance(given, int):
            problems.append(
                f"{path}: expected a {COUNT}, got {_type_name(given)}"
            )
        elif given < 0:
            problems.append(f"{path}: must not be negative (got {given})")
    elif expected is NUMBER:
        # bool is an int in Python; a boolean where a number belongs is a mistake.
        if isinstance(given, bool) or not isinstance(given, (int, float)):
            problems.append(f"{path}: expected a {NUMBER}, got {_type_name(given)}")
        elif not math.isfinite(given):
            # YAML parses .inf, .nan and 1e400 to exactly these, and nan
            # compares false against everything: a nan ceiling turns
            # `on_ceiling: pause` into `on_ceiling: ignore` with no error.
            # Checked for every number in the schema, not just prices, because
            # finiteness is a property of the type rather than of one key.
            problems.append(f"{path}: must be a finite {NUMBER} (got {given})")
        elif given < 0 and path.endswith("_usd"):
            # A negative ceiling refuses every job, which errs safe but reads as
            # a broken install; say which key is wrong instead.
            problems.append(
                f"{path}: must not be negative (got {given})"
            )
    elif expected is STRING:
        if not isinstance(given, str):
            problems.append(f"{path}: expected a {STRING}, got {_type_name(given)}")
    elif expected is NON_EMPTY_TRIMMED_STRING:
        if not isinstance(given, str):
            problems.append(f"{path}: expected a {NON_EMPTY_TRIMMED_STRING}, got {_type_name(given)}")
        elif not given or given != given.strip():
            problems.append(f"{path}: must be a {NON_EMPTY_TRIMMED_STRING} (got {given!r})")
    elif expected is BOOLEAN:
        if not isinstance(given, bool):
            problems.append(f"{path}: expected a {BOOLEAN}, got {_type_name(given)}")
    elif expected is STRING_LIST:
        if not isinstance(given, list) or any(not isinstance(i, str) for i in given):
            problems.append(f"{path}: expected a {STRING_LIST}, got {_type_name(given)}")
        elif path == "sweep.exclude":
            _check_globs(path, given, problems)
    elif expected is MAPPING:
        if not isinstance(given, dict):
            problems.append(f"{path}: expected a {MAPPING}, got {_type_name(given)}")
        elif path.endswith("prices"):
            _check_price_table(path, given, problems)


def _check_globs(path, globs, problems):
    """Each glob compiles as a rule glob (rules.glob_regex), so a typo such
    as `[z-a]` is named here, not raised from inside a sweep."""
    import re

    from curamoir.rules import glob_regex

    for glob in globs:
        try:
            glob_regex(glob)
        except re.error as error:
            problems.append(f"{path}: {glob!r} is not a glob that compiles ({error})")


def _check_price_table(path, table, problems):
    """Each price entry needs numeric `input` and `output`, and may give
    `cache_write`, `cache_write_1h` and `cache_read`.

    Validated here rather than discovered at read time: the table is documented
    as editable, so a typo is an ordinary user action, and the error belongs with
    the key the user mistyped rather than deep inside the budget guard.
    """
    for model, rate in table.items():
        where = f"{path}.{model}"
        if not isinstance(rate, dict):
            problems.append(f"{where}: expected a {MAPPING} with numeric 'input' and 'output'")
            continue
        # `cache_write` and `cache_read` are optional (budget.PriceTable
        # defaults them from `input`, cm-2ui), but one that is given must be
        # a price like the other two.
        for side in ("input", "output", "cache_write", "cache_write_1h", "cache_read"):
            if side.startswith("cache_") and side not in rate:
                continue
            value = rate.get(side)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(
                    f"{where}.{side}: expected a {NUMBER} (USD per million tokens), "
                    f"got {_type_name(value)}"
                )
            elif not math.isfinite(value):
                # YAML parses .inf, .nan and 1e400 to exactly these. They passed
                # validation, then made every job refuse with "would reach $inf";
                # and nan compares false against everything, silently inverting
                # the ceiling comparison.
                problems.append(
                    f"{where}.{side}: must be a finite {NUMBER} (got {value})"
                )
            elif value < 0:
                # A mistyped -5 would make a run look like it refunded money,
                # pulling month-to-date below the real total — the same
                # undercount the ceiling exists to prevent, and silent because
                # nothing raises.
                problems.append(
                    f"{where}.{side}: must not be negative (got {value}); a negative "
                    "price understates spend and would silently raise the ceiling"
                )


def _merge(base, override):
    """Deep-merge `override` into `base`, so a user price does not drop the rest."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


def _type_name(value):
    return {
        bool: BOOLEAN, int: NUMBER, float: NUMBER, str: STRING,
        list: "list", dict: MAPPING, type(None): "null",
    }.get(type(value), type(value).__name__)


# The runner kind (E14.1, cm-2qp.1).
#
# docs/direction-platform-first.md §3 and §5 R2. Not curamoir.yaml: the kind
# is a property of the machine a run is on, so it is the scheduler's to
# declare (the workflow template and the Routine prompt set it), never the
# repository's, whose committed config would make every clone of it "hosted".
# Here because the modules that will branch on it (green_gate, E14.2;
# fix_step, E14.3) already import this one, directly or through
# repo_config, and it needs only the standard library and
# curamoir.redaction (itself stdlib-only).
#
# Resolved ONCE per run, by job_guard.run_guarded, from the environment its
# caller passes (runner.run_job's `environ`, os.environ by default), and
# handed down explicitly: the body reads it as `run.runner` (JobRun), and
# nothing below the entry point reads these variables itself. So a test
# hands in a dict, and a run's kind cannot change half way through.

#: The variable a scheduler declares the runner kind in.
RUNNER_VARIABLE = "CURAMOIR_RUNNER"

#: The hosted markers: variables a hosted runner sets itself, each to
#: exactly "true". CLAUDE_CODE_REMOTE is set in every Claude Code cloud
#: session, which is where a Routine runs
#: (https://code.claude.com/docs/en/env-vars), and is enough on its own.
#: GITHUB_ACTIONS is set on every GitHub Actions runner, and is enough only
#: with RUNNER_ENVIRONMENT_VARIABLE (below). A marker is a check on the
#: declaration, never a substitute for it: a laptop that happens to export
#: one is still local unless it says `hosted`.
CLAUDE_MARKER = "CLAUDE_CODE_REMOTE"
GITHUB_MARKER = "GITHUB_ACTIONS"

#: GitHub's own name for where an Actions job runs: `github-hosted` or
#: `self-hosted` (RUNNER_ENVIRONMENT, GitHub's "Variables reference").
#: WHY it is required with GITHUB_ACTIONS: §3's hosted boundary is a
#: disposable VM, discarded after the run. GITHUB_ACTIONS=true is also set
#: on a self-hosted runner (someone's machine, kept between jobs) and under
#: nektos/act (a laptop), where nothing is discarded; those must run as
#: local, behind the OS sandbox. So the GitHub marker counts only on a
#: GitHub-hosted runner, and a hosted claim anywhere else is refused.
RUNNER_ENVIRONMENT_VARIABLE = "RUNNER_ENVIRONMENT"
GITHUB_HOSTED = "github-hosted"

#: The Claude Code cloud session's id, `cse_<token>`, from which the
#: transcript link is made (session_transcript).
SESSION_VARIABLE = "CLAUDE_CODE_REMOTE_SESSION_ID"

#: Where a cloud session's transcript is read: this, then `session_<token>`.
TRANSCRIPT_BASE = "https://claude.ai/code/"

#: The session id's prefix, and the one the link spells it with.
_SESSION_ID_PREFIX = "cse_"
_SESSION_LINK_PREFIX = "session_"

#: A session token: letters, digits, `_` and `-`, and no longer than any
#: session id is. WHY so narrow: the value is the environment's text and is
#: put into a URL and a PR body, so anything that could end the URL, start
#: Markdown or a new line (`/`, `?`, `#`, `]`, a space, a line break) makes
#: it not a token, and then there is no link at all.
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: A transcript link as session_transcript makes it, for a consumer that is
#: handed one (sweep_pr checks its argument against this).
TRANSCRIPT_URL_PATTERN = re.compile(
    re.escape(TRANSCRIPT_BASE + _SESSION_LINK_PREFIX) + _SESSION_TOKEN.pattern)

#: How the job line's `transcript` reads when a session id is set but gives
#: no link: this, then the reason in parentheses (never the value).
TRANSCRIPT_UNAVAILABLE = "unavailable"


class RunnerKind(str, Enum):
    """Where a run is, as its scheduler declared it (§3's two columns).

    LOCAL: cron, launchd, or by hand on a developer's machine; the OS
    sandbox is the process boundary, and every guard runs as at 3f50a3f.
    HOSTED: a disposable VM (GitHub Actions, a Claude Code Routine); the VM
    is the boundary. A str, as guards.Reason is, so its value is what the
    ledger writes."""

    LOCAL = "local"
    HOSTED = "hosted"

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class Runner:
    """A run's resolved runner kind: its `kind`, the link to its transcript
    when it is a cloud session with a well-formed id (session_transcript),
    else None, and why there is no link when a session id was set but gave
    none (`transcript_unavailable`). What job_guard.run_guarded resolves
    once and the body receives as `run.runner`."""

    kind: RunnerKind
    transcript_url: Optional[str] = None
    transcript_unavailable: Optional[str] = None

    @property
    def hosted(self):
        """True only for RunnerKind.HOSTED: what the steps that differ by
        runner kind test (`run.runner.hosted`), so anything that is not
        hosted, now or later, takes the local path."""
        return self.kind is RunnerKind.HOSTED

    def ledger_fields(self):
        """The job line's fields for this runner kind: `runner`, and the
        `transcript` field (transcript_fields)."""
        return {"runner": self.kind.value,
                **transcript_fields(self.transcript_url, self.transcript_unavailable)}


#: The local runner with no transcript: what a run with no declaration
#: resolves to, and what a JobRun made outside run_guarded gives its body.
LOCAL_RUNNER = Runner(RunnerKind.LOCAL)


class RunnerDeclarationError(ValueError):
    """CURAMOIR_RUNNER says something the run cannot honour: `hosted` with
    no hosted marker, or a value that names no runner kind. The run is
    refused (job_guard, reason `runner`), never downgraded to local: a
    workflow that believes it runs hosted and silently runs local guards
    is a misconfiguration nobody would see, and a laptop claiming hosted
    would skip the sandbox (E14.2) and get a shell (E14.3)."""


def resolve_runner(environ):
    """The Runner `environ` (a mapping, os.environ or a test's dict)
    declares, or RunnerDeclarationError (its message is the refusal's).

    - CURAMOIR_RUNNER unset or empty: LOCAL.
    - `local`: LOCAL.
    - `hosted`: HOSTED when CLAUDE_CODE_REMOTE is exactly `true`, or when
      GITHUB_ACTIONS is exactly `true` and RUNNER_ENVIRONMENT is exactly
      `github-hosted`; otherwise refused (a GitHub runner that is not
      GitHub-hosted with its own message).
    - Anything else (`Hosted`, `ci`, `github`, ` hosted`, a value that is
      not text): refused, naming the accepted values. Exact, as the markers
      are: a near miss is a declaration the scheduler did not quite make.
      The value itself is never echoed: the message lands in the committed
      ledger, and a secret pasted into the variable by mistake would land
      with it (a clipped one can slip past redaction's patterns).

    Never inferred: a marker with no declaration is LOCAL. The transcript
    (session_transcript) is attached whatever the kind."""
    declared = environ.get(RUNNER_VARIABLE)
    if declared is None:
        declared = ""
    link, unavailable = session_transcript(environ)
    if declared in ("", RunnerKind.LOCAL.value):
        return Runner(RunnerKind.LOCAL, link, unavailable)
    if declared == RunnerKind.HOSTED.value:
        if environ.get(CLAUDE_MARKER) == "true":
            return Runner(RunnerKind.HOSTED, link, unavailable)
        if environ.get(GITHUB_MARKER) == "true":
            if environ.get(RUNNER_ENVIRONMENT_VARIABLE) == GITHUB_HOSTED:
                return Runner(RunnerKind.HOSTED, link, unavailable)
            raise RunnerDeclarationError(
                f"{RUNNER_VARIABLE}=hosted was declared on a GitHub Actions runner, but "
                f"{RUNNER_ENVIRONMENT_VARIABLE} is not {GITHUB_HOSTED!r}, so this is a "
                "self-hosted runner (or a local emulation such as act), not a disposable "
                "VM; the job does not run. A self-hosted runner must run as local: unset "
                f"{RUNNER_VARIABLE} there, and the OS sandbox holds the run.")
        raise RunnerDeclarationError(
            f"{RUNNER_VARIABLE}=hosted was declared, but neither {CLAUDE_MARKER}=true "
            f"nor {GITHUB_MARKER}=true (on a {RUNNER_ENVIRONMENT_VARIABLE}={GITHUB_HOSTED} "
            "runner) is set, so this is not a hosted runner; the job does not run. Unset "
            f"{RUNNER_VARIABLE} to run locally, with the OS sandbox.")
    what = (f"a value of {len(declared)} characters" if isinstance(declared, str)
            else "a value that is not text")
    raise RunnerDeclarationError(
        f"{RUNNER_VARIABLE} is set to {what} that is neither "
        f"{RunnerKind.HOSTED.value!r} nor {RunnerKind.LOCAL.value!r} (the value is not "
        "shown: it is written to the ledger). Set it to exactly one of those, or unset it "
        "for local. The job does not run.")


def session_transcript(environ):
    """(link, None), (None, why there is no link), or (None, None) when no
    session id is set.

    CLAUDE_CODE_REMOTE_SESSION_ID is `cse_<token>`; the link is
    TRANSCRIPT_BASE + `session_<token>`. A value already spelled
    `session_<token>` is used as it is. Any other value gives no link
    (decided here, E14.1): an id with no known prefix may be some other
    kind of id, and a link that looks right but opens the wrong page is
    worse than none. So does a token that is not plain (_SESSION_TOKEN), or
    one redaction would change (a PR body redaction would change is not
    sent, sweep_pr._check_redacted, so the link would cost the PR). The
    reason never quotes the value: it is the environment's text."""
    value = environ.get(SESSION_VARIABLE)
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        return None, "the session id is not text"
    for prefix in (_SESSION_ID_PREFIX, _SESSION_LINK_PREFIX):
        if value.startswith(prefix):
            token = value[len(prefix):]
            break
    else:
        return None, (f"the session id starts with neither {_SESSION_ID_PREFIX!r} nor "
                      f"{_SESSION_LINK_PREFIX!r}")
    if not _SESSION_TOKEN.fullmatch(token):
        return None, ("the session id is not a plain token of 1 to 128 letters, digits, "
                      "'_' or '-'")
    url = f"{TRANSCRIPT_BASE}{_SESSION_LINK_PREFIX}{token}"
    if redact(url) != url:
        return None, "the session id looks like a secret to redaction"
    return url, None


def transcript_fields(link, unavailable):
    """The job line's `transcript` field: the link; or
    `unavailable (<why>)` when a session id was set but gave no link, so
    the line says so rather than dropping it silently; or nothing."""
    if link is not None:
        return {"transcript": link}
    if unavailable is not None:
        return {"transcript": f"{TRANSCRIPT_UNAVAILABLE} ({unavailable})"}
    return {}
