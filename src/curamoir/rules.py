"""E2-R3: rule files — the link between a principle and a line of code.

A rule lives in `.curamoir/rules/*.yaml` and says: this principle, this pattern,
this severity, this message. The link to the principle is the integrity
constraint the promotion ladder rests on — E4 promotes a principle when its
violations keep being fixed, and it can only count those if every rule names
exactly one principle that exists. A rule citing a dead id lints happily and
contributes its fixes to nothing, so the ladder silently stops working for that
principle and nothing reports why. That is why the id is resolved against
`principles.md` at load time rather than trusted.

Everything that can be wrong with a rule is checked when it is LOADED, not when
it runs. A sweep that dies on rule 7 of 11 has already spent the budget for six,
and the failure arrives without a file to open. So an uncompilable regex, an
unknown severity, an unknown message variable and a duplicate id are all load
errors naming the file and the field.

**Matchers** (DEC-3, 2026-10-05): `ast-grep` is primary and `regex` is a
declared escape hatch, not a parallel engine. Reach for regex only when no AST
pattern can express the thing — a file NAME like `foo_v2.py`, prose in a commit
message, a value in a config file — because an AST pattern is the one that
survives reformatting.

**Shape**, with only the required fields:

    id: no-bare-print
    principle: structured-logging
    severity: error
    matcher: regex
    pattern: '^\\s*print\\('
    message: "{match} writes to stdout with no level or logger name"

and the optional ones: `languages` or `files` to scope it, `exclude` to carve
out the places the principle itself exempts, `fix` for the remediation E2-R4
prints, and `target` to match a path rather than a line.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from curamoir.principles import PrincipleError, load_principles

#: E2-R3's required fields. `fix` is not among them, though E2-R4 prints it:
#: a rule for something with no mechanical remediation is still worth having,
#: and demanding a fix would get a vacuous one.
REQUIRED_FIELDS = ("id", "principle", "severity", "matcher", "pattern", "message")

#: DEC-3: ast-grep primary, regex as the declared escape hatch.
MATCHERS = ("ast-grep", "regex")

#: Ordered, least severe first, because E2-R4 exits non-zero at or above a
#: configured threshold and therefore has to compare them.
SEVERITIES = ("info", "warning", "error")

#: What a message template may reference. A closed set, because the template is
#: rendered once per violation during a sweep: an unknown name there is a
#: KeyError in the middle of a run that has already cost money.
MESSAGE_VARIABLES = ("path", "line", "col", "match", "rule", "principle")

#: Language to file patterns. The point of naming languages rather than globs is
#: E2-R6: the same format has to load for both bundled languages, and a rule
#: that said `**/*.py` would make that a claim rather than a demonstration.
LANGUAGE_PATTERNS = {
    "python": ("**/*.py", "**/*.pyi"),
    "typescript": ("**/*.ts", "**/*.tsx"),
    "javascript": ("**/*.js", "**/*.jsx", "**/*.mjs"),
    "go": ("**/*.go",),
    "rust": ("**/*.rs",),
    "yaml": ("**/*.yaml", "**/*.yml"),
    "markdown": ("**/*.md",),
    "toml": ("**/*.toml",),
}

#: What a rule matches against: a line of a file, or the file's path.
#: `no-file-variant-proliferation` matches a NAME, which is not a language.
TARGETS = ("line", "path")

#: Never searched, whatever a rule says. Extracted from the source lint
#: scripts' shared EXCLUDE_DIRS, which every one of them carried.
ALWAYS_EXCLUDED = (
    "venv", ".venv", "__pycache__", ".beads", ".curamoir", "node_modules",
    "dist", "build", ".git", ".tox", ".mypy_cache", ".pytest_cache",
    "site-packages",
)


class RuleError(Exception):
    """A rule file could not be read as a rule.

    Every message names the file, because the reader's next action is to open
    it, and the field, because that is the line to change.
    """


@dataclass(frozen=True)
class Rule:
    """One rule, validated."""

    id: str
    principle: str
    severity: str
    matcher: str
    pattern: str
    message: str
    fix: str = ""
    languages: tuple = ()
    files: tuple = ()
    exclude: tuple = ()
    target: str = "line"
    #: Where this came from, for error messages and for E2-R4's output.
    source: str = ""

    def file_patterns(self):
        """The globs E2-R4 should search for this rule.

        Explicit `files` wins over inferred languages, because a rule matching
        on a filename is not about a language at all. A rule that declares
        neither applies to every language we know how to name, which is the
        useful default for something like an orphaned TODO.
        """
        if self.files:
            return tuple(self.files)
        languages = self.languages or tuple(LANGUAGE_PATTERNS)
        patterns = []
        for language in languages:
            patterns.extend(LANGUAGE_PATTERNS[language])
        return tuple(patterns)

    def compiled(self):
        """The compiled regex, for a `regex` rule. Raises for an ast-grep one."""
        if self.matcher != "regex":
            raise RuleError(
                f"rule {self.id!r} uses the {self.matcher!r} matcher, so it has "
                "no regex to compile; run it through ast-grep instead"
            )
        return re.compile(self.pattern)

    def render(self, **values):
        """The message for one violation.

        Missing values render as `?` rather than raising: by the time this is
        called the violation is real, and a half-known location is more useful
        to the agent reading it than a crash.
        """
        filled = {name: "?" for name in MESSAGE_VARIABLES}
        filled.update({"rule": self.id, "principle": self.principle})
        filled.update({k: v for k, v in values.items() if v is not None})
        return self.message.format(**filled)


def load_rules(repo):
    """Every rule in `<repo>/.curamoir/rules/*.yaml`, validated together.

    Together, because two of the checks are cross-file: a rule id must be
    unique across the directory, and every principle id must resolve against
    the one `principles.md`.

    A repo with no rules directory loads as empty. That is every repo before
    E2-R6 ships rules, and what `/curamoir:doctor` counts.
    """
    repo = Path(repo)
    directory = repo / ".curamoir" / "rules"
    if not directory.is_dir():
        return []

    try:
        principles = {p.id for p in load_principles(repo).principles}
    except PrincipleError as invalid:
        raise RuleError(
            f"cannot validate rules because principles.md does not parse: "
            f"{invalid}"
        ) from invalid

    rules = []
    seen = {}
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        rule = parse_rule(
            path.read_text(encoding="utf-8"),
            source=path.name,
            known_principles=principles,
        )
        if rule.id in seen:
            raise RuleError(
                f"rule id {rule.id!r} is defined in both {seen[rule.id]} and "
                f"{path.name}. A finding carries its rule id (E2-R4) and E3-R5 "
                "counts fixes per rule, so one id must mean one rule."
            )
        seen[rule.id] = path.name
        rules.append(rule)
    return rules


def parse_rule(text, source, known_principles):
    """One rule from YAML text. Raises RuleError with `source` named.

    `known_principles` is passed in rather than read here so that a caller with
    text and no repository — a test, an editor integration — can still validate
    the whole rule including its principle reference.
    """
    data = _load_yaml(text, source)
    if not isinstance(data, dict):
        raise RuleError(f"{source}: expected a mapping of fields, got {type(data).__name__}")

    missing = [name for name in REQUIRED_FIELDS if not _present(data.get(name))]
    if missing:
        raise RuleError(
            f"{source}: missing required field(s): {', '.join(missing)}. "
            f"E2-R3 requires {', '.join(REQUIRED_FIELDS)}."
        )

    rule_id = str(data["id"]).strip()
    principle = _one_principle(data["principle"], rule_id, source)
    if principle not in known_principles:
        raise RuleError(
            f"{source}: rule {rule_id!r} cites principle {principle!r}, which is "
            f"not defined in principles.md. Defined ids: "
            f"{', '.join(sorted(known_principles)) or 'none'}. A rule citing an "
            "id that does not resolve lints normally and contributes its fixes "
            "to nothing, so the promotion ladder stops working for it silently."
        )

    severity = str(data["severity"]).strip()
    if severity not in SEVERITIES:
        raise RuleError(
            f"{source}: rule {rule_id!r} has severity {severity!r}; allowed: "
            f"{', '.join(SEVERITIES)}. E2-R4 exits non-zero at or above a "
            "configured severity, so it has to be one it can order."
        )

    matcher = str(data["matcher"]).strip()
    if matcher not in MATCHERS:
        raise RuleError(
            f"{source}: rule {rule_id!r} has matcher {matcher!r}; allowed: "
            f"{', '.join(MATCHERS)}. DEC-3: ast-grep is primary, regex is the "
            "escape hatch for what no AST pattern can express."
        )

    pattern = str(data["pattern"])
    if matcher == "regex":
        try:
            re.compile(pattern)
        except re.error as invalid:
            raise RuleError(
                f"{source}: rule {rule_id!r} has a pattern that is not a valid "
                f"regex: {invalid}. Caught now rather than mid-sweep, which "
                "would already have spent the budget for the rules before it."
            ) from invalid

    message = str(data["message"])
    _check_template(message, rule_id, source)

    languages = _sequence(data.get("languages"))
    unknown = [name for name in languages if name not in LANGUAGE_PATTERNS]
    if unknown:
        raise RuleError(
            f"{source}: rule {rule_id!r} names unknown language(s): "
            f"{', '.join(unknown)}. Known: {', '.join(sorted(LANGUAGE_PATTERNS))}. "
            "Use `files:` with globs for anything that is not a language."
        )

    target = str(data.get("target", "line")).strip()
    if target not in TARGETS:
        raise RuleError(
            f"{source}: rule {rule_id!r} has target {target!r}; allowed: "
            f"{', '.join(TARGETS)}."
        )

    return Rule(
        id=rule_id,
        principle=principle,
        severity=severity,
        matcher=matcher,
        pattern=pattern,
        message=message,
        fix=str(data.get("fix", "")),
        languages=languages,
        files=_sequence(data.get("files")),
        exclude=_sequence(data.get("exclude")),
        target=target,
        source=source,
    )


def _load_yaml(text, source):
    # Imported here, not at module scope: PyYAML is a runtime dependency a
    # plugin install does not guarantee, and `config` already fails with an
    # actionable message for that. A rule file is YAML, so there is no
    # stdlib-only path to offer.
    try:
        import yaml
    except ImportError as missing:  # pragma: no cover - environment-dependent
        raise RuleError(
            f"{source}: cannot read rule files without PyYAML installed. "
            "/curamoir:doctor reports this as a missing prerequisite."
        ) from missing
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as invalid:
        raise RuleError(f"{source}: not valid YAML: {invalid}") from invalid


def _present(value):
    """Whether a field was supplied. `0` and `False` are not valid for any
    field here, so emptiness is the only question."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value) or value == 0


def _one_principle(value, rule_id, source):
    """The single principle id this rule cites.

    "Exactly one" is E2-R3's wording and it is load-bearing: a rule covering
    two principles makes every fix it produces ambiguous for E3-R5's
    per-principle counts, which is what E4 promotes on.
    """
    if isinstance(value, (list, tuple, set)):
        raise RuleError(
            f"{source}: rule {rule_id!r} cites {len(value)} principles. E2-R3 "
            "requires exactly one, because E3-R5 counts fixes per principle "
            "and a fix attributed to two of them is attributed to neither. "
            "Split it into one rule per principle."
        )
    return str(value).strip()


def _check_template(message, rule_id, source):
    """Every `{name}` in `message` must be one E2-R4 will supply."""
    import string

    used = {
        name for _, name, _, _ in string.Formatter().parse(message) if name
    }
    unknown = sorted(used - set(MESSAGE_VARIABLES))
    if unknown:
        raise RuleError(
            f"{source}: rule {rule_id!r} has a message using unknown "
            f"variable(s): {', '.join(unknown)}. Available: "
            + ", ".join("{" + name + "}" for name in MESSAGE_VARIABLES)
            + ". The template is rendered once per violation during a sweep, so "
            "an unknown name is a failure in the middle of a paid run."
        )


def _sequence(value):
    """A field that may be a single string or a list of them."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),)
    return tuple(str(item).strip() for item in value)
