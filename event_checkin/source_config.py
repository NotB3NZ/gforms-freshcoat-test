"""Supported source sets; adding an optional source never changes old datasets."""

BASE_SOURCES = ("regular", "discount_15", "discount_10")
SOURCES = BASE_SOURCES + ("special",)


def valid_source_ids(ids):
    ids = list(ids)
    return len(ids) == len(set(ids)) and set(ids) in (set(BASE_SOURCES), set(SOURCES))
