"""repo_config's pair of readers: `effective_config` falls back to the
defaults, `strict_config` refuses a config that exists but cannot be loaded.

These call the readers directly. The tests that show a strict refusal
reaching the sweep as a `guard_error` line, through job_guard.run_guarded,
are in tests/test_job_guard.py.
"""

import os

import pytest

from curamoir.config import DEFAULTS, ConfigError
from curamoir.repo_config import effective_config, strict_config


def test_the_strict_reader_refuses_what_the_tolerant_one_falls_back_from(harness):
    """repo_config's pair: the guards read strictly, the hooks tolerantly."""
    harness.write(".curamoir/curamoir.yaml", "version: 1\nbudget: [unclosed\n")

    assert effective_config(harness.repo) == DEFAULTS
    with pytest.raises(ConfigError, match="not valid YAML"):
        strict_config(harness.repo)


def test_the_strict_reader_gives_the_defaults_when_there_is_no_config(harness):
    assert strict_config(harness.repo) == DEFAULTS


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root searches a directory whatever its mode")
def test_a_config_in_a_directory_that_cannot_be_searched_refuses_rather_than_defaulting(harness):
    """Without the x bit nothing inside .curamoir/ can be looked up, so a
    config that exists looks absent to a check that swallows every error.
    It exists but cannot be loaded: a refusal, not the defaults."""
    harness.write(".curamoir/curamoir.yaml", "version: 1\nbudget:\n  monthly_ceiling_usd: 5\n")
    config_dir = harness.repo / ".curamoir"
    config_dir.chmod(0o600)
    try:
        with pytest.raises(ConfigError, match="cannot be read"):
            strict_config(harness.repo)
    finally:
        config_dir.chmod(0o755)


def test_a_curamoir_that_is_a_file_gives_the_defaults(harness):
    """No config can exist inside a file, so there is nothing a refusal would
    protect: the defaults, as lint gave before it read strictly. (A FIFO or
    directory at the config path itself is different; something is there.)"""
    harness.write(".curamoir", "not a directory\n")

    assert strict_config(harness.repo) == DEFAULTS


def test_a_config_whose_bytes_are_not_utf8_refuses(harness):
    """The file is there and meant something, but cannot be decoded into the
    text that says what; the defaults would stand in for limits unread."""
    (harness.repo / ".curamoir").mkdir(exist_ok=True)
    (harness.repo / ".curamoir" / "curamoir.yaml").write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigError, match="cannot be read"):
        strict_config(harness.repo)


def test_each_schema_problem_is_its_own_entry_naming_the_path(harness):
    """One ConfigError entry per problem, each naming the file: a reader
    that lists `problems` one per line shows every one with where it is."""
    harness.write(".curamoir/curamoir.yaml",
                  "version: 1\nbudget:\n  monthly_ceiling_usd: -1\n"
                  "sweep:\n  max_files_per_pr: -5\n")

    with pytest.raises(ConfigError) as raised:
        strict_config(harness.repo)

    problems = raised.value.problems
    path = str(harness.repo / ".curamoir" / "curamoir.yaml")
    assert len(problems) == 2
    assert all(problem.startswith(f"{path}: invalid: ") for problem in problems)
    assert "monthly_ceiling_usd" in problems[0] and "max_files_per_pr" in problems[1]
