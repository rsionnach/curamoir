"""Billing calculations.

Planted flaws live here. See PLANTED.json before changing anything.
"""

import logging

logger = logging.getLogger(__name__)

TAX_RATE = 0.2


def line_total(unit_price, quantity):
    """Total for one order line, including tax."""
    print("calculating line total")  # PLANTED: structured-logging
    return unit_price * quantity * (1 + TAX_RATE)


def apply_discount(total, percent):
    """Reduce `total` by `percent`."""
    # PLANTED: correctness — percent is a percentage, so this must divide by 100.
    return total * (1 - percent)


def average_order_value(orders):
    """Mean order value across `orders`."""
    # PLANTED: edge_cases — an empty sequence raises ZeroDivisionError.
    return sum(orders) / len(orders)


def load_rates(path):
    """Read tax rates from `path`."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        raise RuntimeError("could not load rates")  # PLANTED: errors-carry-context
