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

Model ids are NOT validated against a list. The spec says "verify current model
ids", and a hardcoded whitelist would reject every model released after this
file was written. The key is validated; the value is the user's business.

PyYAML is needed only by `load_config_text`, and is imported there rather than
at module scope: a plugin is an unpacked directory with no install step, so
third-party imports cannot be assumed present. Everything else here is stdlib.
"""

import math
from copy import deepcopy

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
BOOLEAN = "boolean"
STRING_LIST = "list of strings"
MAPPING = "mapping"

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
    "sweep": {"max_open_prs": 3, "max_files_per_pr": 20},
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
    "sweep": {"max_open_prs": COUNT, "max_files_per_pr": COUNT},
    "promotion": {"fix_threshold": COUNT, "window_days": COUNT},
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
        "branch_prefix": STRING,
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


class ConfigError(Exception):
    """Configuration that cannot be loaded, with every problem named."""

    def __init__(self, problems):
        self.problems = list(problems)
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
    except DuplicateKeyError as duplicate:
        raise ConfigError([str(duplicate)]) from None
    return load_config(raw or {})


class DuplicateKeyError(ValueError):
    """A YAML mapping names the same key twice."""


def safe_load_unique(text):
    """`yaml.safe_load`, except that a key repeated in one mapping raises
    DuplicateKeyError rather than silently keeping the last value — which for
    `rules.fail_on` or a rule's `severity` is a setting changed with both
    lines in plain view. Shared by config and rule files. PyYAML must be
    importable; the caller reports it missing in its own terms.

    A key repeated after a `<<` merge is not a duplicate: overriding a merged
    value is what the merge key is for.
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
        pass

    def construct_mapping(loader, node, deep=False):
        seen = {}
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
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
    if expected is COUNT:
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
    elif expected is BOOLEAN:
        if not isinstance(given, bool):
            problems.append(f"{path}: expected a {BOOLEAN}, got {_type_name(given)}")
    elif expected is STRING_LIST:
        if not isinstance(given, list) or any(not isinstance(i, str) for i in given):
            problems.append(f"{path}: expected a {STRING_LIST}, got {_type_name(given)}")
    elif expected is MAPPING:
        if not isinstance(given, dict):
            problems.append(f"{path}: expected a {MAPPING}, got {_type_name(given)}")
        elif path.endswith("prices"):
            _check_price_table(path, given, problems)


def _check_price_table(path, table, problems):
    """Each price entry needs numeric `input` and `output`.

    Validated here rather than discovered at read time: the table is documented
    as editable, so a typo is an ordinary user action, and the error belongs with
    the key the user mistyped rather than deep inside the budget guard.
    """
    for model, rate in table.items():
        where = f"{path}.{model}"
        if not isinstance(rate, dict):
            problems.append(f"{where}: expected a {MAPPING} with numeric 'input' and 'output'")
            continue
        for side in ("input", "output"):
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
