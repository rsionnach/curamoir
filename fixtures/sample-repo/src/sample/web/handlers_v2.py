"""PLANTED: no-file-variant-proliferation.

A parallel implementation of handlers.py, which is exactly the drift the
principle exists to stop. The file's existence is the violation.
"""

PAGE_SIZE = 20


def paginate(items, page):
    start = (page - 1) * PAGE_SIZE
    return items[start:start + PAGE_SIZE]
