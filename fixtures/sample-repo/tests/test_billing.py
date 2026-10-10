"""Tests that pass as shipped.

They deliberately avoid asserting the planted bugs, so the suite is green and
E3-R4 has a green baseline to protect. The planted bugs are for the r5 review
to find (E5), not for these tests to catch.

One test is itself a plant: `test_load_rates_reads_the_file` asserts that a
mock was called rather than what `load_rates` returned, which is what the
bundled `tests-assert-behaviour` rule exists to find (E2-R6, PLANTED.json).
It still passes.
"""

from unittest import mock

from sample.core.billing import line_total, load_rates
from sample.web.handlers import paginate


def test_line_total_applies_tax():
    assert line_total(100, 1) == 120.0


def test_paginate_returns_a_page_sized_slice():
    assert len(paginate(list(range(100)), 2)) == 20


def test_load_rates_reads_the_file():
    with mock.patch("builtins.open", mock.mock_open(read_data="0.2")) as mock_open:
        load_rates("rates.txt")
    assert mock_open.called  # PLANTED: tests-assert-behaviour
