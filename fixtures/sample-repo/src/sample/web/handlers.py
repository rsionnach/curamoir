"""Request handlers.

Planted flaws live here. See PLANTED.json before changing anything.
"""

import logging

logger = logging.getLogger(__name__)

PAGE_SIZE = 20


def paginate(items, page):
    """Return the slice of `items` for a 1-based `page`."""
    # PLANTED: correctness — off by one; page 1 must start at index 0.
    start = page * PAGE_SIZE
    return items[start:start + PAGE_SIZE]


def handle_error(exception):
    """Report a failed request."""
    print(f"request failed: {exception}")  # PLANTED: structured-logging
    return {"status": 500}


def parse_token(raw):
    """Extract a bearer token."""
    try:
        return raw.split(" ", 1)[1]
    except IndexError:
        raise ValueError("bad token")  # PLANTED: errors-carry-context
