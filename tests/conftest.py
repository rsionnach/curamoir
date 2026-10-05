import pytest

from tests.harness import Harness


@pytest.fixture
def harness(tmp_path):
    """A throwaway git repo with a ledger, for running one guardrail at a time."""
    return Harness(tmp_path / "repo")
