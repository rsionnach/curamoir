"""E1-R5: configuration is validated on load, with errors that name the key.

"Errors that name the offending key" is the requirement, and it is the whole
value: a config error a returning developer cannot locate costs more than the
feature the key controls.

The defaults here are the spec's §6.3 block as amended by the five decisions:
  DEC-1  models.cheap/deep are claude-sonnet-5 / claude-opus-5; batch + prices
  DEC-2  modules.strategy defaults to `auto`, not `top-level-dirs`
  DEC-3  rules take matcher `ast-grep` (default) or `regex`
  DEC-4  filing.github defaults to OFF — GitHub mirroring is opt-in
"""

import pytest

from curamoir.config import DEFAULTS, ConfigError, load_config


def test_an_empty_config_yields_the_documented_defaults():
    config = load_config({})

    assert config["version"] == 1
    assert config["budget"]["monthly_ceiling_usd"] == 15
    assert config["sweep"]["max_open_prs"] == 3
    assert config["promotion"]["fix_threshold"] == 3


def test_module_strategy_defaults_to_auto_not_top_level_dirs():
    """DEC-2: top-level-dirs yields one code module named `src` on a src-layout
    repo, which breaks the review selector and per-module back-off."""
    assert load_config({})["modules"]["strategy"] == "auto"


def test_top_level_dirs_remains_a_valid_choice():
    """Rejected as the DEFAULT, not as an option — it fits some repos."""
    config = load_config({"modules": {"strategy": "top-level-dirs"}})

    assert config["modules"]["strategy"] == "top-level-dirs"


def test_build_artefacts_are_excluded_by_default():
    """Otherwise `build`, `dist` and `*.egg-info` are graded as modules."""
    excludes = load_config({})["modules"]["exclude"]

    for noise in ("build/", "dist/", "docs/"):
        assert noise in excludes


def test_model_tiers_default_to_the_verified_current_ids():
    models = load_config({})["models"]

    assert models["cheap"] == "claude-sonnet-5"
    assert models["deep"] == "claude-opus-5"


def test_github_filing_is_off_by_default():
    """DEC-4: GitHub mirroring is a nice-to-have for popular repos with outside
    contributors, not the default for a solo developer."""
    assert load_config({})["filing"]["github"] is False
    assert load_config({})["filing"]["beads"] == "auto"


def test_the_price_table_ships_with_defaults_and_is_editable():
    prices = load_config({})["budget"]["prices"]

    assert prices["claude-sonnet-5"] == {"input": 2.00, "output": 10.00}
    assert prices["claude-opus-5"] == {"input": 5.00, "output": 25.00}


def test_a_user_price_overrides_the_default_without_dropping_the_others():
    config = load_config({"budget": {"prices": {"claude-opus-5": {"input": 9.0, "output": 45.0}}}})

    assert config["budget"]["prices"]["claude-opus-5"]["input"] == 9.0
    assert "claude-sonnet-5" in config["budget"]["prices"], "other models were dropped"


# --- validation errors name the key ---------------------------------------

def test_an_unknown_top_level_key_is_rejected_by_name():
    with pytest.raises(ConfigError) as caught:
        load_config({"modules": {}, "sweeep": {}})

    assert "sweeep" in str(caught.value)


def test_an_unknown_nested_key_is_rejected_with_its_full_path():
    with pytest.raises(ConfigError) as caught:
        load_config({"sweep": {"max_open_prz": 3}})

    assert "sweep.max_open_prz" in str(caught.value)


def test_a_wrong_type_names_the_key_and_the_expected_type():
    with pytest.raises(ConfigError) as caught:
        load_config({"budget": {"monthly_ceiling_usd": "fifteen dollars"}})

    message = str(caught.value)
    assert "budget.monthly_ceiling_usd" in message
    assert "number" in message.lower()


def test_a_value_outside_an_enum_names_the_key_and_the_allowed_values():
    with pytest.raises(ConfigError) as caught:
        load_config({"budget": {"on_ceiling": "explode"}})

    message = str(caught.value)
    assert "budget.on_ceiling" in message
    assert "pause" in message and "warn" in message


def test_a_bad_module_strategy_names_the_allowed_values():
    with pytest.raises(ConfigError) as caught:
        load_config({"modules": {"strategy": "guess"}})

    assert "modules.strategy" in str(caught.value)
    assert "auto" in str(caught.value)


def test_every_problem_is_reported_not_just_the_first():
    """A developer fixing config one error per run is a developer who gives up."""
    with pytest.raises(ConfigError) as caught:
        load_config({
            "budget": {"on_ceiling": "explode", "monthly_ceiling_usd": "lots"},
            "nonsense": True,
        })

    message = str(caught.value)
    assert "budget.on_ceiling" in message
    assert "budget.monthly_ceiling_usd" in message
    assert "nonsense" in message


def test_a_model_id_is_not_validated_against_a_hardcoded_list():
    """The spec says "verify current model ids". Validate the KEY, not the value:
    a whitelist would reject every model released after this code was written."""
    config = load_config({"models": {"cheap": "claude-some-future-model"}})

    assert config["models"]["cheap"] == "claude-some-future-model"


def test_the_shipped_template_is_valid_and_matches_the_defaults():
    """templates/curamoir.yaml is what /curamoir:init writes, so a drift between
    it and the schema would ship a repo that fails its own validation."""
    from pathlib import Path

    template = Path(__file__).resolve().parents[2] / "templates" / "curamoir.yaml"
    assert template.is_file(), "templates/curamoir.yaml not shipped"

    from curamoir.config import load_config_text

    config = load_config_text(template.read_text(encoding="utf-8"))
    assert config["modules"]["strategy"] == DEFAULTS["modules"]["strategy"]
    assert config["filing"]["github"] is DEFAULTS["filing"]["github"]


def test_the_template_keeps_its_comments_because_init_writes_commented_defaults():
    """E1-R2 must write "a curamoir.yaml with commented defaults", so the
    comments are product, not decoration."""
    from pathlib import Path

    template = Path(__file__).resolve().parents[2] / "templates" / "curamoir.yaml"
    text = template.read_text(encoding="utf-8")

    assert text.count("#") >= 20, "template has lost its explanatory comments"


def test_the_lint_threshold_defaults_to_error_and_is_a_closed_vocabulary():
    """E2-R4: `curamoir-lint` exits non-zero on a violation at or above the
    CONFIGURED severity, so the threshold is a key. Closed, because a typo here
    would otherwise move the gate without anybody deciding to: `fail_on: eror`
    must be refused by name, not read as "never fail"."""
    assert DEFAULTS["rules"]["fail_on"] == "error"
    for severity in ("info", "warning", "error"):
        assert load_config({"rules": {"fail_on": severity}})["rules"]["fail_on"] == severity

    with pytest.raises(ConfigError) as caught:
        load_config({"rules": {"fail_on": "eror"}})

    assert "rules.fail_on" in str(caught.value)


def test_the_template_states_the_lint_threshold():
    """The template shows every default so a developer can see what will happen
    before it does; a gate threshold is the last thing to leave implicit."""
    from pathlib import Path

    from curamoir.config import load_config_text

    template = Path(__file__).resolve().parents[2] / "templates" / "curamoir.yaml"
    text = template.read_text(encoding="utf-8")

    assert "fail_on:" in text
    assert load_config_text(text)["rules"]["fail_on"] == DEFAULTS["rules"]["fail_on"]


def test_a_duplicate_key_is_reported_not_last_wins():
    """`fail_on: error` then `fail_on: info` would silently loosen the gate."""
    from curamoir.config import load_config_text

    with pytest.raises(ConfigError, match="fail_on"):
        load_config_text("rules:\n  fail_on: error\n  fail_on: info\n")


def test_a_merge_key_may_still_be_overridden():
    """`<<: *base` then the key itself is YAML's override idiom, not a duplicate."""
    from curamoir.config import load_config_text

    config = load_config_text("rules:\n  <<: {fail_on: error}\n  fail_on: info\n")
    assert config["rules"]["fail_on"] == "info"
