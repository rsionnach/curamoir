"""Schema-valid values that silently disabled the thing they configure.

`curamoir.yaml` is documented as editable, so a plausible typo in it is an
ordinary user action — and `/curamoir:doctor` reporting "config: valid" is the
developer's only confirmation. Each value below passed validation and then
turned a feature off with no error anywhere:

- `handoff.keep_history: 2.5` made every handoff write raise, which the hooks
  swallow, so handoffs stopped for good and the only symptom was a `status`
  age that kept growing. `-1` deleted the entire archive including the entry
  just written.
- `cadence.brief: nonsense` disabled the documented `weekly` mode. Everything
  else with a fixed vocabulary — `modules.strategy`, `filing.beads`,
  `budget.on_ceiling`, `rules.matcher` — is already a tuple; this one was a
  bare STRING.
"""

import pytest

from curamoir.config import ConfigError, load_config


@pytest.mark.parametrize("value", [2.5, -1, 10.5, True])
def test_a_non_integer_keep_history_is_rejected(value):
    with pytest.raises(ConfigError) as caught:
        load_config({"handoff": {"keep_history": value}})

    assert "handoff.keep_history" in str(caught.value)


@pytest.mark.parametrize("value", ["nonsense", "Weekly", ""])
def test_an_unknown_brief_cadence_is_rejected(value):
    with pytest.raises(ConfigError) as caught:
        load_config({"cadence": {"brief": value}})

    assert "cadence.brief" in str(caught.value)


def test_the_documented_cadences_are_accepted():
    """The vocabulary must not be so tight it rejects what the template ships."""
    for value in ("on_return", "weekly"):
        assert load_config({"cadence": {"brief": value}})["cadence"]["brief"] == value


def test_a_whole_handoff_cycle_survives_a_zero_keep_history(tmp_path):
    """`0` is a legitimate "do not keep an archive", not an error."""
    from curamoir.handoff import write_handoff
    from curamoir.scaffold import init_repo

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    assert write_handoff(tmp_path, note="one", keep_history=0) is not None
    assert write_handoff(tmp_path, note="two", keep_history=0) is not None
