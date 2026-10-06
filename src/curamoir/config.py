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

from copy import deepcopy

NUMBER = "number"
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
        # Summarisation only: handoff last-decision extraction, brief assembly.
        "summarise": "claude-haiku-4-5",
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
    "rules": {"matcher": "ast-grep"},
    "smoke": {
        "command": "bash scripts/quickstart.sh",
        "container": "python:3.12-slim",
    },
}

# Types and allowed values, keyed the same way as DEFAULTS. A key absent here is
# unknown and rejected by name.
SCHEMA = {
    "version": NUMBER,
    "modules": {
        "strategy": ("auto", "top-level-dirs", "explicit"),
        "include": STRING_LIST,
        "exclude": STRING_LIST,
    },
    "models": {"cheap": STRING, "deep": STRING, "summarise": STRING},
    "budget": {
        "monthly_ceiling_usd": NUMBER,
        "per_run_ceiling_usd": NUMBER,
        "on_ceiling": ("pause", "warn"),
        "batch": BOOLEAN,
        "prices": MAPPING,
    },
    "cadence": {
        "sweep": STRING, "review": STRING, "brief": STRING, "smoke": STRING,
    },
    "review": {
        "lenses": STRING_LIST,
        "excellence_min_grade": ("A", "B", "C", "D", "F"),
        "modules_per_run": NUMBER,
        "random_extra": NUMBER,
        "backoff": {"clean_rounds_to_double": NUMBER, "max_interval": STRING},
    },
    "sweep": {"max_open_prs": NUMBER, "max_files_per_pr": NUMBER},
    "promotion": {"fix_threshold": NUMBER, "window_days": NUMBER},
    "filing": {
        "beads": ("auto", "on", "off"),
        "github": BOOLEAN,
        "labels": STRING_LIST,
        "max_per_run": NUMBER,
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
        "session_start_token_budget": NUMBER,
        "keep_history": NUMBER,
        "brief_threshold_days": NUMBER,
    },
    "rules": {"matcher": ("ast-grep", "regex")},
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
    """Parse YAML then validate. The only function that needs PyYAML."""
    try:
        import yaml
    except ImportError as missing:  # pragma: no cover - environment-dependent
        raise ConfigError([
            "PyYAML is required to read curamoir.yaml but is not importable "
            f"({missing}). Install it with `pip install pyyaml`, or run "
            "`/curamoir:doctor`, which reports it as a missing prerequisite."
        ]) from None
    return load_config(yaml.safe_load(text) or {})


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
    if expected is NUMBER:
        # bool is an int in Python; a boolean where a number belongs is a mistake.
        if isinstance(given, bool) or not isinstance(given, (int, float)):
            problems.append(f"{path}: expected a {NUMBER}, got {_type_name(given)}")
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
