"""Tests that pass as shipped.

They deliberately avoid asserting the planted bugs, so the suite is green and
E3-R4 has a green baseline to protect. The planted bugs are for the r5 review
to find (E5), not for these tests to catch.
"""

from sample.core.billing import line_total
from sample.web.handlers import paginate


def test_line_total_applies_tax():
    assert line_total(100, 1) == 120.0


def test_paginate_returns_a_page_sized_slice():
    assert len(paginate(list(range(100)), 2)) == 20
